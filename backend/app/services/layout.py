"""Per-manga folder layout: which of the ``{Language}`` / ``{Group}`` levels exist.

::

    {Manga}/{Language}/{Group}/{Chapter}   more than one language, more than one group
    {Manga}/{Language}/{Chapter}           more than one language, one group in that language
    {Manga}/{Group}/{Chapter}              one language, more than one group
    {Manga}/{Chapter}                      one language, one group

Rules:

* the language level exists when the manga has more than one language (the
  languages selected for it, plus any language already in the library or queue);
* inside a language, the group level exists when chapters of more than one
  scanlation group are in the library or queued for that language.

The layout is stored on the manga (``Manga.folder_layout``) and only grows during
normal operation. When a download needs a level that does not exist yet (a second
language, another scanlation group), the chapter folders already on disk – and
legacy partial folders inside the library – are moved into the new structure first,
then the new chapter is downloaded. This happens under the exclusive per-manga
folder lock and never while another chapter of the same manga is being written.

``mdm folder-layout`` recomputes the minimal layout (it can also remove levels)
and applies it to existing titles.

Only folders linked to a release are moved; unknown folders are left where they
are. Nothing is ever deleted except directories that became empty.

With *group in chapter name* on (``AppSettings.group_in_chapter_name``, the
default) the group level is never needed: chapters of several groups sit side by
side in one folder, and a chapter folder ends with `` - {Group}`` only when the
same chapter (same language, same logical chapter) is in the library or queued
from more than one group (``group_suffix_ids``). When such a duplicate is about
to be downloaded, the copy already on disk is renamed first
(``ensure_group_suffixes``); suffixes are never removed automatically. Titles
that already have a group level keep it until ``mdm chapter-names --apply``
flattens them.

``rename_chapter_folders`` renames chapter folders to the current naming rules:
automatically after a scan when MangaDex assigned a volume to a chapter that is
still in the library (``Volume TBD …`` → ``Volume 07 …``), and for every folder
with ``mdm chapter-names``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.enums import ACTIVE_JOB_STATUSES, JobStatus, LocalStatus
from app.library.marker import (
    PARTIAL_PREFIX,
    RELEASE_MARKER,
    is_legacy_partial,
    read_release_marker,
    write_release_marker,
)
from app.library.naming import (
    group_folder_name,
    name_ends_with_group,
    normalize_number,
    parse_chapter_folder,
    with_collision_suffix,
)
from app.library.storage import fsync_dir, resolve_rel, safe_join
from app.logging_setup import get_logger
from app.models import ChapterRelease, DownloadJob, Language, Manga

log = get_logger(__name__)

FLAT_LAYOUT: dict[str, Any] = {"language": False, "groups": []}
_FINISHED_JOBS = (JobStatus.COMPLETED, JobStatus.CANCELLED)


class LayoutBusy(Exception):
    """Another chapter of this manga is being written; the folders cannot be moved now."""


class LayoutConflict(Exception):
    """A folder cannot be moved into the new layout (the destination is taken)."""

    def __init__(self, message: str, rel_path: str):
        super().__init__(message)
        self.rel_path = rel_path


@dataclass(frozen=True)
class Layout:
    language: bool = False
    groups: frozenset[str] = field(default_factory=frozenset)  # language codes with a group level

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> Layout:
        value = value or {}
        return cls(bool(value.get("language")), frozenset(value.get("groups") or []))

    def to_json(self) -> dict[str, Any]:
        return {"language": self.language, "groups": sorted(self.groups)}

    def merge(self, other: Layout) -> Layout:
        return Layout(self.language or other.language, self.groups | other.groups)

    def group_level(self, language_code: str) -> bool:
        return language_code in self.groups


def language_folders(session: Session) -> dict[str, str]:
    return {lang.code: lang.folder_code for lang in session.scalars(select(Language))}


def base_parts(manga_folder: str, language_code: str, lang_folder: str, group_names: list[str] | None,
               layout: Layout) -> list[str]:
    """Path components from the library root to the folder that holds the chapter folders."""
    parts = [manga_folder]
    if layout.language:
        parts.append(lang_folder)
    if layout.group_level(language_code):
        parts.append(group_folder_name(group_names or []))
    return parts


def release_base_parts(session: Session, manga: Manga, release: ChapterRelease, layout: Layout | None = None,
                       folders: dict[str, str] | None = None) -> list[str]:
    folders = folders if folders is not None else language_folders(session)
    layout = layout if layout is not None else Layout.from_json(manga.folder_layout)
    lang_folder = folders.get(release.language_code, release.language_code.upper())
    return base_parts(manga.folder_name, release.language_code, lang_folder, release.group_names, layout)


def _on_disk(root: Path, rel: str | None) -> bool:
    try:
        return bool(rel) and resolve_rel(root, rel).is_dir()
    except Exception:
        return False


def _relevant_releases(session: Session, root: Path, manga_id: uuid.UUID) -> tuple[list[ChapterRelease], int]:
    """Releases whose folder is in the library right now, or that are queued for download.

    Chapters moved out of the library (or deleted) no longer count. Also returns how
    many folders (chapters + partial downloads) of the manga are on disk.
    """
    queued_jobs = list(session.scalars(
        select(DownloadJob).join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga_id,
               DownloadJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]))))
    queued_ids = {j.release_id for j in queued_jobs}
    partial_on_disk = sum(1 for j in queued_jobs
                          if is_legacy_partial(j.temp_dir_rel) and _on_disk(root, j.temp_dir_rel))
    out, on_disk = [], 0
    for r in session.scalars(select(ChapterRelease).where(
        ChapterRelease.manga_id == manga_id,
        or_(ChapterRelease.folder_rel_path.is_not(None), ChapterRelease.id.in_(queued_ids)),
    )):
        present = r.local_status != LocalStatus.MISSING_ON_DISK and _on_disk(root, r.folder_rel_path)
        on_disk += present
        if present or r.id in queued_ids:
            out.append(r)
    return out, on_disk + partial_on_disk


def needed_layout(session: Session, root: Path, manga_id: uuid.UUID,
                  extra: ChapterRelease | None = None) -> tuple[Layout, int]:
    """The minimal layout for what is selected, on disk and queued (plus ``extra``).

    Also returns the number of the manga's folders currently on disk.
    """
    from app.services.catalog import manga_language_codes
    from app.settings_service import load_settings

    group_level = not load_settings(session).group_in_chapter_name
    languages = set(manga_language_codes(session, manga_id))
    groups: dict[str, set[str]] = {}
    releases, on_disk = _relevant_releases(session, root, manga_id)
    if extra is not None:
        releases.append(extra)
    for r in releases:
        languages.add(r.language_code)
        groups.setdefault(r.language_code, set()).add(group_folder_name(r.group_names or []).lower())
    group_codes = frozenset(code for code, g in groups.items() if len(g) > 1) if group_level else frozenset()
    return Layout(len(languages) > 1, group_codes), on_disk


# --------------------------------------------------------------------- planning


@dataclass
class _Plan:
    moves: list[tuple[str, str]] = field(default_factory=list)  # (old rel, new rel), directories
    releases: list[tuple[ChapterRelease, str]] = field(default_factory=list)
    jobs: list[tuple[DownloadJob, str | None, str | None]] = field(default_factory=list)


def _plan(session: Session, root: Path, manga: Manga, target: Layout,
          leaf_for: Callable[[ChapterRelease], str | None] | None = None) -> _Plan:
    """Moves into ``target``. ``leaf_for`` gives a new chapter folder name (None = keep it)."""
    folders = language_folders(session)
    plan = _Plan()
    linked = list(session.scalars(select(ChapterRelease).where(
        ChapterRelease.manga_id == manga.id, ChapterRelease.folder_rel_path.is_not(None))))
    all_paths = {r.folder_rel_path: r for r in linked}
    taken: set[str] = set()
    new_path_of: dict[uuid.UUID, str] = {}

    for r in sorted(linked, key=lambda x: x.folder_rel_path):
        old = r.folder_rel_path
        base = release_base_parts(session, manga, r, target, folders)
        leaf = old.rsplit("/", 1)[-1]
        if leaf_for is not None and _on_disk(root, old):
            wanted = leaf_for(r)
            # A folder already carrying its collision suffix keeps it when that is the wanted name.
            if wanted and wanted != leaf and with_collision_suffix(wanted, str(r.id)) != leaf:
                leaf = wanted
        new = "/".join([*base, leaf])
        if new == old:
            taken.add(new)
            continue
        if not resolve_rel(root, old).is_dir():
            continue  # deleted by the user: the record keeps pointing at its old place
        candidates = [new]
        suffixed = with_collision_suffix(leaf, str(r.id))
        if suffixed != leaf:
            candidates.append("/".join([*base, suffixed]))
        for cand in candidates:
            if cand in taken or cand in all_paths or resolve_rel(root, cand).exists():
                continue
            new = cand
            break
        else:
            raise LayoutConflict(f"Cannot move “{old}”: “{new}” is already taken in the library", new)
        taken.add(new)
        new_path_of[r.id] = new
        plan.moves.append((old, new))
        plan.releases.append((r, new))

    jobs = session.scalars(
        select(DownloadJob).join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga.id,
               DownloadJob.status.not_in([s.value for s in _FINISHED_JOBS]),
               or_(DownloadJob.temp_dir_rel.is_not(None), DownloadJob.target_rel_path.is_not(None)))
    ).all()
    for job in jobs:
        r = session.get(ChapterRelease, job.release_id)
        base = release_base_parts(session, manga, r, target, folders)
        new_temp = job.temp_dir_rel
        if is_legacy_partial(job.temp_dir_rel):  # staging folders live outside the library
            new_temp = "/".join([*base, f"{PARTIAL_PREFIX}{r.id}"])
            if new_temp != job.temp_dir_rel and resolve_rel(root, job.temp_dir_rel).is_dir():
                if resolve_rel(root, new_temp).exists() or new_temp in taken:
                    raise LayoutConflict(f"Cannot move the partial download “{job.temp_dir_rel}”", new_temp)
                taken.add(new_temp)
                plan.moves.append((job.temp_dir_rel, new_temp))
        new_target = job.target_rel_path
        if job.target_rel_path:
            if r.id in new_path_of and r.folder_rel_path == job.target_rel_path:
                new_target = new_path_of[r.id]
            else:
                new_target = "/".join([*base, job.target_rel_path.rsplit("/", 1)[-1]])
        if (new_temp, new_target) != (job.temp_dir_rel, job.target_rel_path):
            plan.jobs.append((job, new_target, new_temp))

    # A destination must never end up inside another chapter folder.
    chapter_dirs = set(all_paths) | {old for old, _ in plan.moves}
    for _, new in plan.moves:
        parts = new.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in chapter_dirs:
                raise LayoutConflict(f"“{new}” would be created inside a chapter folder", new)
    for old, new in plan.moves:  # validates every component (no traversal, sane names)
        safe_join(root, *old.split("/"))
        safe_join(root, *new.split("/"))
    return plan


# --------------------------------------------------------------------- applying


def _remove_empty_parents(root: Path, manga_dir: Path, rel_paths: list[str]) -> None:
    for rel in rel_paths:
        p = resolve_rel(root, rel).parent
        while p != manga_dir and manga_dir in p.parents:
            try:
                p.rmdir()  # only succeeds on an empty directory
            except OSError:
                break
            p = p.parent


def apply_layout(session: Session, root: Path, manga: Manga, target: Layout, *, dry_run: bool = False,
                 reason: str = "", leaf_for: Callable[[ChapterRelease], str | None] | None = None,
                 event: tuple[str, str] | None = None) -> dict[str, Any]:
    """Move the manga's chapter folders into ``target`` and store it. Commits on success.

    The caller must hold the exclusive folder lock of the manga and make sure no
    chapter of it is being downloaded. ``leaf_for`` also renames chapter folders;
    ``event`` replaces the default *reorganised* history entry.
    """
    current = Layout.from_json(manga.folder_layout)
    plan = _plan(session, root, manga, target, leaf_for)
    report: dict[str, Any] = {
        "manga": manga.folder_name, "from": current.to_json(), "to": target.to_json(),
        "moves": [{"from": o, "to": n} for o, n in plan.moves], "dry_run": dry_run,
    }
    if dry_run:
        session.rollback()
        return report

    done: list[tuple[Path, Path]] = []
    touched: set[Path] = set()
    try:
        for old, new in plan.moves:
            src, dst = resolve_rel(root, old), resolve_rel(root, new)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                raise LayoutConflict(f"“{new}” appeared while reorganising", new)
            os.rename(src, dst)
            done.append((src, dst))
            touched.update({src.parent, dst.parent})
        for d in touched:
            if d.is_dir():
                fsync_dir(d)
        for r, new in plan.releases:
            r.folder_rel_path = None
        session.flush()  # folder_rel_path is unique: clear first, then set (swaps are possible)
        for r, new in plan.releases:
            r.folder_rel_path = new
        for job, new_target, new_temp in plan.jobs:
            job.target_rel_path = new_target
            job.temp_dir_rel = new_temp
        manga.folder_layout = target.to_json()
        from app.services.control import record_event

        if event is not None:
            record_event(session, event[0], event[1].format(n=len(plan.moves)), manga_id=manga.id)
        else:
            record_event(session, "reorganised",
                         f"Folder layout {_describe(current)} → {_describe(target)}"
                         f"{f' ({reason})' if reason else ''}: {len(plan.moves)} folder(s) moved",
                         manga_id=manga.id)
        session.commit()
    except Exception:
        session.rollback()
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError:
                log.exception("layout_rollback_failed", src=str(src), dst=str(dst))
        raise
    _remove_empty_parents(root, safe_join(root, manga.folder_name), [o for o, _ in plan.moves])
    if leaf_for is not None:
        for r, new in plan.releases:
            _refresh_marker(resolve_rel(root, new), r)
    log.info("layout_applied", manga_id=str(manga.id), layout=target.to_json(), moved=len(plan.moves))
    return report


def _describe(layout: Layout) -> str:
    parts = ["Language" if layout.language else None,
            f"Group[{','.join(sorted(layout.groups))}]" if layout.groups else None]
    return "/".join(p for p in parts if p) or "flat"


def ensure_layout_for_release(session: Session, root: Path, release_id: uuid.UUID, job_id: int | None) -> Layout:
    """Grow the manga's layout if ``release`` needs a new level, moving existing folders first."""
    from app.services.rename import _lock_exclusive

    release = session.get(ChapterRelease, release_id)
    _lock_exclusive(session, release.manga_id)
    manga = session.get(Manga, release.manga_id, with_for_update=True)
    current = Layout.from_json(manga.folder_layout)
    needed, on_disk = needed_layout(session, root, manga.id, release)
    # Levels are never removed while chapters of the manga are in the library. When nothing
    # is left on disk (everything was moved away) the layout starts over from the minimum.
    target = current.merge(needed) if on_disk else needed
    if target == current:
        return current
    busy = session.scalar(
        select(func.count()).select_from(DownloadJob)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga.id, DownloadJob.status == JobStatus.DOWNLOADING,
               DownloadJob.id != (job_id or -1))
    ) or 0
    if busy:
        raise LayoutBusy("Waiting for the other chapters of this manga to finish before reorganising its folders")
    lang = release.language_code
    reason = ("library empty, layout reset" if not on_disk
              else "new language" if target.language and not current.language
              else f"new scanlation group in {lang}")
    apply_layout(session, root, manga, target, reason=reason)
    return target


def recompute_layout(session: Session, root: Path, manga_id: uuid.UUID, *, dry_run: bool = True) -> dict[str, Any]:
    """Apply the minimal layout to a manga (may also remove levels). Used by ``mdm folder-layout``."""
    from app.services.rename import _lock_exclusive

    _lock_exclusive(session, manga_id)
    manga = session.get(Manga, manga_id, with_for_update=True)
    current = Layout.from_json(manga.folder_layout)
    target, _ = needed_layout(session, root, manga_id)
    if target == current:
        session.rollback()
        return {"manga": manga.folder_name, "from": current.to_json(), "to": target.to_json(),
                "moves": [], "dry_run": dry_run, "unchanged": True}
    busy = session.scalar(
        select(func.count()).select_from(DownloadJob)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga_id,
               DownloadJob.status.in_([JobStatus.QUEUED, JobStatus.DOWNLOADING]))
    ) or 0
    if busy and not dry_run:
        session.rollback()
        raise LayoutBusy(f"{busy} chapter(s) of “{manga.folder_name}” are downloading; try again later")
    return apply_layout(session, root, manga, target, dry_run=dry_run, reason="recomputed")


# --------------------------------------------------------------------- chapter names


def _refresh_marker(folder: Path, release: ChapterRelease) -> None:
    """Keep the volume / title / group in ``.mdm-release.json`` in step with MangaDex."""
    try:
        if not (folder / RELEASE_MARKER).is_file():
            return
        data = read_release_marker(folder)
        if not data or data.get("chapter_id") != str(release.id):
            return
        fresh = {
            "volume": release.volume, "chapter": release.chapter, "title": release.title,
            "scanlation_group": " & ".join(n for n in (release.group_names or []) if n) or None,
        }
        if all(data.get(k) == v for k, v in fresh.items()):
            return
        data.update(fresh)
        data.pop("schema", None)
        data.pop("written_at", None)
        write_release_marker(folder, data)
    except OSError:
        log.warning("release_marker_refresh_failed", folder=str(folder))


def _norm_volume(value: str | None) -> str | None:
    return normalize_number(value) if value and value.strip() else None


def volume_out_of_date(release: ChapterRelease, placeholder: str | None) -> bool:
    """Whether the chapter folder name shows a different volume than MangaDex has now.

    Folders whose name cannot be parsed (imported with other names) are never touched.
    """
    if not release.folder_rel_path:
        return False
    parsed = parse_chapter_folder(release.folder_rel_path.rsplit("/", 1)[-1], placeholder)
    if parsed is None:
        return False
    return _norm_volume(parsed.volume) != _norm_volume(release.volume)


def rename_chapter_folders(session: Session, root: Path, manga_id: uuid.UUID, *, dry_run: bool = False,
                           only_volume_changes: bool = False, minimal_layout: bool = False) -> dict[str, Any]:
    """Rename the manga's chapter folders to the current naming rules. Commits on success.

    ``only_volume_changes``: only chapters whose volume on MangaDex differs from the
    one in their folder name (automatic, after scans). ``minimal_layout``: also apply
    the minimal folder layout (removes a {Group} level made unnecessary by group names
    in the chapter folders). Raises ``LayoutBusy`` while a chapter is downloading.
    """
    from app.services.downloader import desired_chapter_folder_name
    from app.services.rename import _lock_exclusive
    from app.settings_service import load_settings

    settings = load_settings(session)
    _lock_exclusive(session, manga_id)
    manga = session.get(Manga, manga_id, with_for_update=True)
    current = Layout.from_json(manga.folder_layout)
    target = needed_layout(session, root, manga_id)[0] if minimal_layout else current
    suffixed = group_suffix_ids(session, root, manga_id)

    def leaf_for(r: ChapterRelease) -> str | None:
        if only_volume_changes:
            if not volume_out_of_date(r, settings.volume_placeholder):
                return None
            # A volume rename never drops a group suffix the folder already has.
            leaf = r.folder_rel_path.rsplit("/", 1)[-1]
            return desired_chapter_folder_name(
                r, settings, r.id in suffixed or name_ends_with_group(leaf, r.group_names))
        return desired_chapter_folder_name(r, settings, r.id in suffixed)

    plan = _plan(session, root, manga, target, leaf_for)
    report: dict[str, Any] = {"manga": manga.folder_name, "from": current.to_json(), "to": target.to_json(),
                              "moves": [{"from": o, "to": n} for o, n in plan.moves], "dry_run": dry_run,
                              "unchanged": not plan.moves and target == current}
    if dry_run or report["unchanged"]:
        session.rollback()
        return report
    busy = session.scalar(
        select(func.count()).select_from(DownloadJob)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga_id, DownloadJob.status == JobStatus.DOWNLOADING)
    ) or 0
    if busy:
        session.rollback()
        raise LayoutBusy(f"A chapter of “{manga.folder_name}” is downloading; try again later")
    if only_volume_changes:
        event = ("renamed_chapters", "Volume assigned on MangaDex: {n} chapter folder(s) renamed")
    else:
        event = ("renamed_chapters", "Chapter folders renamed to the current naming rules: {n} folder(s) moved")
    return apply_layout(session, root, manga, target, leaf_for=leaf_for, event=event)


def rename_after_volume_changes(manga_id: uuid.UUID) -> int:
    """Best effort after a scan / feed sync: rename folders whose volume MangaDex just set.

    Never raises: a busy manga or an unavailable library is simply retried after the
    next scan.
    """
    from app.db import session_scope
    from app.library.storage import require_library
    from app.settings_service import load_settings

    try:
        with session_scope() as s:
            settings = load_settings(s)
            if not settings.rename_on_volume_change:
                return 0
            candidates = [r for r in s.scalars(select(ChapterRelease).where(
                ChapterRelease.manga_id == manga_id, ChapterRelease.folder_rel_path.is_not(None),
                ChapterRelease.local_status != LocalStatus.MISSING_ON_DISK))
                if volume_out_of_date(r, settings.volume_placeholder)]
        if not candidates:
            return 0
        root = require_library()
        with session_scope() as s:
            report = rename_chapter_folders(s, root, manga_id, only_volume_changes=True)
        moved = len(report.get("moves") or [])
        if moved:
            log.info("chapter_folders_renamed_for_volume", manga_id=str(manga_id), moved=moved)
        return moved
    except LayoutBusy:
        return 0
    except Exception:  # noqa: BLE001  (never fail the scan for a rename)
        log.warning("volume_rename_failed", manga_id=str(manga_id), exc_info=True)
        return 0


# --------------------------------------------------------------------- group suffixes


def group_suffix_ids(session: Session, root: Path, manga_id: uuid.UUID,
                     extra: ChapterRelease | None = None) -> set[uuid.UUID]:
    """Releases whose chapter folder needs " - {Group}": the same chapter is in the library
    (or queued) from more than one group, in the same language.

    "Same chapter" is the logical chapter (series number, or volume + chapter for
    titles numbered per volume). Two releases of the *same* group keep the usual
    ``[uuid]`` collision suffix instead.
    """
    releases, _ = _relevant_releases(session, root, manga_id)
    if extra is not None and all(r.id != extra.id for r in releases):
        releases.append(extra)
    buckets: dict[tuple[str, int], list[ChapterRelease]] = {}
    for r in releases:
        if r.logical_chapter_id is not None:
            buckets.setdefault((r.language_code, r.logical_chapter_id), []).append(r)
    out: set[uuid.UUID] = set()
    for rs in buckets.values():
        if len({group_folder_name(r.group_names or []).casefold() for r in rs}) > 1:
            out.update(r.id for r in rs)
    return out


def ensure_group_suffixes(session: Session, root: Path, release_id: uuid.UUID, job_id: int | None) -> int:
    """Before downloading ``release``: add " - {Group}" to chapter folders already on disk
    that now share their chapter with another group. Commits; returns folders renamed.
    """
    from app.services.downloader import desired_chapter_folder_name
    from app.services.rename import _lock_exclusive
    from app.settings_service import load_settings

    settings = load_settings(session)
    if not settings.group_in_chapter_name:
        return 0
    release = session.get(ChapterRelease, release_id)
    _lock_exclusive(session, release.manga_id)
    manga = session.get(Manga, release.manga_id, with_for_update=True)
    suffixed = group_suffix_ids(session, root, manga.id, release)

    def leaf_for(r: ChapterRelease) -> str | None:
        if r.id not in suffixed or r.id == release.id:
            return None
        if name_ends_with_group(r.folder_rel_path.rsplit("/", 1)[-1], r.group_names):
            return None
        return desired_chapter_folder_name(r, settings, True)

    current = Layout.from_json(manga.folder_layout)
    plan = _plan(session, root, manga, current, leaf_for)
    if not plan.moves:
        session.rollback()
        return 0
    busy = session.scalar(
        select(func.count()).select_from(DownloadJob)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga.id, DownloadJob.status == JobStatus.DOWNLOADING,
               DownloadJob.id != (job_id or -1))
    ) or 0
    if busy:
        raise LayoutBusy("Waiting for the other chapters of this manga to finish before renaming their folders")
    lang = release.language_code
    rep = apply_layout(session, root, manga, current, leaf_for=leaf_for,
                       event=("renamed_chapters", "Same chapter from another group (" + lang + "): "
                              "{n} chapter folder(s) renamed with the group name"))
    return len(rep["moves"])
