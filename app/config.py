from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# libpq/psycopg2-only connection-string query params. SQLAlchemy's asyncpg
# dialect forwards every URL query param as an asyncpg.connect() keyword
# argument, and asyncpg rejects these. asyncpg uses its own ``ssl`` setting
# (default "prefer", which negotiates TLS with Neon) and handles channel
# binding itself, so these can safely be dropped.
_LIBPQ_ONLY_QUERY_PARAMS = {"sslmode", "channel_binding", "pgbouncer"}

FIREBASE_PROJECT_ID = "jssg-7e2b0"


def _async_database_url(url: str) -> str:
    """Force the SQLAlchemy async engine to use an async driver.

    Neon/Vercel often provide a plain ``postgresql://`` (or legacy
    ``postgres://`` / ``postgresql+psycopg2://``) URL. ``create_async_engine``
    maps that scheme to the default sync driver (psycopg2), which fails with
    "The asyncio extension requires an async driver". Rewrite the scheme to
    ``postgresql+asyncpg://`` unless an async driver is already selected.
    """
    url = url.strip()
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    elif url.startswith("postgresql+asyncpg://"):
        pass
    else:
        return url

    parts = urlsplit(url)
    if parts.query:
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query)
            if key not in _LIBPQ_ONLY_QUERY_PARAMS
        ]
        url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
        )
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "JSSG API"
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    auto_bootstrap_on_startup: bool = True

    # Database
    database_url: str = "postgresql+asyncpg://jssg:jssg_secret@localhost:5432/jssg_db"
    database_echo: bool = False

    @field_validator("database_url", mode="before")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        return _async_database_url(value)

    # JWT
    jwt_secret: str = "CHANGE-ME-IN-PRODUCTION"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = True
    redis_provider: Literal["local", "redis", "upstash"] = "local"
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: str | None = None

    # OTP login
    otp_length: int = 6
    otp_expire_seconds: int = 300
    otp_max_attempts: int = 5
    otp_max_per_phone_per_hour: int = 5

    # SMS
    sms_provider: str = ""
    sms_api_key: str = ""
    sms_sender_id: str = ""
    sms_template_id: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # MSG91 OTP
    msg91_auth_key: str = ""
    msg91_otp_template_id: str = ""
    msg91_otp_api_url: str = "https://control.msg91.com/api/v5/otp"
    msg91_otp_verify_url: str = "https://control.msg91.com/api/v5/otp/verify"

    # MSG91 OTP Widget (fallback)
    msg91_widget_id: str = ""
    msg91_widget_token: str = ""
    msg91_verify_url: str = (
        "https://control.msg91.com/api/v5/widget/verifyAccessToken"
    )
    msg91_send_otp_url: str = (
        "https://control.msg91.com/api/v5/widget/sendOtpMobile"
    )
    msg91_verify_otp_url: str = "https://control.msg91.com/api/v5/widget/verifyOtp"
    msg91_timeout_seconds: float = 10.0

    # Cloudflare R2
    # ``cloudflare_r2_endpoint`` must be the S3 API host
    # ``https://<ACCOUNT_ID>.r2.cloudflarestorage.com``. The public
    # ``r2.dev`` domain and custom public domains are read-only HTTP and are
    # what belongs in ``cloudflare_r2_public_url``.
    cloudflare_r2_endpoint: str = ""
    cloudflare_r2_access_key: str = ""
    cloudflare_r2_secret_key: str = ""
    cloudflare_r2_bucket: str = ""
    cloudflare_r2_public_url: str = ""

    @field_validator(
        "cloudflare_r2_endpoint",
        "cloudflare_r2_access_key",
        "cloudflare_r2_secret_key",
        "cloudflare_r2_bucket",
        "cloudflare_r2_public_url",
        mode="before",
    )
    @classmethod
    def _normalise_cloudflare_r2(cls, value: object) -> object:
        """Trim whitespace and one layer of quoting from the R2 settings.

        SigV4 signs the exact bytes of the secret, so a value pasted out of the
        Cloudflare dashboard with a trailing newline - or wrapped in quotes that
        became part of the value - is a valid-looking setting that fails every
        request with ``SignatureDoesNotMatch``. Normalising here keeps that
        class of mistake out of the signing path entirely.
        """
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
            cleaned = cleaned[1:-1].strip()
        return cleaned

    # Which backend stores uploaded media: "auto" (R2 when configured,
    # otherwise Postgres), "r2", "database", or "local". Postgres is the
    # default fallback because it is the only backend that survives on
    # serverless hosts, where local disk is per-instance and ephemeral.
    media_storage_backend: Literal["auto", "r2", "database", "local"] = "auto"

    # Absolute base used to build media URLs when the request base is not
    # available (e.g. background jobs). Leave empty to derive it per request.
    public_base_url: str = ""

    # The timezone the platform's wall-clock times are expressed in. Event
    # dates and times are stored as local wall-clock values; they are turned
    # into instants (and back) through this zone, so every "today" and
    # "within a week" judgement means the same thing for every reader.
    app_timezone: str = "Asia/Kolkata"

    # Firebase
    firebase_project_id: str = FIREBASE_PROJECT_ID
    firebase_private_key: str = ""
    firebase_client_email: str = ""

    @field_validator("firebase_project_id", mode="before")
    @classmethod
    def _pin_firebase_project_id(cls, value: object) -> str:
        return FIREBASE_PROJECT_ID

    # Google Sheets (registration import).
    # Credentials are never stored in the repository: either inline the service
    # account key in GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON (single line, ``\n``
    # escaped newlines are accepted) or point GOOGLE_APPLICATION_CREDENTIALS
    # at the key file that lives outside the repo.
    google_application_credentials: str = ""
    google_sheets_spreadsheet_id: str = ""
    google_sheets_sheet_name: str = ""
    google_sheets_range: str = ""
    google_sheets_header_row: int = 1
    google_sheets_data_start_row: int = 2
    google_sheets_service_account_json: str = ""
    google_sheets_sync_enabled: bool = False
    # Google Cloud Translation
    google_cloud_project: str = ""
    google_service_account_json: str = ""
    google_translate_enabled: bool = True

    # Translation provider selection: "azure" or "google"
    # Set TRANSLATION_PROVIDER=azure to use Microsoft Azure Translator (Free F0 tier).
    # Falls back to Google Cloud Translation when set to "google" or left empty.
    translation_provider: Literal["azure", "google"] = "google"

    # Microsoft Azure Translator (Free F0: 2M characters/month, no charge)
    azure_translator_key: str = ""
    azure_translator_region: str = ""
    azure_translator_endpoint: str = "https://api.cognitive.microsofttranslator.com"


    # Payment
    payment_provider: str = ""
    payment_key: str = ""
    payment_secret: str = ""
    payment_webhook_secret: str = ""

    # Razorpay
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    group_member_rate: float = 100.0

    # CORS
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
