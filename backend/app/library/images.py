"""Image validation without modifying the image bytes, and the optional page conversion (``convert_page``)."""

from __future__ import annotations

import hashlib
import io
import re
import warnings

from PIL import Image, ImageChops

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


# --------------------------------------------------------------------- optional page conversion

# Settings → Output format → Page images (AppSettings.page_format).
PAGE_FORMATS = ("original", "png", "jpg", "webp_lossy", "webp_lossless", "mixed")
LEGACY_PAGE_FORMATS = {"webp_lossless_png": "mixed"}  # short-lived value of the first 1.1.0 build
WEBP_MAX_DIMENSION = 16383  # WebP format limit
JPEG_MAX_DIMENSION = 65500  # JPEG format limit (65535) with a margin
MIXED_WEBP_MIN_SAVING = 0.05  # MIXED: lossless WebP only when it beats the best PNG/original by 5 %


def _open_page(data: bytes) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        img = Image.open(io.BytesIO(data))
        img.load()
    return img


def _is_animated(img: Image.Image) -> bool:
    return bool(getattr(img, "is_animated", False)) and getattr(img, "n_frames", 1) > 1


def _normalised(img: Image.Image) -> Image.Image:
    """The pixels in a mode every encoder understands (L, LA, RGB or RGBA), without changing them.

    RGB pages whose three channels are identical (grayscale scans saved in colour) become L.
    CMYK and other exotic modes are converted to RGB (the only lossy step, and only for those).
    """
    im = img
    if im.mode == "P":
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
    elif im.mode == "PA":
        im = im.convert("RGBA")
    elif im.mode == "1":
        im = im.convert("L")
    elif im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        im = im.convert("L")
    elif im.mode not in ("L", "LA", "RGB", "RGBA"):
        im = im.convert("RGB")
    if im.mode == "RGBA" and im.getchannel("A").getextrema() == (255, 255):
        im = im.convert("RGB")  # fully opaque: the alpha channel carries nothing
    if im.mode == "LA" and im.getchannel("A").getextrema() == (255, 255):
        im = im.convert("L")
    if im.mode == "RGB":
        r, g, b = im.split()
        if ImageChops.difference(r, g).getbbox() is None and ImageChops.difference(g, b).getbbox() is None:
            im = r  # grayscale stored as RGB: one channel holds all the information
    return im


def _pixels_equal(a: Image.Image, b_data: bytes) -> bool:
    """Do the encoded bytes decode to exactly the pixels of ``a``?"""
    b = _open_page(b_data)
    mode = "RGBA" if a.mode in ("RGBA", "LA") else "RGB"
    return a.size == b.size and a.convert(mode).tobytes() == b.convert(mode).tobytes()


def _encode(im: Image.Image, fmt: str, quality: int, icc: bytes | None) -> bytes:
    out = io.BytesIO()
    extra = {"icc_profile": icc} if icc else {}
    if fmt == "png":
        im.save(out, "PNG", optimize=True, **extra)
    elif fmt == "jpg":
        if im.mode in ("RGBA", "LA"):  # JPEG has no transparency: flatten on white, like a reader would
            bg = Image.new("RGB" if im.mode == "RGBA" else "L", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        q = max(75, min(100, int(quality)))
        im.save(out, "JPEG", quality=q, optimize=True, subsampling=0 if q >= 90 else 2, **extra)
    elif fmt == "webp_lossless":
        im.save(out, "WEBP", lossless=True, quality=80, method=4, exact=True, **extra)
    elif fmt == "webp_lossy":
        im.save(out, "WEBP", quality=max(75, min(100, int(quality))), method=6, **extra)
    else:
        raise ValueError(fmt)
    return out.getvalue()


_EXT = {"png": "png", "jpg": "jpg", "webp_lossless": "webp", "webp_lossy": "webp"}


def _fits(fmt: str, im: Image.Image) -> bool:
    limit = WEBP_MAX_DIMENSION if fmt.startswith("webp") else JPEG_MAX_DIMENSION if fmt == "jpg" else None
    return limit is None or (im.width <= limit and im.height <= limit)


def convert_page(data: bytes, ext: str, page_format: str, quality: int = 90,
                 jpeg_quality: int = 90) -> tuple[bytes, str]:
    """Convert a validated page for ``AppSettings.page_format``. Returns ``(bytes, extension)``.

    * ``original`` — unchanged.
    * ``png`` / ``webp_lossless`` — every page in that format, pixel for pixel (verified after
      encoding). A page already in the format is re-encoded only when that makes it smaller.
    * ``jpg`` / ``webp_lossy`` — every page in that format at ``jpeg_quality`` / ``quality``.
      A page already in the format is kept as it is: compressing it again would only lose quality.
    * ``mixed`` — saves space without losing anything: JPEG and WebP pages are kept; PNG, GIF and
      BMP pages become the smallest of the original, an optimised PNG and a lossless WebP (WebP
      only when it saves at least 5 %), every candidate verified pixel for pixel.

    Pages a format cannot hold use the closest format that can: wider or taller than 16 383 px
    for WebP → PNG (lossless) or JPEG (lossy); beyond 65 500 px for JPEG → PNG. Animated GIFs
    stay GIF. Any decoding or encoding error keeps the original bytes, so a conversion problem
    never fails a download.
    """
    page_format = LEGACY_PAGE_FORMATS.get(page_format, page_format)
    if page_format not in PAGE_FORMATS or page_format == "original":
        return data, ext
    try:
        img = _open_page(data)
        if _is_animated(img):
            return data, ext
        icc = img.info.get("icc_profile")
        im = _normalised(img)

        if page_format == "mixed":
            if ext in ("jpg", "webp", "avif"):
                return data, ext  # already compressed: re-encoding would lose quality or grow
            best, best_ext = data, ext
            png = _encode(im, "png", quality, icc)
            if len(png) < len(best) and _pixels_equal(im, png):
                best, best_ext = png, "png"
            if _fits("webp_lossless", im):
                webp = _encode(im, "webp_lossless", quality, icc)
                if len(webp) <= len(best) * (1 - MIXED_WEBP_MIN_SAVING) and _pixels_equal(im, webp):
                    best, best_ext = webp, "webp"
            return best, best_ext

        fmt = page_format
        if not _fits(fmt, im):
            fmt = "jpg" if fmt == "webp_lossy" else "png"
            if not _fits(fmt, im):
                fmt = "png"
        target = _EXT[fmt]
        lossless = fmt in ("png", "webp_lossless")
        if ext == target and not lossless:
            return data, ext  # a JPEG stays the JPEG it is, a lossy WebP stays as uploaded
        if ext == "webp" and fmt == "webp_lossless":
            return data, ext  # may be lossy inside: a lossless copy would only be bigger
        out = _encode(im, fmt, jpeg_quality if fmt == "jpg" else quality, icc)
        if lossless and not _pixels_equal(im, out):
            return data, ext
        if ext == target and len(out) >= len(data):
            return data, ext  # same format: keep the smaller of the two identical images
        with Image.open(io.BytesIO(out)) as check:
            check.verify()
        return out, target
    except Exception:  # noqa: BLE001  (keep the original page)
        return data, ext
