"""Read-only comics archive: find the newest chapter you already own, to suggest where to start.

The archive (the NAS share with the CBZ collection, mounted read-only at
``MDM_ARCHIVE_ROOT``) is never written to. Only the folders listed in the
``archive_dirs`` setting are looked at (by default ``.``, the whole archive), e.g.
``Manga/English/Scanlations``. In the ``archive_custom_dirs`` (e.g. folders of official
releases, empty by default) only the volumes the user assembles from MangaDex chapters,
named ``… - Custom.cbz``, are taken into account.

* **Index** (daily, or on demand): every *title folder* (a folder that directly
  contains ``.cbz``/``.zip`` files) is stored in ``archive_folders`` with its file
  names. Sub-folders of a title (``Extra 1``, ``Previous version``…) are ignored.
  The language comes from the path (``English`` / ``Inglese`` → en, ``Italian`` / ``Italiano`` → it…).
* **Detection** (when you add/approve a title): the folder is matched by any of the
  manga's MangaDex titles (main, alternative, romaji…). The newest chapter is read
  from the file names (``c046``, ``Chapter 12``, ``Vol.08 Ch.110``); for volume
  archives (``v07``, ``Volume 6``, ``… 23``) only the zip *central directory* of the
  newest volume is read – chapter folders (``Volume 1 Chapter 05/``) or page names
  (``… - c043 (v08) - p001``). A volume whose pages carry no chapter number is
  mapped through MangaDex (the last chapter MangaDex lists in that volume).

The result is only a suggestion: nothing is marked or skipped automatically.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.logging_setup import get_logger

log = get_logger(__name__)

ARCHIVE_EXTS = (".cbz", ".zip")
# Path component → MangaDex language code.
FOLDER_LANGUAGES = {
    "inglese": "en", "english": "en", "italiano": "it", "italian": "it", "giapponese": "ja", "japanese": "ja",
    "spagnolo": "es", "spanish": "es", "francese": "fr", "french": "fr", "tedesco": "de", "german": "de",
    "portoghese": "pt-br", "portuguese": "pt-br", "cinese": "zh", "chinese": "zh", "coreano": "ko", "korean": "ko",
}
# Folders searched by default: "." is the whole archive root. Narrow it in Settings → Comics
# archive (one folder per line, relative to the root), e.g. "Manga/English/Scanlations".
DEFAULT_ARCHIVE_DIRS: tuple[str, ...] = (".",)
# Folders where only volumes the user assembles from MangaDex chapters, named "… - Custom.cbz",
# are indexed (e.g. folders of official releases, which are not on MangaDex). None by default.
DEFAULT_ARCHIVE_CUSTOM_DIRS: tuple[str, ...] = ()
CUSTOM_RE = re.compile(r"[\s\-–_]+custom\s*\.(cbz|zip)$", re.I)
INDEX_STATE_KEY = "archive_index_state"
MAX_WALK_DEPTH = 4
# Walking from the archive root (".") goes two levels deeper (e.g. Manga/English/…).
ROOT_WALK_DEPTH = MAX_WALK_DEPTH + 2
_IMG = re.compile(r"\.(jpe?g|png|webp|gif|avif|jxl|bmp)$", re.I)

# Explicit chapter / volume markers. "c" alone must be followed by a digit (c046, c016#2, c072x1).
_NUM = r"(\d+(?:\.\d+)?)"
CHAPTER_RE = re.compile(r"(?:^|[\s_\-.\[(#])(?:chapter|chap|ch)\s*\.?\s*" + _NUM + r"|(?:^|[\s_\-.\[(])c" + _NUM, re.I)
VOLUME_RE = re.compile(r"(?:^|[\s_\-.\[(])(?:volume|vol)\s*\.?\s*" + _NUM + r"|(?:^|[\s_\-.\[(])v" + _NUM + r"|第\s*" + _NUM + r"\s*巻", re.I)
LEADING_NUM_RE = re.compile(r"^[\s\-–_.]*" + _NUM + r"(?:\s|$|[\-–_(\[])")


def now_utc() -> datetime:
    return datetime.now(UTC)


def norm_title(text: str | None) -> str:
    """Comparable form of a title: case/width/punctuation-insensitive, without [END] markers."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).casefold()
    t = re.sub(r"[\[(]\s*(end|completed|complete|fin)\s*[\])]", " ", t)
    t = re.sub(r"[’'`´]", "", t)
    t = re.sub(r"&", " and ", t)
    t = re.sub(r"[\W_]+", " ", t)
    return " ".join(t.split())


def folder_title(name: str) -> str:
    return re.sub(r"\s*[\[(]\s*(END|Completed|Complete)\s*[\])]\s*$", "", name, flags=re.I).strip()


def language_of(rel_path: str) -> str | None:
    for part in rel_path.split("/"):
        code = FOLDER_LANGUAGES.get(part.strip().casefold())
        if code:
            return code
    return None


def _num(text: str) -> float:
    return float(text)


def _first(match: re.Match | None) -> float | None:
    if not match:
        return None
    for g in match.groups():
        if g is not None:
            return _num(g)
    return None


def _strip_title(stem: str, title: str) -> str:
    """Drop the series title from the start of a file name ("Mob Psycho 100 c012" → " c012")."""
    if title and stem.casefold().startswith(title.casefold()):
        return stem[len(title):]
    return stem


def parse_archive_name(name: str, title: str = "") -> tuple[str, float | None]:
    """Classify one archive file name → ("chapter", n) | ("volume", n) | ("other", None)."""
    stem = re.sub(r"\.(cbz|zip)$", "", name, flags=re.I)
    rest = _strip_title(stem, title)
    ch = _first(CHAPTER_RE.search(rest))
    if ch is not None:
        return "chapter", ch
    vol = _first(VOLUME_RE.search(rest))
    if vol is not None:
        return "volume", vol
    # "Title 23.cbz" / "Title - 07 (Digital).cbz": a bare number right after the title is a volume.
    if rest != stem or not title:
        lead = LEADING_NUM_RE.search(rest)
        if lead:
            return "volume", _num(lead.group(1))
    return "other", None


def chapters_inside(names: list[str]) -> list[float]:
    """Chapter numbers encoded inside a volume archive (folder names first, else page names)."""
    found: list[float] = []
    for n in names:
        parts = [p for p in n.replace("\\", "/").split("/") if p]
        if not parts:
            continue
        dirs, base = parts[:-1], parts[-1]
        if not dirs and not _IMG.search(base):
            continue  # ComicInfo.xml and the like
        for candidate in (*reversed(dirs), re.sub(_IMG, "", base)):
            num = _first(CHAPTER_RE.search(candidate))
            if num is not None:
                found.append(num)
                break
    return found


def read_zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:  # reads only the central directory at the end of the file
        return z.namelist()


@dataclass
class FolderAnalysis:
    chapter: float | None = None        # newest chapter found (file name or inside a volume)
    chapter_file: str | None = None
    chapter_from: str | None = None     # "file" | "inside"
    volume: float | None = None         # newest volume whose pages carry no chapter number
    volume_file: str | None = None
    archives: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"chapter": self.chapter, "chapter_file": self.chapter_file, "chapter_from": self.chapter_from,
                "volume": self.volume, "volume_file": self.volume_file, "archives": self.archives,
                "errors": self.errors[:5]}

    @classmethod
    def from_dict(cls, d: dict) -> FolderAnalysis:
        return cls(**{k: d.get(k) for k in ("chapter", "chapter_file", "chapter_from", "volume", "volume_file")},
                   archives=int(d.get("archives") or 0), errors=list(d.get("errors") or []))


def analyze_folder(folder: Path, files: list[str], title: str, reader=read_zip_names) -> FolderAnalysis:
    res = FolderAnalysis(archives=len(files))
    volumes: list[tuple[float, str]] = []
    for name in files:
        kind, num = parse_archive_name(name, title)
        if kind == "chapter" and (res.chapter is None or num > res.chapter):
            res.chapter, res.chapter_file, res.chapter_from = num, name, "file"
        elif kind == "volume":
            volumes.append((num, name))
    # Newest volumes first; stop at the first one that tells us something.
    for vol, name in sorted(volumes, reverse=True)[:3]:
        try:
            inside = chapters_inside(reader(folder / name))
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            res.errors.append(f"{name}: {exc.__class__.__name__}")
            continue
        if inside:
            top = max(inside)
            if res.chapter is None or top > res.chapter:
                res.chapter, res.chapter_file, res.chapter_from = top, name, "inside"
        else:
            res.volume, res.volume_file = vol, name
        break
    return res


# ----------------------------------------------------------------- index


def archive_root() -> Path | None:
    root = get_config().archive_root
    if root is None:
        return None
    root = Path(root)
    try:
        return root if root.is_dir() and any(root.iterdir()) else None
    except OSError:
        return None


def _walk_titles(base: Path, rel: str, depth: int = 0, custom_only: bool = False,
                 max_depth: int = MAX_WALK_DEPTH):
    """Yield (rel_path, archive names) for every folder that directly holds archives.

    With ``custom_only`` only "… - Custom" archives count; a title folder without any is skipped.
    """
    try:
        entries = list(os.scandir(base))
    except OSError as exc:
        log.warning("archive_dir_unreadable", path=rel, error=exc.__class__.__name__)
        return
    files = sorted(e.name for e in entries if e.is_file() and e.name.lower().endswith(ARCHIVE_EXTS))
    if files and rel:
        if custom_only:
            files = [f for f in files if CUSTOM_RE.search(f)]
        if files:
            yield rel, files
        return  # sub-folders of a title are extras / old versions
    if depth >= max_depth:
        return
    for e in sorted(entries, key=lambda e: e.name):
        if e.is_dir() and not e.name.startswith("."):
            child = f"{rel}/{e.name}" if rel else e.name
            yield from _walk_titles(Path(e.path), child, depth + 1, custom_only, max_depth)


def rebuild_index(session: Session, root: Path | None = None, dirs: list[str] | None = None,
                  custom_dirs: list[str] | None = None) -> dict:
    from app.models import ArchiveFolder
    from app.settings_service import load_settings, set_raw

    root = root or archive_root()
    if root is None:
        return {"status": "not_configured"}
    settings = load_settings(session)
    dirs = dirs if dirs is not None else settings.archive_dirs
    custom_dirs = custom_dirs if custom_dirs is not None else settings.archive_custom_dirs
    started = now_utc()
    existing = {f.rel_path: f for f in session.scalars(select(ArchiveFolder))}
    seen: set[str] = set()
    plan = [(d, False) for d in dirs] + [(d, True) for d in custom_dirs if d not in dirs]
    for d, custom_only in plan:
        d = d.strip().strip("/")
        if d in (".", "./"):
            walk = _walk_titles(root, "", custom_only=custom_only, max_depth=ROOT_WALK_DEPTH)
        elif not d or ".." in d.split("/"):
            continue
        else:
            walk = _walk_titles(root / d, d, custom_only=custom_only)
        for rel, files in walk:
            if rel in seen:
                continue
            seen.add(rel)
            title = folder_title(rel.rsplit("/", 1)[-1])
            digest = hashlib.sha256("\n".join(files).encode()).hexdigest()
            row = existing.get(rel)
            if row is None:
                row = ArchiveFolder(rel_path=rel)
                session.add(row)
            if row.files_hash != digest:
                row.analysis = None  # recomputed on demand
            row.title, row.title_norm, row.language = title, norm_title(title), language_of(rel)
            row.files, row.files_hash, row.indexed_at = files, digest, started
            row.custom_only = custom_only
    gone = [p for p in existing if p not in seen]
    if gone:
        session.execute(delete(ArchiveFolder).where(ArchiveFolder.rel_path.in_(gone)))
    summary = {"status": "success", "folders": len(seen), "removed": len(gone), "at": started.isoformat(),
               "dirs": dirs, "custom_dirs": custom_dirs}
    set_raw(session, INDEX_STATE_KEY, summary)
    session.flush()
    log.info("archive_indexed", folders=len(seen), removed=len(gone))
    return summary


def refresh_folder(session: Session, rel_path: str, root: Path | None = None) -> dict:
    """Re-read one indexed title folder now (after adding or replacing archives in it).

    Only folders already in the index can be refreshed (the path never comes from the
    client unchecked). The cached analysis is always recomputed, so archives replaced
    under the same name are read again too. A folder that is gone or no longer holds
    archives leaves the index.
    """
    from app.models import ArchiveFolder

    root = root or archive_root()
    if root is None:
        return {"status": "not_configured"}
    row = session.get(ArchiveFolder, rel_path)
    if row is None:
        return {"status": "unknown_folder"}
    folder = root / row.rel_path
    try:
        entries = list(os.scandir(folder))
    except FileNotFoundError:
        entries = None
    except OSError as exc:
        log.warning("archive_dir_unreadable", path=row.rel_path, error=exc.__class__.__name__)
        return {"status": "unreadable", "error": exc.__class__.__name__}
    files = sorted(e.name for e in entries or [] if e.is_file() and e.name.lower().endswith(ARCHIVE_EXTS))
    if row.custom_only:
        files = [f for f in files if CUSTOM_RE.search(f)]
    if not files:
        session.delete(row)
        session.flush()
        log.info("archive_folder_removed", path=rel_path)
        return {"status": "removed", "folder": rel_path}
    digest = hashlib.sha256("\n".join(files).encode()).hexdigest()
    changed = digest != row.files_hash
    row.files, row.files_hash, row.indexed_at = files, digest, now_utc()
    row.analysis = None
    res = folder_analysis(session, row, root)
    session.flush()
    log.info("archive_folder_refreshed", path=rel_path, archives=len(files), changed=changed, chapter=res.chapter)
    return {"status": "success", "folder": rel_path, "changed": changed, "archives": len(files),
            "chapter": res.chapter, "chapter_file": res.chapter_file}


def index_state(session: Session) -> dict:
    from app.settings_service import get_raw

    return dict(get_raw(session, INDEX_STATE_KEY, {}) or {})


def index_due(session: Session, now: datetime | None = None) -> bool:
    from app.settings_service import load_settings

    settings = load_settings(session)
    if not settings.archive_enabled or archive_root() is None:
        return False
    at = index_state(session).get("at")
    if not at:
        return True
    try:
        last = datetime.fromisoformat(at)
    except ValueError:
        return True
    return ((now or now_utc()) - last).total_seconds() >= settings.archive_rescan_hours * 3600


def enqueue_index(force: bool = False) -> bool:
    from app.redis_client import get_redis
    from app.services.jobs import QUEUE_DEFAULT, get_queue

    r = get_redis()
    if not r.set("mdm:archive_index_pending", "1", nx=True, ex=3600):
        return False
    try:
        get_queue(QUEUE_DEFAULT).enqueue("app.tasks.task_archive_index", job_timeout=3600, result_ttl=3600,
                                         failure_ttl=7 * 86400)
    except Exception:
        r.delete("mdm:archive_index_pending")
        raise
    return True


# ----------------------------------------------------------------- suggestion


def manga_title_keys(titles: list[str | None], alt_titles: list[Any]) -> set[str]:
    keys = set(titles)
    for a in alt_titles or []:
        if isinstance(a, dict):
            keys.update(a.values())
    return {k for k in (norm_title(x) for x in keys if x) if len(k) >= 3}


def folder_analysis(session: Session, row, root: Path) -> FolderAnalysis:
    if row.analysis:
        return FolderAnalysis.from_dict(row.analysis)
    res = analyze_folder(root / row.rel_path, list(row.files or []), row.title)
    row.analysis = res.to_dict()
    return res


@dataclass
class MangaDexChapters:
    """Numbered chapters MangaDex lists (selected languages): number → volume."""

    numbers: list[float]
    volume_last: dict[str, float]  # volume label → newest chapter number in it

    @classmethod
    def build(cls, pairs: list[tuple[float, str | None]]) -> MangaDexChapters:
        nums = sorted({n for n, _ in pairs})
        last: dict[str, float] = {}
        for n, vol in pairs:
            if vol is None:
                continue
            key = _vol_key(vol)
            if key is not None and n > last.get(key, -1):
                last[key] = n
        return cls(nums, last)


def _vol_key(vol: Any) -> str | None:
    try:
        return f"{float(vol):g}"
    except (TypeError, ValueError):
        return None


def resolve(analysis: FolderAnalysis, md: MangaDexChapters) -> dict[str, Any] | None:
    """Newest owned chapter + the chapter to continue from."""
    last, source, evidence = analysis.chapter, analysis.chapter_from, analysis.chapter_file
    if analysis.volume is not None:
        mapped = md.volume_last.get(_vol_key(analysis.volume) or "")
        if mapped is not None and (last is None or mapped > last):
            last, source, evidence = mapped, "volume", analysis.volume_file
        elif last is None:
            return {"last_chapter": None, "last_volume": analysis.volume, "source": "volume_unmapped",
                    "evidence": analysis.volume_file, "next_chapter": None, "after_count": None,
                    "up_to_date": False}
    if last is None:
        return None
    after = [n for n in md.numbers if n > last]
    return {"last_chapter": last, "last_volume": analysis.volume if source == "volume" else None,
            "source": source, "evidence": evidence, "next_chapter": after[0] if after else None,
            "after_count": len(after), "after_last": after[-1] if after else None, "up_to_date": not after}


def suggest(session: Session, titles: list[str | None], alt_titles: list[Any], languages: list[str],
            md: MangaDexChapters, root: Path | None = None) -> dict[str, Any]:
    from app.models import ArchiveFolder
    from app.settings_service import load_settings

    root = root or archive_root()
    settings = load_settings(session)
    state = index_state(session)
    out: dict[str, Any] = {"configured": root is not None and settings.archive_enabled,
                           "indexed_at": state.get("at"), "candidates": [], "best": None}
    if not out["configured"]:
        return out
    keys = manga_title_keys(titles, alt_titles)
    if not keys:
        return out
    rows = list(session.scalars(select(ArchiveFolder).where(ArchiveFolder.title_norm.in_(keys))))
    cands = []
    for row in rows:
        analysis = folder_analysis(session, row, root)
        r = resolve(analysis, md)
        cands.append({"folder": row.rel_path, "title": row.title, "language": row.language,
                      "custom_only": bool(row.custom_only),
                      "language_match": row.language in languages if row.language else False,
                      "archives": analysis.archives, "errors": analysis.errors, **(r or {"last_chapter": None})})
    # Selected languages first, then the newest chapter.
    cands.sort(key=lambda c: (not c["language_match"], -(c.get("last_chapter") or -1), c["folder"]))
    out["candidates"] = cands
    out["best"] = next((c for c in cands if c["language_match"] and c.get("last_chapter") is not None), None)
    return out
