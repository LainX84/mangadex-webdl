"""Image validation without modifying or re-encoding the image bytes."""

from __future__ import annotations

import hashlib
import io
import re
import warnings

from PIL import Image

from app.mangadex.errors import InvalidImageError

Image.MAX_IMAGE_PIXELS = 300_000_000  # tall webtoon strips are legitimately huge

_FORMAT_EXT = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "AVIF": "avif", "BMP": "bmp"}

# MangaDex page file names carry the SHA-256 of the file: "35-40d98604…73dc.jpg".
_FILENAME_SHA256 = re.compile(r"(?:^|[-_])([0-9a-f]{64})(?:\.[A-Za-z0-9]+)?$")


def sniff_extension(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return None


def extension_from_filename(name: str) -> str | None:
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[1].lower()
    return "jpg" if ext == "jpeg" else ext


def sha256_from_filename(name: str | None) -> str | None:
    """The SHA-256 embedded in a MangaDex page file name, if there is one."""
    if not name:
        return None
    m = _FILENAME_SHA256.search(name.rsplit("/", 1)[-1].lower())
    return m.group(1) if m else None


def _looks_complete(data: bytes, ext: str) -> bool:
    if ext == "jpg":
        # EOI marker should be at (or very near) the end; tolerate trailing padding.
        return b"\xff\xd9" in data[-64:]
    if ext == "png":
        return b"IEND" in data[-16:]
    if ext == "gif":
        return data.rstrip(b"\x00")[-1:] == b";"
    return True


def _has_samsung_trailer(data: bytes) -> bool:
    """Samsung's photo editor appends metadata after the JPEG end-of-image marker, ending in "SEFT"."""
    return data.endswith(b"SEFT") and b"\xff\xd9" in data[:-4]


def _decodes_fully(data: bytes) -> bool:
    """Decode every pixel. Pillow raises on truncated data (LOAD_TRUNCATED_IMAGES is off)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                img.load()
        return True
    except Exception:
        return False


def validate_image(
    data: bytes,
    content_type: str | None = None,
    expected_length: int | None = None,
    source_filename: str | None = None,
) -> str:
    """Validate downloaded bytes are a complete image. Returns the file extension.

    When ``source_filename`` carries a SHA-256 (MangaDex page names do) and it matches the bytes, the file is
    exactly what the uploader published, so the heuristic end-of-file check is skipped. Without a matching hash,
    JPEGs with Samsung's photo-editor trailer after the end-of-image marker are accepted when every pixel
    decodes; any other data after the marker still counts as truncated.
    """
    if not data:
        raise InvalidImageError("Empty image response")
    if expected_length is not None and expected_length != len(data):
        raise InvalidImageError(f"Incomplete image: got {len(data)} of {expected_length} bytes")
    if content_type and not content_type.lower().startswith(("image/", "application/octet-stream")):
        raise InvalidImageError(f"Unexpected content type {content_type!r}")
    ext = sniff_extension(data)
    if ext is None:
        raise InvalidImageError("Response is not a recognised image format")
    expected_sha = sha256_from_filename(source_filename)
    hash_verified = expected_sha is not None and hashlib.sha256(data).hexdigest() == expected_sha
    if not hash_verified and not _looks_complete(data, ext):
        if not (ext == "jpg" and _has_samsung_trailer(data) and _decodes_fully(data)):
            raise InvalidImageError("Image appears truncated")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
                fmt = img.format
    except InvalidImageError:
        raise
    except Exception as exc:
        if ext == "avif":
            return ext  # Pillow builds without AVIF support: rely on the signature check
        raise InvalidImageError(f"Corrupt image data: {exc.__class__.__name__}") from exc
    return _FORMAT_EXT.get(fmt or "", ext)
