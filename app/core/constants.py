from __future__ import annotations

from enum import Enum


class RoleName(str, Enum):
    """The three roles the platform recognises.

    ``SUPER_ADMIN`` is the only platform-wide administrator (React Admin Panel).
    ``GROUP_ADMIN`` administers exactly one social group (Flutter admin app).
    ``MEMBER`` is the community member (Flutter member app).
    """

    SUPER_ADMIN = "SUPER_ADMIN"
    GROUP_ADMIN = "GROUP_ADMIN"
    MEMBER = "MEMBER"


DEFAULT_USER_PASSWORD = "Pass@1234"


# Canonical lookup: the three RoleName values are the ONLY roles the system recognises.
# The `roles` table is populated from external sources (Excel / Google Sheet imports),
# which have historically stored whitespace-polluted labels such as "Admin\n". Those
# rows matched no ROLE_PERMISSIONS key, so legitimate admins silently lost every
# permission.
# Resolving the stored label back to the canonical enum keeps exactly one role identity
# per name instead of loosening authorization: "admin" can only ever resolve to ADMIN.
_CANONICAL_ROLES: dict[str, RoleName] = {
    role.value.casefold(): role for role in RoleName
}

# Retired role labels. The platform is collapsing its six-tier admin hierarchy to
# SUPER_ADMIN / GROUP_ADMIN / MEMBER. These labels are still accepted on input and
# still resolve during the transition so a stale `roles` row or an old token cannot
# lock an administrator out, but each one collapses onto a single surviving role
# rather than keeping a tier alive:
#
# * ADMIN / FEDERATION_ADMIN were platform-wide administrators -> SUPER_ADMIN.
# * REGIONAL_ADMIN was group-scoped, exactly like GROUP_ADMIN -> GROUP_ADMIN.
#
# This is deliberately a narrowing of what those labels can reach: FEDERATION_ADMIN
# was already missing USER_MANAGE / ROLE_MANAGE, so a former ADMIN holder resolving
# to SUPER_ADMIN is the only mapping that does not silently reduce an administrator's
# reach over production data.
RETIRED_ROLE_ALIASES: dict[str, RoleName] = {
    "admin": RoleName.SUPER_ADMIN,
    "federation_admin": RoleName.SUPER_ADMIN,
    "regional_admin": RoleName.GROUP_ADMIN,
}

# The same mapping keyed by the uppercase label as stored in ``roles.name``, for
# the bootstrap service and migrations that work on the raw column.
LEGACY_ROLE_ALIASES: dict[str, RoleName] = {
    label.upper(): role for label, role in RETIRED_ROLE_ALIASES.items()
}

_ROLE_LOOKUP: dict[str, RoleName] = {**_CANONICAL_ROLES, **RETIRED_ROLE_ALIASES}


def resolve_role_name(raw: str | None) -> RoleName | None:
    """Map a stored/incoming role label onto its canonical RoleName, or None if unknown.

    Accepts whitespace-polluted and retired labels; see ``RETIRED_ROLE_ALIASES``.
    """
    if raw is None:
        return None
    return _ROLE_LOOKUP.get(str(raw).strip().casefold())


def is_canonical_role_label(raw: str | None) -> bool:
    """True only for a label that is already one of the three surviving roles."""
    if raw is None:
        return False
    return str(raw).strip().casefold() in _CANONICAL_ROLES


class MemberStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    INACTIVE = "INACTIVE"


class EventStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class EventType(str, Enum):
    """The kind of programme an event is, chosen by the host.

    ``OTHER`` is the escape hatch: it must be paired with a free-text
    ``event_type_other`` so the label is never lost.
    """

    ANNUAL = "ANNUAL"
    MONTHLY_PROGRAM = "MONTHLY_PROGRAM"
    CULTURAL_PROGRAM = "CULTURAL_PROGRAM"
    RELIGIOUS_PROGRAM = "RELIGIOUS_PROGRAM"
    SOCIAL_PROGRAM = "SOCIAL_PROGRAM"
    SPORTS_PROGRAM = "SPORTS_PROGRAM"
    EDUCATIONAL_PROGRAM = "EDUCATIONAL_PROGRAM"
    NATIONAL_DAY_PROGRAM = "NATIONAL_DAY_PROGRAM"
    FUNDRAISER = "FUNDRAISER"
    OTHER = "OTHER"


class EventTimeFrame(str, Enum):
    """Where an event sits relative to now, derived from its start moment.

    ``UPCOMING`` is the next seven days, ``FUTURE`` is everything beyond that
    and ``PAST`` is everything already started. It is never stored: it is
    recomputed on every read so it can never go stale.
    """

    PAST = "PAST"
    UPCOMING = "UPCOMING"
    FUTURE = "FUTURE"


class NewsStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    SCHEDULED = "SCHEDULED"


class AdvertisementStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class MediaType(str, Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"


class AuditAction(str, Enum):
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    REGISTER = "REGISTER"
    PASSWORD_CHANGE = "PASSWORD_CHANGE"
    OTP_REQUEST = "OTP_REQUEST"
    OTP_VERIFY = "OTP_VERIFY"
    OTP_FAILED = "OTP_FAILED"
    MEMBER_CREATE = "MEMBER_CREATE"
    MEMBER_UPDATE = "MEMBER_UPDATE"
    MEMBER_APPROVE = "MEMBER_APPROVE"
    MEMBER_REJECT = "MEMBER_REJECT"
    MEMBER_BLOCK = "MEMBER_BLOCK"
    GROUP_CREATE = "GROUP_CREATE"
    GROUP_UPDATE = "GROUP_UPDATE"
    GROUP_DELETE = "GROUP_DELETE"
    EVENT_CREATE = "EVENT_CREATE"
    EVENT_UPDATE = "EVENT_UPDATE"
    EVENT_DELETE = "EVENT_DELETE"
    EVENT_IMAGE_UPLOAD = "EVENT_IMAGE_UPLOAD"
    JAIN_TIRTH_CREATE = "JAIN_TIRTH_CREATE"
    JAIN_TIRTH_UPDATE = "JAIN_TIRTH_UPDATE"
    JAIN_TIRTH_DELETE = "JAIN_TIRTH_DELETE"
    JAIN_TIRTH_IMAGE_UPLOAD = "JAIN_TIRTH_IMAGE_UPLOAD"
    NEWS_CREATE = "NEWS_CREATE"
    NEWS_PUBLISH = "NEWS_PUBLISH"
    ADVERTISEMENT_CREATE = "ADVERTISEMENT_CREATE"
    ADVERTISEMENT_UPDATE = "ADVERTISEMENT_UPDATE"
    ADVERTISEMENT_DELETE = "ADVERTISEMENT_DELETE"
    ADVERTISEMENT_APPROVE = "ADVERTISEMENT_APPROVE"
    ADVERTISEMENT_IMAGE_UPLOAD = "ADVERTISEMENT_IMAGE_UPLOAD"
    ADVERTISEMENT_PRICING_UPDATE = "ADVERTISEMENT_PRICING_UPDATE"
    PAYMENT_CREATE = "PAYMENT_CREATE"
    PAYMENT_VERIFY = "PAYMENT_VERIFY"
    ROLE_CHANGE = "ROLE_CHANGE"
    MEDIA_UPLOAD = "MEDIA_UPLOAD"
    MEDIA_DELETE = "MEDIA_DELETE"
    MEDIA_MIGRATE = "MEDIA_MIGRATE"


class FeeStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    WAIVED = "WAIVED"
