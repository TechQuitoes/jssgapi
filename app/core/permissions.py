from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.constants import RoleName, resolve_role_name
from app.core.exceptions import ForbiddenException

if TYPE_CHECKING:
    from app.models.user import User

ROLE_HIERARCHY: dict[RoleName, int] = {
    RoleName.MEMBER: 1,
    RoleName.GROUP_ADMIN: 2,
    RoleName.SUPER_ADMIN: 3,
}


def get_user_role_level(user: User) -> int:
    if not user.role:
        return 0
    return ROLE_HIERARCHY.get(resolve_role_name(user.role.name) or RoleName.MEMBER, 0)


def check_role_level(user: User, minimum_role: RoleName) -> None:
    min_level = ROLE_HIERARCHY[minimum_role]
    user_level = get_user_role_level(user)
    if user_level < min_level:
        raise ForbiddenException(f"Requires {minimum_role.value} role or higher")


def require_role(*allowed_roles: RoleName):
    def _check(user: User) -> None:
        if not user.role:
            raise ForbiddenException("No role assigned")
        if resolve_role_name(user.role.name) not in allowed_roles:
            role_names = " or ".join(r.value for r in allowed_roles)
            raise ForbiddenException(f"Requires one of: {role_names}")

    return _check


class Permission:
    MEMBER_READ = "member.read"
    MEMBER_CREATE = "member.create"
    MEMBER_UPDATE = "member.update"
    MEMBER_DELETE = "member.delete"
    MEMBER_APPROVE = "member.approve"

    GROUP_READ = "group.read"
    GROUP_CREATE = "group.create"
    GROUP_UPDATE = "group.update"
    GROUP_DELETE = "group.delete"
    # Attaching and detaching members is membership management, not a group
    # settings edit, so it is gated separately from group.update.
    GROUP_MEMBER_MANAGE = "group.member_manage"

    TRUSTEE_READ = "trustee.read"
    TRUSTEE_CREATE = "trustee.create"
    TRUSTEE_UPDATE = "trustee.update"
    TRUSTEE_DELETE = "trustee.delete"

    LOCATION_READ = "location.read"
    LOCATION_CREATE = "location.create"
    LOCATION_UPDATE = "location.update"
    LOCATION_DELETE = "location.delete"

    EVENT_READ = "event.read"
    EVENT_CREATE = "event.create"
    EVENT_UPDATE = "event.update"
    EVENT_DELETE = "event.delete"

    JAIN_TIRTH_READ = "jain_tirth.read"
    JAIN_TIRTH_CREATE = "jain_tirth.create"
    JAIN_TIRTH_UPDATE = "jain_tirth.update"
    JAIN_TIRTH_DELETE = "jain_tirth.delete"

    NEWS_READ = "news.read"
    NEWS_CREATE = "news.create"
    NEWS_PUBLISH = "news.publish"
    NEWS_UPDATE = "news.update"
    NEWS_DELETE = "news.delete"

    NOTIFICATION_READ = "notification.read"
    NOTIFICATION_SEND = "notification.send"

    ADVERTISEMENT_READ = "advertisement.read"
    ADVERTISEMENT_CREATE = "advertisement.create"
    ADVERTISEMENT_APPROVE = "advertisement.approve"
    ADVERTISEMENT_PRICING_MANAGE = "advertisement.pricing_manage"

    MEDIA_READ = "media.read"
    MEDIA_UPLOAD = "media.upload"
    MEDIA_DELETE = "media.delete"
    MEDIA_MIGRATE = "media.migrate"

    PAYMENT_READ = "payment.read"
    PAYMENT_MANAGE = "payment.manage"

    FEE_READ = "fee.read"
    FEE_MANAGE = "fee.manage"

    REPORT_VIEW = "report.view"

    AUDIT_READ = "audit.read"

    USER_READ = "user.read"
    USER_MANAGE = "user.manage"
    ROLE_MANAGE = "role.manage"


ROLE_PERMISSIONS: dict[RoleName, set[str]] = {
    RoleName.SUPER_ADMIN: {
        Permission.MEMBER_READ,
        Permission.MEMBER_CREATE,
        Permission.MEMBER_UPDATE,
        Permission.MEMBER_DELETE,
        Permission.MEMBER_APPROVE,
        Permission.GROUP_READ,
        Permission.GROUP_CREATE,
        Permission.GROUP_UPDATE,
        Permission.GROUP_DELETE,
        Permission.GROUP_MEMBER_MANAGE,
        Permission.TRUSTEE_READ,
        Permission.TRUSTEE_CREATE,
        Permission.TRUSTEE_UPDATE,
        Permission.TRUSTEE_DELETE,
        Permission.LOCATION_READ,
        Permission.LOCATION_CREATE,
        Permission.LOCATION_UPDATE,
        Permission.LOCATION_DELETE,
        Permission.EVENT_READ,
        Permission.EVENT_CREATE,
        Permission.EVENT_UPDATE,
        Permission.EVENT_DELETE,
        Permission.JAIN_TIRTH_READ,
        Permission.JAIN_TIRTH_CREATE,
        Permission.JAIN_TIRTH_UPDATE,
        Permission.JAIN_TIRTH_DELETE,
        Permission.NEWS_READ,
        Permission.NEWS_CREATE,
        Permission.NEWS_PUBLISH,
        Permission.NEWS_UPDATE,
        Permission.NEWS_DELETE,
        Permission.NOTIFICATION_READ,
        Permission.NOTIFICATION_SEND,
        Permission.ADVERTISEMENT_READ,
        Permission.ADVERTISEMENT_CREATE,
        Permission.ADVERTISEMENT_APPROVE,
        Permission.ADVERTISEMENT_PRICING_MANAGE,
        Permission.MEDIA_READ,
        Permission.MEDIA_UPLOAD,
        Permission.MEDIA_DELETE,
        Permission.MEDIA_MIGRATE,
        Permission.PAYMENT_READ,
        Permission.PAYMENT_MANAGE,
        Permission.FEE_READ,
        Permission.FEE_MANAGE,
        Permission.REPORT_VIEW,
        Permission.AUDIT_READ,
        Permission.USER_READ,
        Permission.USER_MANAGE,
        Permission.ROLE_MANAGE,
    },
    # A GROUP_ADMIN runs one social group. Group scoping is enforced separately by
    # `app/core/scope.py` (the group comes from the authenticated User), so every
    # permission here is a "may attempt this", not "may do this anywhere".
    RoleName.GROUP_ADMIN: {
        Permission.MEMBER_READ,
        Permission.MEMBER_CREATE,
        Permission.MEMBER_UPDATE,
        Permission.MEMBER_APPROVE,
        Permission.GROUP_READ,
        Permission.GROUP_MEMBER_MANAGE,
        Permission.TRUSTEE_READ,
        Permission.TRUSTEE_CREATE,
        Permission.TRUSTEE_UPDATE,
        Permission.TRUSTEE_DELETE,
        Permission.LOCATION_READ,
        Permission.EVENT_READ,
        Permission.EVENT_CREATE,
        Permission.EVENT_UPDATE,
        Permission.JAIN_TIRTH_READ,
        Permission.JAIN_TIRTH_CREATE,
        Permission.JAIN_TIRTH_UPDATE,
        Permission.NEWS_READ,
        Permission.NEWS_CREATE,
        Permission.NOTIFICATION_READ,
        Permission.ADVERTISEMENT_READ,
        Permission.ADVERTISEMENT_CREATE,
        Permission.MEDIA_READ,
        Permission.MEDIA_UPLOAD,
        Permission.PAYMENT_READ,
        Permission.FEE_READ,
    },
    RoleName.MEMBER: {
        Permission.MEMBER_READ,
        Permission.GROUP_READ,
        Permission.TRUSTEE_READ,
        Permission.LOCATION_READ,
        Permission.EVENT_READ,
        Permission.JAIN_TIRTH_READ,
        Permission.NEWS_READ,
        Permission.NOTIFICATION_READ,
        Permission.ADVERTISEMENT_READ,
        Permission.MEDIA_READ,
    },
}


def check_permission(user: User, permission: str) -> bool:
    if not user.role:
        return False
    role = resolve_role_name(user.role.name)
    if role is None:
        return False
    return permission in ROLE_PERMISSIONS.get(role, set())


def require_permission(user: User, permission: str) -> None:
    if not check_permission(user, permission):
        raise ForbiddenException(f"Missing permission: {permission}")
