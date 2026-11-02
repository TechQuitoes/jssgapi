from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.core.exceptions import BadRequestException
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    OAuth2TokenResponse,
    OtpLoginRequest,
    OtpRequest,
    OtpVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SendRegisterOtpResponse,
)
from app.schemas.user import UserOut
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Auth"])


def _request_context(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.login(
        email=body.email,
        phone_number=body.phone_number,
        password=body.password,
        id_token=body.id_token,
        ip_address=ip,
        user_agent=ua,
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post("/login/otp", response_model=AuthResponse)
async def otp_login(body: OtpLoginRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    result = await AuthService(db).login_with_firebase_otp(
        id_token=body.id_token,
        ip_address=ip,
        user_agent=ua,
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post(
    "/token",
    response_model=OAuth2TokenResponse,
    summary="OAuth2 token endpoint (Swagger Authorize)",
)
async def oauth2_token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    db: DBSession,
):
    """OAuth2 password-flow token endpoint used by Swagger's Authorize dialog.

    Accepts `application/x-www-form-urlencoded` data where `username` is the
    ADMIN account's phone number and `password` is the account password.
    Only ADMIN accounts are accepted.
    """
    ip, ua = _request_context(request)
    return await AuthService(db).oauth2_admin_token(
        username=form.username,
        password=form.password,
        ip_address=ip,
        user_agent=ua,
    )


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.register(
        full_name=body.full_name,
        email=body.email,
        password=body.password,
        phone_number=body.phone_number,
        spouse_mobile=body.spouse_mobile,
        group_id=body.group_id,
        group_name=body.group_name,
        ip_address=ip,
        user_agent=ua,
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result.get("tokens"),
        message=result.get("message"),
    )


@router.post("/otp/send-register", response_model=SendRegisterOtpResponse)
async def send_register_otp(body: OtpRequest, request: Request, db: DBSession):
    """Send an MSG91 OTP for new-user registration.

    DEPRECATED: the registration flow now uses Firebase Phone
    Authentication. Kept for backward compatibility only.

    The OTP is generated, delivered, and stored by MSG91 — never by this
    server. Returns a ``req_id`` that must be passed along with the OTP to
    ``POST /auth/register``.
    """
    ip, ua = _request_context(request)
    service = AuthService(db)
    req_id = await service.send_registration_otp(
        phone=body.phone, ip_address=ip, user_agent=ua
    )
    return SendRegisterOtpResponse(req_id=req_id)


@router.post("/otp/request")
async def request_otp(body: OtpRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    result = await AuthService(db).request_otp(
        phone=body.phone, ip_address=ip, user_agent=ua
    )
    return ok("OTP sent successfully", result)


@router.post("/otp/verify", response_model=AuthResponse)
async def verify_otp(body: OtpVerifyRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.verify_otp_login(
        phone=body.phone, code=body.otp, ip_address=ip, user_agent=ua
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh_token(body: RefreshRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.refresh(
        refresh_token=body.refresh_token, ip_address=ip, user_agent=ua
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post("/logout")
async def logout(body: LogoutRequest, db: DBSession, current_user: CurrentUser):
    await AuthService(db).logout(body.refresh_token, user_id=current_user.id)
    return ok("Logged out successfully")


@router.get("/me", response_model=UserOut)
async def me(current_user: CurrentUser):
    return current_user


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    if body.current_password and body.current_password == body.new_password:
        raise BadRequestException("New password must differ from the current password")
    await AuthService(db).change_password(
        current_user,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    return ok("Password updated successfully")


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request, db: DBSession):
    """Reset a password using only a phone number.

    DEV/TEST ONLY: no OTP, SMS, email, or ownership verification is
    performed intentionally. Anyone who knows a phone number could reset
    that account's password. For development/testing only — NOT suitable
    for production.
    """
    ip, ua = _request_context(request)
    await AuthService(db).forgot_password(
        phone_number=body.phone_number,
        new_password=body.new_password,
        ip_address=ip,
        user_agent=ua,
    )
    return ok("Password updated successfully")


@router.post("/reset-password")
async def reset_password(
    body: ResetPasswordRequest, request: Request, db: DBSession
):
    ip, ua = _request_context(request)
    await AuthService(db).reset_password(
        phone_number=body.phone_number,
        password=body.password,
        confirm_password=body.confirm_password,
        id_token=body.id_token,
        ip_address=ip,
        user_agent=ua,
    )
    return ok("पासवर्ड सफलतापूर्वक बदला गया")
