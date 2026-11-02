from __future__ import annotations

import hashlib
import hmac as hmac_module
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import AuditAction, MemberStatus, RoleName, resolve_role_name
from app.core.exceptions import (
    AlreadyExistsException,
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
    UserNotFoundException,
    ValidationException,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)
from app.integrations.firebase import verify_id_token as firebase_verify_id_token
from app.integrations.msg91 import send_otp as msg91_send_otp
from app.integrations.sms import is_dev_sms, send_otp
from app.models.family import FamilyMember
from app.models.group import SocialGroup
from app.models.member_group import MemberGroupMembership
from app.models.otp_code import OtpCode
from app.models.user import RefreshToken, Role, User
from app.repositories.member_repository import MemberRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.utils.helpers import hash_refresh_token
from app.utils.validators import normalize_phone_number, validate_password

settings = get_settings()


class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.users = UserRepository(session)
        self.members = MemberRepository(session)
        self.audit = AuditService(session)

    async def send_registration_otp(
        self,
        *,
        phone: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """Send an MSG91 OTP for the registration flow.

        DEPRECATED: the registration flow now uses Firebase Phone
        Authentication. This MSG91 path is kept for backward compatibility
        only and will be removed once clients migrate.

        Only numbers that are not already registered get an OTP. The OTP is
        generated, delivered, and stored by MSG91 — this server never sees or
        stores it. Returns MSG91's ``req_id``.
        """
        normalized = normalize_phone_number(phone)
        in_use, _ = await self.members.check_phone_in_use(normalized)
        if in_use:
            raise AlreadyExistsException(
                "This phone number is already registered or in use by another member."
            )

        req_id = await msg91_send_otp(normalized)
        await self.audit.log(
            AuditAction.OTP_REQUEST,
            entity_type="registration",
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return req_id

    async def register(
        self,
        *,
        full_name: str,
        email: str | None,
        password: str,
        phone_number: str,
        spouse_mobile: str | None = None,
        group_id: UUID | None = None,
        group_name: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """Register a new user with the phone number supplied by the client."""
        full_name = full_name.strip()
        if not full_name:
            raise BadRequestException("Full name cannot be empty")
        email = email.strip().lower() if email else None

        verified_phone = normalize_phone_number(phone_number)

        phone_in_use, _ = await self.members.check_phone_in_use(verified_phone)
        if phone_in_use:
            raise AlreadyExistsException(
                "This phone number is already registered or in use by another member."
            )

        norm_spouse_phone: str | None = None
        if spouse_mobile and spouse_mobile.strip():
            norm_spouse_phone = normalize_phone_number(spouse_mobile.strip())
            if norm_spouse_phone == verified_phone:
                raise AlreadyExistsException(
                    "Member mobile and spouse mobile cannot be the same number."
                )
            spouse_in_use, _ = await self.members.check_phone_in_use(
                norm_spouse_phone
            )
            if spouse_in_use:
                raise AlreadyExistsException(
                    "Spouse mobile number is already registered or in use by another member."
                )

        if email and await self.users.get_by_email(email):
            raise AlreadyExistsException("This email is already registered.")

        role_row = await self._resolve_registration_role(None)
        if role_row is None:
            raise BadRequestException("Role is not configured yet")

        selected_group = None
        if group_id is not None:
            result = await self.session.execute(
                select(SocialGroup).where(
                    SocialGroup.id == group_id, SocialGroup.is_deleted.is_(False)
                )
            )
            selected_group = result.scalar_one_or_none()
            if selected_group is None:
                raise BadRequestException("Selected group not found")
        elif group_name is not None and group_name.strip():
            cleaned_name = group_name.strip().casefold()
            result = await self.session.execute(
                select(SocialGroup).where(
                    func.lower(func.trim(SocialGroup.name)) == cleaned_name,
                    SocialGroup.is_deleted.is_(False),
                )
            )
            selected_group = result.scalars().first()
            if selected_group is None:
                raise BadRequestException("Selected group not found")

        selected_group_id = selected_group.id if selected_group is not None else None
        selected_group_name = selected_group.name if selected_group is not None else None

        user = await self.users.create(
            email=email,
            full_name=full_name,
            phone_number=verified_phone,
            password_hash=hash_password(password),
            is_active=False,
            is_phone_verified=True,
            role_id=role_row.id,
            group_id=selected_group_id,
        )
        if selected_group is not None:
            user.group = selected_group

        name_parts = full_name.strip().split(maxsplit=1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]

        profile_data: dict[str, Any] = {
            "full_name": full_name,
            "phone": verified_phone,
        }
        if norm_spouse_phone:
            profile_data["spouse_mobile"] = norm_spouse_phone
        if selected_group_name:
            profile_data["social_group_name"] = selected_group_name
        if email:
            profile_data["email"] = email

        member = await self.members.create(
            user_id=user.id,
            group_id=selected_group_id,
            first_name=first_name,
            last_name=last_name,
            contact_email=email,
            contact_phone=verified_phone,
            membership_status=MemberStatus.PENDING.value,
            profile_data=profile_data,
        )

        if norm_spouse_phone:
            spouse_member = FamilyMember(
                member_id=member.id,
                name="Spouse",
                relationship_type="spouse",
                contact_phone=norm_spouse_phone,
            )
            self.session.add(spouse_member)

        if selected_group is not None:
            membership = MemberGroupMembership(
                member_id=member.id,
                group_id=selected_group.id,
                priority=1,
                joined_at=datetime.now(UTC),
            )
            self.session.add(membership)

        await self.audit.log(
            AuditAction.REGISTER,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()

        return {
            "user": user,
            "message": "Your registration is pending approval by the Group Admin. Please wait for approval before logging in.",
            "tokens": None,
        }

    async def _assert_user_approved(self, user: User) -> None:
        member = await self.members.get_by_user_id(user.id)
        if member is not None:
            if member.membership_status == MemberStatus.REJECTED.value:
                reason = member.rejection_reason
                detail = (
                    f"Your registration has been rejected. Reason: {reason}"
                    if reason
                    else "Your registration has been rejected by the Group Admin."
                )
                raise ForbiddenException(detail)
            if (
                member.membership_status == MemberStatus.PENDING.value
                or not user.is_active
            ):
                raise ForbiddenException(
                    "Your registration is pending approval by the Group Admin. Please wait for approval before logging in."
                )
            if member.membership_status != MemberStatus.APPROVED.value:
                raise ForbiddenException(
                    "Your registration is pending approval by the Group Admin. Please wait for approval before logging in."
                )
        elif not user.is_active:
            raise ForbiddenException(
                "Your registration is pending approval by the Group Admin. Please wait for approval before logging in."
            )

    async def _login_existing_firebase_user(
        self,
        user: User,
        firebase_uid: str | None,
        *,
        ip_address: str | None,
        user_agent: str | None,
        verified_phone: str | None = None,
    ) -> dict:
        user.is_phone_verified = True
        user.last_login_at = datetime.now(UTC)
        is_primary = True
        if verified_phone:
            try:
                norm_v = normalize_phone_number(verified_phone)
                norm_u = normalize_phone_number(user.phone_number)
                is_primary = norm_v == norm_u
            except Exception:
                pass
        if is_primary and firebase_uid and not user.firebase_uid:
            user.firebase_uid = firebase_uid
        await self.audit.log(
            AuditAction.LOGIN,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return await self._issue_tokens(user, ip_address, user_agent)

    async def _get_user_by_phone_forms(self, phone: str) -> User | None:
        normalized = normalize_phone_number(phone)
        local_phone = normalized.removeprefix("+91")
        user = await self.users.get_by_phone(local_phone)
        if user is None and local_phone != normalized:
            user = await self.users.get_by_phone(normalized)
        if user is None:
            member = await self.members.get_by_spouse_phone(phone)
            if member is not None and member.user_id:
                user = await self.users.get_by_id(member.user_id)
        return user

    async def _resolve_registration_role(self, role: str | None) -> Role:
        if role is None or not role.strip():
            role_name = RoleName.MEMBER.value
        else:
            candidate = role.strip().upper()
            if candidate != RoleName.SUPER_ADMIN.value:
                raise BadRequestException(
                    "Invalid role. Allowed roles: MEMBER, SUPER_ADMIN"
                )
            role_name = candidate
        role_result = await self.session.execute(
            select(Role).where(Role.name == role_name)
        )
        role_row = role_result.scalar_one_or_none()
        if role_row is None:
            raise BadRequestException(f"{role_name} role is not configured yet")
        return role_row

    async def login(
        self,
        *,
        email: str | None = None,
        phone_number: str | None = None,
        password: str | None = None,
        id_token: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        if id_token:
            claims = firebase_verify_id_token(id_token)
            firebase_uid = claims.get("uid")
            verified_phone = claims.get("phone_number")
            if not firebase_uid or not verified_phone:
                raise UnauthorizedException(
                    "Firebase token does not contain a verified phone number"
                )
            normalized = normalize_phone_number(str(verified_phone))
            user = await self._get_user_by_phone_forms(normalized)
            if user is None or user.is_deleted:
                raise UnauthorizedException(
                    "Invalid phone number or password"
                )
            await self._assert_user_approved(user)
            return await self._login_existing_firebase_user(
                user,
                firebase_uid,
                ip_address=ip_address,
                user_agent=user_agent,
                verified_phone=str(verified_phone),
            )

        if not email and not phone_number:
            raise UnauthorizedException("Invalid email or phone number or password")
        if password is None:
            raise UnauthorizedException("Invalid email or phone number or password")

        if phone_number:
            normalized = normalize_phone_number(phone_number)
            user = await self._get_user_by_phone_forms(normalized)
            credentials = "Invalid phone number or password"
        else:
            assert email is not None
            user = await self.users.get_by_email(email.strip().lower())
            credentials = "Invalid email or password"

        if user is None or user.is_deleted or not verify_password(password, user.password_hash):
            raise UnauthorizedException(credentials)

        await self._assert_user_approved(user)

        user.last_login_at = datetime.now(UTC)
        await self.session.flush()

        await self.audit.log(
            AuditAction.LOGIN,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return await self._issue_tokens(user, ip_address, user_agent)

    async def login_with_firebase_otp(
        self,
        *,
        id_token: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        claims = firebase_verify_id_token(id_token)
        firebase_uid = claims.get("uid") or claims.get("sub") or claims.get("user_id")
        verified_phone = claims.get("phone_number")
        if not firebase_uid or not verified_phone:
            raise UnauthorizedException(
                "Firebase token does not contain a verified phone number"
            )

        user = await self._get_user_by_phone_forms(str(verified_phone))
        if user is None or user.is_deleted:
            raise UserNotFoundException()
        await self._assert_user_approved(user)

        return await self._login_existing_firebase_user(
            user,
            firebase_uid,
            ip_address=ip_address,
            user_agent=user_agent,
            verified_phone=str(verified_phone),
        )

    async def oauth2_admin_token(
        self,
        *,
        username: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """OAuth2 password-flow token endpoint used by Swagger's Authorize.

        username is treated as the account's phone number. Only SUPER_ADMIN
        accounts are allowed to authenticate through this flow.
        """
        user = await self.users.get_active_by_phone(
            normalize_phone_number(username)
        )
        if user is None or not verify_password(password, user.password_hash):
            raise UnauthorizedException("Invalid phone number or password")

        resolved = resolve_role_name(user.role.name) if user.role else None
        if resolved is not RoleName.SUPER_ADMIN:
            raise ForbiddenException(
                "Only SUPER_ADMIN accounts can authenticate for Swagger access"
            )

        user.last_login_at = datetime.now(UTC)
        await self.audit.log(
            AuditAction.LOGIN,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(user)

        return {
            "access_token": create_access_token(subject=str(user.id)),
            "token_type": "bearer",
        }

    async def request_otp(
        self,
        *,
        phone: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        normalized = normalize_phone_number(phone)
        user = await self._get_user_by_phone_forms(normalized)
        if user is None:
            raise NotFoundException(
                "Account not found for this phone number. Please register first."
            )
        if not user.is_active:
            raise UnauthorizedException("Account is not active")

        now = datetime.now(UTC)
        hour_ago = now - timedelta(hours=1)
        recent = (
            await self.session.execute(
                select(func.count(OtpCode.id)).where(
                    OtpCode.phone_number == normalized,
                    OtpCode.created_at >= hour_ago,
                )
            )
        ).scalar_one()
        if recent >= settings.otp_max_per_phone_per_hour:
            raise BadRequestException(
                "Too many OTP requests for this number. Please try again later."
            )

        await self.session.execute(
            update(OtpCode)
            .where(
                OtpCode.phone_number == normalized,
                OtpCode.verified_at.is_(None),
                OtpCode.expires_at > now,
            )
            .values(expires_at=now)
        )

        code = self._generate_otp_code()
        expires_at = now + timedelta(seconds=settings.otp_expire_seconds)
        otp = OtpCode(
            phone_number=normalized,
            user_id=user.id,
            code_hash=self._hash_otp(normalized, code),
            purpose="LOGIN",
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.session.add(otp)
        await self.audit.log(
            AuditAction.OTP_REQUEST,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await send_otp(normalized, code, round(settings.otp_expire_seconds / 60))
        return {
            "dev_otp": code if is_dev_sms() else None,
            "expires_in": settings.otp_expire_seconds,
        }

    async def verify_otp_login(
        self,
        *,
        phone: str,
        code: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        normalized = normalize_phone_number(phone)
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(OtpCode)
            .where(
                OtpCode.phone_number == normalized,
                OtpCode.verified_at.is_(None),
            )
            .order_by(OtpCode.created_at.desc())
            .limit(1)
        )
        otp = result.scalar_one_or_none()
        if otp is None:
            raise UnauthorizedException("Invalid or expired OTP")

        expected_hash = self._hash_otp(normalized, code)
        if not hmac_module.compare_digest(expected_hash, otp.code_hash):
            otp.attempts += 1
            if otp.attempts >= settings.otp_max_attempts:
                otp.expires_at = now
            await self.audit.log(
                AuditAction.OTP_FAILED,
                user_id=otp.user_id,
                entity_type="otp_code",
                entity_id=otp.id,
                details={
                    "phone": self._mask_phone(normalized),
                    "attempt": otp.attempts,
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await self.session.commit()
            raise UnauthorizedException("Invalid OTP")

        if otp.expires_at <= now:
            raise UnauthorizedException("OTP has expired. Please request a new one.")

        user = await self.users.get_by_id(otp.user_id) if otp.user_id else None
        if user is None or not user.is_active:
            raise UnauthorizedException("Account not found or inactive")

        otp.verified_at = now
        user.is_phone_verified = True
        user.last_login_at = now
        await self.audit.log(
            AuditAction.OTP_VERIFY,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(user)
        return await self._issue_tokens(user, ip_address, user_agent)

    @staticmethod
    def _generate_otp_code() -> str:
        return f"{secrets.randbelow(10 ** settings.otp_length):0{settings.otp_length}d}"

    @staticmethod
    def _hash_otp(phone: str, code: str) -> str:
        return hashlib.sha256(
            f"{phone}:{code}:{settings.jwt_secret}".encode()
        ).hexdigest()

    @staticmethod
    def _mask_phone(phone: str) -> str:
        return f"{phone[:5]}****{phone[-2:]}"

    async def refresh(
        self,
        *,
        refresh_token: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        payload = decode_refresh_token(refresh_token)
        if payload is None:
            raise UnauthorizedException("Invalid or expired refresh token")

        token_hash = hash_refresh_token(refresh_token)
        result = await self.session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        stored = result.scalar_one_or_none()
        if (
            stored is None
            or stored.revoked_at is not None
            or stored.expires_at <= datetime.now(UTC)
        ):
            raise UnauthorizedException("Refresh token has been revoked or expired")

        user = await self.users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedException("User account is not active")

        stored.revoked_at = datetime.now(UTC)
        await self.session.flush()
        return await self._issue_tokens(user, ip_address, user_agent)

    async def logout(self, refresh_token: str, user_id: UUID | None = None) -> None:
        token_hash = hash_refresh_token(refresh_token)
        result = await self.session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        stored = result.scalar_one_or_none()
        if stored and stored.revoked_at is None:
            stored.revoked_at = datetime.now(UTC)
        await self.audit.log(
            AuditAction.LOGOUT, user_id=user_id, ip_address=None, user_agent=None
        )
        await self.session.flush()

    async def change_password(
        self,
        user: User,
        *,
        current_password: str | None = None,
        new_password: str,
    ) -> None:
        if current_password is not None:
            if not verify_password(current_password, user.password_hash):
                raise BadRequestException("Current password is incorrect")
        user.password_hash = hash_password(new_password)
        await self.audit.log(
            AuditAction.PASSWORD_CHANGE,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
        )
        await self.session.flush()

    async def forgot_password(
        self,
        *,
        phone_number: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> User:
        """Reset a user's password by phone number.

        DEV/TEST ONLY: this endpoint performs NO ownership verification
        (no OTP, SMS, email, or token). Anyone who knows another user's
        phone number could change that user's password. It is intentionally
        unsecured for development/testing and is NOT suitable for production.
        """
        normalized = normalize_phone_number(phone_number)
        user = await self.users.get_by_phone(normalized)
        if user is None:
            raise NotFoundException("User")

        user.password_hash = hash_password(new_password)
        await self.audit.log(
            AuditAction.PASSWORD_CHANGE,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return user

    async def reset_password(
        self,
        *,
        phone_number: str,
        password: str,
        confirm_password: str,
        id_token: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        claims = firebase_verify_id_token(id_token)
        verified_phone = claims.get("phone_number")
        if not verified_phone:
            raise UnauthorizedException(
                "Firebase token does not contain a verified phone number"
            )

        try:
            verified_phone = normalize_phone_number(str(verified_phone))
            provided_phone = normalize_phone_number(phone_number)
        except ValidationException:
            raise BadRequestException("मोबाइल नंबर सही नहीं है") from None
        if verified_phone != provided_phone:
            raise BadRequestException(
                "मोबाइल नंबर Firebase टोकन से मेल नहीं खाता"
            )

        if len(password) < 8:
            raise BadRequestException("पासवर्ड कम से कम 8 अक्षरों का होना चाहिए")
        if password != confirm_password:
            raise BadRequestException("पासवर्ड और पुष्टि पासवर्ड मेल नहीं खाते")
        try:
            validate_password(password)
        except ValidationException:
            raise BadRequestException(
                "पासवर्ड मजबूत होना चाहिए: इसमें कम से कम एक छोटा अक्षर, "
                "एक बड़ा अक्षर और एक अंक होना चाहिए"
            ) from None

        user = await self._get_user_by_phone_forms(verified_phone)
        if user is None:
            raise UserNotFoundException()

        user.password_hash = hash_password(password)
        await self.audit.log(
            AuditAction.PASSWORD_CHANGE,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"phone": self._mask_phone(verified_phone)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()

    async def _issue_tokens(
        self,
        user: User,
        ip_address: str | None,
        user_agent: str | None,
    ) -> dict:
        access_token = create_access_token(subject=str(user.id))
        refresh_token = create_refresh_token(subject=str(user.id))

        payload = decode_refresh_token(refresh_token)
        expires_at = (
            datetime.fromtimestamp(payload["exp"], tz=UTC)
            if payload
            else datetime.now(UTC)
        )

        await self.users.create_refresh_token(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(user)

        return {
            "user": user,
            "tokens": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": settings.access_token_expire_minutes * 60,
            },
        }
