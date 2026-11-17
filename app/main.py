from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.api.router import api_router
from app.config import get_settings
from app.core.exceptions import AppException
from app.core.redis import redis_client
from app.core.responses import error_response
from app.database import async_session_factory
from app.services.bootstrap_service import bootstrap_app
from app.services.media_storage import validate_storage_configuration

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reported before anything else so a bad Cloudflare R2 setting is visible at
    # boot instead of only as a signature error on the first media read.
    logger.info("media storage backend: %s", validate_storage_configuration())
    await redis_client.init(
        redis_url=settings.redis_url,
        enabled=settings.redis_enabled,
        provider=settings.redis_provider,
        upstash_url=settings.upstash_redis_rest_url,
        upstash_token=settings.upstash_redis_rest_token,
    )
    if settings.auto_bootstrap_on_startup:
        await bootstrap_app(async_session_factory)
    else:
        logger.info("Startup bootstrap skipped (AUTO_BOOTSTRAP_ON_STARTUP=false)")
    try:
        yield
    finally:
        await redis_client.close()


app = FastAPI(
    title=settings.app_name,
    description=(
        "JSSG - Centralized Jain Community Digital Platform. "
        "REST API consumed by the Flutter mobile app and the web admin panel."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def parse_integrity_error(exc: IntegrityError) -> str:
    """Extract a user-friendly error message from an IntegrityError.

    Ensures raw SQL queries, stack traces, and database internals are never exposed.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint_name = (
        getattr(diag, "constraint_name", None)
        or getattr(orig, "constraint_name", None)
        or ""
    )
    detail = (
        getattr(diag, "message_detail", None)
        or getattr(orig, "detail", None)
        or ""
    )
    raw_info = f"{constraint_name} {detail} {str(exc)}".lower()

    if "phone" in raw_info:
        return "This phone number is already registered."
    if "email" in raw_info:
        return "This email is already registered."
    if "membership_number" in raw_info:
        return "This membership number is already registered."
    if "uq_members_user_id" in raw_info or "members_user_id" in raw_info:
        return "A member profile already exists for this user."
    if "foreign key" in raw_info or "is not present in table" in raw_info:
        return "A referenced record does not exist."
    if "unique" in raw_info or "duplicate key" in raw_info:
        return "A record with this information already exists."
    return "A database integrity error occurred. Please check your data."


def _normalize_conflict_message(msg: str) -> str:
    msg_lower = msg.lower()
    if ("phone" in msg_lower or "mobile" in msg_lower) and (
        "already" in msg_lower or "registered" in msg_lower or "exist" in msg_lower
    ):
        return "This phone number is already registered."
    if "email" in msg_lower and (
        "already" in msg_lower or "registered" in msg_lower or "exist" in msg_lower
    ):
        return "This email is already registered."
    return msg


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    session = getattr(request.state, "db", None)
    if session is not None:
        try:
            await session.rollback()
        except Exception:
            pass
    message = parse_integrity_error(exc)
    logger.warning("Database IntegrityError: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=error_response(
            message=message,
            error_code="CONFLICT",
            status_code=status.HTTP_409_CONFLICT,
        )["content"],
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    messages: list[str] = []
    for err in errors:
        loc = err.get("loc", [])
        field_parts = [str(x) for x in loc if x not in ("body", "query", "path")]
        field_name = ".".join(field_parts)
        msg = err.get("msg", "Invalid value")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        normalized = _normalize_conflict_message(msg)
        if normalized in (
            "This phone number is already registered.",
            "This email is already registered.",
        ):
            messages.append(normalized)
        elif field_name:
            messages.append(f"{field_name}: {msg}")
        else:
            messages.append(msg)
    user_msg = "; ".join(messages) if messages else "Validation failed"
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "message": user_msg,
            "detail": jsonable_encoder(errors, custom_encoder={Exception: str}),
            "error_code": "VALIDATION_ERROR",
        },
    )


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    msg = _normalize_conflict_message(str(exc.detail))
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            message=msg,
            error_code=exc.error_code,
            status_code=exc.status_code,
        )["content"],
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    msg = _normalize_conflict_message(msg)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            message=msg,
            error_code=getattr(exc, "error_code", None),
            status_code=exc.status_code,
        )["content"],
    )


@app.get("/", include_in_schema=False)
async def root():
    return {"app": settings.app_name, "message": "JSSG API is running"}


@app.get("/health", tags=["health"])
async def health_check():
    return {
        "success": True,
        "message": "Healthy",
        "data": {
            "app": settings.app_name,
            "environment": settings.app_env,
        },
    }


app.include_router(api_router, prefix="/api")
