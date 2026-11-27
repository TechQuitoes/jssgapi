"""Image compression and thumbnail generation.

Every image the API stores passes through this module on upload. The two
operations are:

1. **Optimise** – re-encode the full-size image to WebP at a quality that
   preserves visible detail while cutting the file size by 70-90 %.
2. **Thumbnail** – produce a small square-ish preview (200 × 200 by default)
   for list views and cards.

Both operations are synchronous because the storage layer (boto3 / Postgres)
is also synchronous, and the typical input is a phone camera photo (3-8 MB)
whose decode + encode takes < 200 ms on commodity hardware.

Non-image files and GIFs (which lose their animation in a still encode) are
returned unchanged.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: MIME types that Pillow can usefully re-encode to WebP.  GIF is excluded
#: because a still encode would lose the animation, and HEIC support depends
#: on a system library that is not always present.
_OPTIMISABLE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

#: Quality setting for the full-size WebP encode.  80 is visually transparent
#: for photographic content and 85-90 % smaller than the JPEG or PNG input.
WEBP_QUALITY = 80

#: Thumbnail dimensions (width, height).
THUMB_SIZE = (200, 200)

#: Quality for the thumbnail WebP encode.  Thumbnails are small so even
#: a moderate quality gives tiny files (10-30 KB).
THUMB_QUALITY = 75

#: Maximum dimension (width or height) for the optimised full-size image.
#: Images larger than this are resized proportionally.  1200 px covers a
#: typical phone screen at 2× density.
MAX_FULL_SIZE_DIMENSION = 1200


@dataclass(frozen=True)
class OptimisedImage:
    """The result of optimising a single upload."""

    content: bytes
    mime_type: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class ImageWithThumb:
    """Full-size image and its thumbnail."""

    full: OptimisedImage
    thumb: OptimisedImage | None


def _can_optimise(mime_type: str) -> bool:
    return mime_type in _OPTIMISABLE_TYPES


def optimise_image(
    content: bytes,
    mime_type: str,
    *,
    max_dimension: int = MAX_FULL_SIZE_DIMENSION,
    quality: int = WEBP_QUALITY,
) -> OptimisedImage:
    """Compress an image to WebP and cap its dimensions.

    Returns the original bytes unchanged (with ``mime_type`` preserved) when
    the input is not a type Pillow can usefully re-encode, or when the
    encode fails for any reason.
    """
    if not _can_optimise(mime_type):
        return OptimisedImage(
            content=content, mime_type=mime_type, width=None, height=None
        )

    try:
        from PIL import Image

        img = Image.open(io.BytesIO(content))
        # Convert palette/RGBA to RGB for WebP compatibility with smaller
        # file sizes.  Transparency is not needed for photos.
        if img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Respect EXIF orientation so the stored image is upright.
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)

        # Resize if larger than the cap.
        w, h = img.size
        if max(w, h) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=4)
        optimised = buf.getvalue()

        # Only use the WebP version if it is actually smaller.
        if len(optimised) < len(content):
            return OptimisedImage(
                content=optimised,
                mime_type="image/webp",
                width=img.size[0],
                height=img.size[1],
            )

        # The original was already well-compressed; keep it but record dims.
        return OptimisedImage(
            content=content,
            mime_type=mime_type,
            width=img.size[0],
            height=img.size[1],
        )

    except Exception:
        logger.warning("Image optimisation failed; keeping original", exc_info=True)
        return OptimisedImage(
            content=content, mime_type=mime_type, width=None, height=None
        )


def generate_thumbnail(
    content: bytes,
    mime_type: str,
    *,
    size: tuple[int, int] = THUMB_SIZE,
    quality: int = THUMB_QUALITY,
) -> OptimisedImage | None:
    """Generate a small WebP thumbnail.

    Returns ``None`` when the input is not a type that can be thumbnailed,
    or when generation fails.
    """
    if not _can_optimise(mime_type):
        return None

    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(content))

        if img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img = ImageOps.exif_transpose(img)

        # ``thumbnail`` preserves aspect ratio and never upscales.
        img.thumbnail(size, Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=4)

        return OptimisedImage(
            content=buf.getvalue(),
            mime_type="image/webp",
            width=img.size[0],
            height=img.size[1],
        )

    except Exception:
        logger.warning("Thumbnail generation failed", exc_info=True)
        return None


def process_image_upload(
    content: bytes,
    mime_type: str,
) -> ImageWithThumb:
    """Optimise an uploaded image and generate its thumbnail.

    This is the main entry point that :class:`MediaService` calls on every
    image upload.
    """
    full = optimise_image(content, mime_type)
    thumb = generate_thumbnail(content, mime_type)
    return ImageWithThumb(full=full, thumb=thumb)
