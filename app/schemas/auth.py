from __future__ import annotations

from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.core.exceptions import BadRequestException

from app.schemas.user import UserOut
from app.utils.validators import (
    normalize_phone_number,
    validate_email,
    validate_password,
    validate_phone,
)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    phone_number: str = Field(pattern=r"^[6-9][0-9]{9}$")
    confirm_password: str = Field(min_length=8, max_length=128)
    group_id: UUID | None = None
    group_name: str | None = None
    spouse_mobile: str | None = Field(
        default=None,
        validation_alias=AliasChoices("spouse_mobile", "spouse_phone"),
    )
    id_token: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return value

    @field_validator("spouse_mobile", mode="before")
    @classmethod
    def _normalize_spouse_mobile(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return value

    @field_validator("group_name", mode="before")
    @classmethod
    def _normalize_group_name(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return value

    @model_validator(mode="after")
    def _validate_registration_fields(self) -> RegisterRequest:
        if not self.full_name.strip():
            raise ValueError("Full name cannot be empty")
        if self.email is not None and not validate_email(self.email):
            raise ValueError("Invalid email address")
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        if self.spouse_mobile is not None:
            if not validate_phone(self.spouse_mobile):
                raise ValueError("Invalid spouse phone number")
            norm_p = self.phone_number.strip().removeprefix("+91")
            norm_s = self.spouse_mobile.strip().removeprefix("+91")
            if norm_p == norm_s:
                raise ValueError(
                    "Member mobile and spouse mobile cannot be the same number"
                )
        return self


class LoginRequest(BaseModel):
    email: str | None = None
    phone_number: str | None = None
    password: str | None = None
    id_token: str | None = None

    def model_post_init(self, __context) -> None:
        if self.id_token:
            return
        if not self.email and not self.phone_number:
            raise ValueError("Either email, phone_number, or id_token is required")
        if self.password is None:
            raise ValueError("Password is required without an id_token")
        if self.email and not validate_email(self.email):
            raise ValueError("Invalid email address")
        if self.phone_number and not validate_phone(self.phone_number):
            raise ValueError("Invalid phone number")


class OtpLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(min_length=1, max_length=4096)

    @field_validator("id_token", mode="before")
    @classmethod
    def _normalize_id_token(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("id_token is required")
        return value


class ForgotPasswordRequest(BaseModel):
    phone_number: str
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    def model_post_init(self, __context) -> None:
        if not validate_phone(self.phone_number):
            raise ValueError("Invalid phone number")
        validate_password(self.new_password)
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_number: str = Field(min_length=1, max_length=20)
    password: str = Field(max_length=128)
    confirm_password: str = Field(max_length=128)
    id_token: str = Field(min_length=1, max_length=4096)

    @field_validator("phone_number", "id_token", mode="before")
    @classmethod
    def _normalize_strings(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Value is required")
        return value


class OtpRequest(BaseModel):
    phone: str

    def model_post_init(self, __context) -> None:
        self.phone = normalize_phone_number(self.phone)


class SendRegisterOtpResponse(BaseModel):
    req_id: str
    expires_in: int = 300


class OtpVerifyRequest(BaseModel):
    phone: str
    otp: str

    def model_post_init(self, __context) -> None:
        self.phone = normalize_phone_number(self.phone)
        if not self.otp.isdigit() or len(self.otp) != 6:
            raise ValueError("OTP must be a 6-digit number")


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    new_password: str = Field(
        validation_alias=AliasChoices("new_password", "password"),
        max_length=128,
    )
    confirm_password: str | None = None
    current_password: str | None = None

    def model_post_init(self, __context) -> None:
        if len(self.new_password) < 6:
            raise BadRequestException(
                "Passwords do not match or minimum length requirement not met"
            )
        if self.confirm_password is not None and self.confirm_password != self.new_password:
            raise BadRequestException(
                "Passwords do not match or minimum length requirement not met"
            )


class AuthToken(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class OAuth2TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user: UserOut
    tokens: AuthToken | None = None
    message: str | None = None


class RegisterResponse(AuthResponse):
    pass
