"""Filesystem-safe, deterministic names for library folders.

Folder layout::

    {root}/{Manga Title}/{Language}/{Scanlation Group}/{Chapter Folder}/001.jpg

Chapter folder formats (padding is configurable; defaults match the spec)::

    Volume 01 Chapter 01 - Ranma Arrives in Tokyo
    Volume 01 Chapter 02
    Chapter 012 - A New Challenge
    Chapter 012.5 - Extra Story
    Oneshot - Some Title

With a volume placeholder (default ``TBD``) numbered chapters without a volume get
one, so every name starts with the volume and alphabetical order is reading order;
the placeholder sorts after the numbered volumes::

    Volume TBD Chapter 012 - A New Challenge

With the scanlation group in the name, the group is appended (the same chapter from
two groups gives two folders side by side)::

    Volume 03 Chapter 12 - A New Challenge - Team X
    Volume 03 Chapter 12 - A New Challenge - Other Scans
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

UNKNOWN_GROUP = "Unknown Group"
MAX_COMPONENT_BYTES = 200  # well below the common 255-byte limit (leaves room for suffixes)
MAX_PATH_BYTES = 3800

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")
_NUMERIC = re.compile(r"^\d+(\.\d+)?$")


def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return cut.rstrip(" .-")


def sanitize_component(name: str | None, fallback: str = "Untitled", max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Make ``name`` safe as a single path component on Linux, NFS and SMB shares."""
    s = unicodedata.normalize("NFC", name or "")
    s = _CONTROL.sub(" ", s)
    s = s.replace(":", " - ")
    s = s.replace("/", "-").replace("\\", "-")
    s = s.replace('"', "'")
    s = re.sub(r"[*?<>|]", "", s)
    s = _WS.sub(" ", s).strip()
    s = re.sub(r"(\s-\s)(\s?-\s)+", " - ", s)  # collapse repeated separators
    s = s.strip(" .")  # no leading dots (hidden files) / trailing dots or spaces
    s = _truncate_bytes(s, max_bytes).strip(" .")
    if not s or s in {".", ".."}:
        s = fallback
    stem = s.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        s = f"{s}_"
    return s


def _pad_number(value: str, width: int) -> str:
    """Zero-pad the integer part of a numeric chapter/volume string."""
    value = value.strip()
    if _NUMERIC.match(value):
        try:
            d = Decimal(value)
        except InvalidOperation:
            return value
        int_part, _, frac = value.partition(".")
        int_part = str(int(int_part)).zfill(width)
        frac = frac.rstrip("0") if frac else ""
        if d == d.to_integral_value() or not frac:
            return int_part
        return f"{int_part}.{frac}"
    return value


def chapter_sort_number(chapter: str | None) -> float | None:
    if chapter is None:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", str(chapter))
    return float(m.group(1)) if m else None


def chapter_kind(chapter: str | None) -> str:
    if chapter is None:
        return "oneshot"
    s = str(chapter).strip()
    if not _NUMERIC.match(s):
        return "special"
    return "decimal" if "." in s and Decimal(s) != Decimal(s).to_integral_value() else "regular"


# Titles whose chapter numbering restarts in every volume (anthologies...) sort
# and compare chapters as volume * VOLUME_FACTOR + chapter ("Vol.15 Ch.2" = 15002).
VOLUME_FACTOR = 1000


def _norm_number(value: str) -> str:
    s = str(value).strip()
    if _NUMERIC.match(s):
        s = format(Decimal(s).normalize(), "f")
    return s.lower()


def logical_chapter_key(chapter: str | None, release_id: str, volume: str | None = None,
                        per_volume: bool = False) -> str:
    """Identity of a logical chapter within a manga.

    Numbered chapters share a key across languages/groups ("c:12.5").
    Chapters without a number never merge with anything (keyed by release UUID).
    With ``per_volume`` (numbering restarts every volume) the volume is part of the
    identity ("v15:c:2"); releases without a volume keep the plain key.
    """
    if chapter is None or not str(chapter).strip():
        return f"x:{release_id}"
    key = f"c:{_norm_number(chapter)}"
    if per_volume and volume is not None and str(volume).strip():
        return f"v{_norm_number(volume)}:{key}"
    return key


def logical_sort_number(chapter: str | None, volume: str | None = None, per_volume: bool = False) -> float | None:
    """Order of a chapter within its manga; also what ``start_chapter`` is compared with.

    Plain chapter number, or volume * VOLUME_FACTOR + chapter for per-volume numbering.
    """
    n = chapter_sort_number(chapter)
    if n is None or not per_volume:
        return n
    v = chapter_sort_number(volume)
    return n if v is None else v * VOLUME_FACTOR + n


def split_sort_number(value: float | None) -> tuple[float | None, float | None]:
    """(volume, chapter) of a per-volume sort number (inverse of ``logical_sort_number``)."""
    if value is None:
        return None, None
    value = float(value)
    vol = int(value // VOLUME_FACTOR)
    return float(vol), round(value - vol * VOLUME_FACTOR, 4)


DEFAULT_VOLUME_PLACEHOLDER = "TBD"


@dataclass(frozen=True)
class NamingOptions:
    volume_padding: int = 2
    chapter_padding_with_volume: int = 2
    chapter_padding_without_volume: int = 3
    # "Volume TBD" for numbered chapters without a volume ("" = no volume part, the old format).
    volume_placeholder: str = ""
    # Append " - {Group}" to the chapter folder (the group never becomes a folder level).
    group_in_name: bool = False


def chapter_folder_name(
    volume: str | None,
    chapter: str | None,
    title: str | None,
    opts: NamingOptions = NamingOptions(),
    group_names: list[str] | None = None,
) -> str:
    parts: list[str] = []
    vol = (volume or "").strip() or None
    chap = (chapter or "").strip() or None
    placeholder = sanitize_component(opts.volume_placeholder, fallback="") if opts.volume_placeholder else ""
    if vol:
        parts.append(f"Volume {_pad_number(vol, opts.volume_padding)}")
    elif chap and placeholder:
        parts.append(f"Volume {placeholder}")
    if chap:
        width = opts.chapter_padding_with_volume if vol else opts.chapter_padding_without_volume
        parts.append(f"Chapter {_pad_number(chap, width)}")
    else:
        parts.append("Oneshot")
    base = " ".join(parts)
    clean_title = sanitize_component(title, fallback="") if title else ""
    # Skip titles that merely repeat the number ("Chapter 12").
    if clean_title and re.fullmatch(r"(?i)(ch(apter)?\.?\s*)?" + re.escape(chap or "") + r"", clean_title) and chap:
        clean_title = ""
    suffix = f" - {group_folder_name(group_names or [])}" if opts.group_in_name else ""
    name = f"{base} - {clean_title}" if clean_title else base
    if suffix:  # the group must survive truncation: shorten the title, never the suffix
        name = _truncate_bytes(name, MAX_COMPONENT_BYTES - len(suffix.encode())).rstrip(" .-") or base
    return sanitize_component(f"{name}{suffix}", fallback=base)


def is_volume_placeholder(volume: str | None, placeholder: str | None = DEFAULT_VOLUME_PLACEHOLDER) -> bool:
    """True for the placeholder volume of a chapter folder name ("TBD")."""
    if not volume:
        return False
    names = {DEFAULT_VOLUME_PLACEHOLDER.casefold()}
    if placeholder:
        names.add(placeholder.strip().casefold())
    return volume.strip().casefold() in names


def group_folder_name(group_names: list[str]) -> str:
    names = [n for n in (g.strip() for g in group_names) if n]
    if not names:
        return UNKNOWN_GROUP
    return sanitize_component(" & ".join(names), fallback=UNKNOWN_GROUP)


def with_collision_suffix(name: str, release_id: str) -> str:
    """Deterministic alternative name for a collision: append a short UUID."""
    suffix = f" [{str(release_id).replace('-', '')[:8]}]"
    base = _truncate_bytes(name, MAX_COMPONENT_BYTES - len(suffix.encode())).rstrip(" .")
    return f"{base}{suffix}"


def page_filename(index: int, extension: str, total: int) -> str:
    width = max(3, len(str(total)))
    ext = extension.lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    return f"{index:0{width}d}.{ext}"


_CHAPTER_FOLDER_RE = re.compile(
    r"^(?:Vol(?:ume)?\.?\s*(?P<vol>[^\s]+)\s+)?"
    r"(?:(?:Ch(?:apter)?\.?\s*(?P<ch>[0-9]+(?:\.[0-9]+)?[a-z]?))|(?P<oneshot>Oneshot))"
    r"(?:\s*-\s*(?P<title>.*?))?"
    r"(?:\s*\[(?P<suffix>[0-9a-f]{8})\])?$",
    re.IGNORECASE,
)


@dataclass
class ParsedFolder:
    volume: str | None
    chapter: str | None
    title: str | None
    oneshot: bool
    uuid_prefix: str | None


def parse_chapter_folder(name: str, volume_placeholder: str | None = DEFAULT_VOLUME_PLACEHOLDER) -> ParsedFolder | None:
    """Best-effort parse of an existing chapter folder name (library import).

    A placeholder volume ("Volume TBD") parses as no volume. A " - {Group}" suffix
    stays in ``title``; ``name_ends_with_group`` checks it against a release.
    """
    m = _CHAPTER_FOLDER_RE.match(name.strip())
    if not m:
        return None

    def unpad(v: str | None) -> str | None:
        if v is None:
            return None
        if _NUMERIC.match(v):
            ip, _, frac = v.partition(".")
            return f"{int(ip)}.{frac}" if frac else str(int(ip))
        return v

    vol = m.group("vol")
    if is_volume_placeholder(vol, volume_placeholder):
        vol = None
    return ParsedFolder(
        volume=unpad(vol),
        chapter=unpad(m.group("ch")),
        title=(m.group("title") or None),
        oneshot=bool(m.group("oneshot")),
        uuid_prefix=m.group("suffix"),
    )


def normalize_number(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if _NUMERIC.match(v):
        return format(Decimal(v).normalize(), "f")
    return v.lower()


def name_ends_with_group(folder_name: str, group_names: list[str] | None) -> bool:
    """Whether a chapter folder name carries the " - {Group}" suffix of these groups."""
    name = re.sub(r"\s*\[[0-9a-f]{8}\]$", "", folder_name.strip())  # collision suffix
    return name.casefold().endswith(f" - {group_folder_name(group_names or [])}".casefold())
