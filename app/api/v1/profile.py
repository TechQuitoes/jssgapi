from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from app.core.exceptions import BadRequestException, ValidationException
from app.core.redis import invalidate_member_cache
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession
from app.schemas.profile import ProfilePhotoResponse, ProfileResponse, ProfileUpdate
from app.services.profile_service import ProfileService

router = APIRouter(prefix="/profile", tags=["Profile"])

PROFILE_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
PROFILE_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PROFILE_PHOTO_MAX_SIZE = 5 * 1024 * 1024


def _safe_photo_filename(filename: str | None) -> str:
    raw_name = (filename or "profile-photo").replace("\\", "/")
    name = Path(raw_name).name or "profile-photo"
    if len(name) <= 255:
        return name
    suffix = Path(name).suffix
    return f"{name[: 255 - len(suffix)]}{suffix}"


@router.get("", response_model=ProfileResponse)
async def get_profile(current_user: CurrentUser, db: DBSession):
    profile = await ProfileService(db).get_profile(current_user.id)
    profile_dump = profile.model_dump(mode="json")
    return {
        "success": True,
        "profile": profile_dump,
        "data": profile_dump,
    }


@router.put("")
async def update_profile(
    body: ProfileUpdate, current_user: CurrentUser, db: DBSession
):
    await ProfileService(db).update_profile(
        current_user.id,
        body.model_dump(exclude_unset=True),
    )
    await invalidate_member_cache()
    return ok("Profile updated successfully")


@router.post("/photo", response_model=ProfilePhotoResponse)
async def upload_profile_photo(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    photo: UploadFile = File(...),
):
    content = await photo.read(PROFILE_PHOTO_MAX_SIZE + 1)
    mime_type = photo.content_type or ""
    file_name = _safe_photo_filename(photo.filename)
    suffix = Path(file_name).suffix.lower()
    if (
        mime_type not in PROFILE_PHOTO_MIME_TYPES
        or suffix not in PROFILE_PHOTO_EXTENSIONS
        or not content
        or len(content) > PROFILE_PHOTO_MAX_SIZE
    ):
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "गलत फोटो फ़ाइल"},
        )

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    try:
        photo_link = await ProfileService(db).upload_photo(
            current_user.id,
            file_name=file_name,
            content=content,
            mime_type=mime_type,
            ip_address=ip_address,
            user_agent=user_agent,
            base_url=str(request.base_url),
        )
    except (BadRequestException, ValidationException):
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "गलत फोटो फ़ाइल"},
        )
    await invalidate_member_cache()
    return {"success": True, "photo_link": photo_link}
