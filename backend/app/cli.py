"""Command line administration (``mdm ...`` inside the container)."""

from __future__ import annotations

import getpass
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import click
from sqlalchemy import func, select, update

from app.config import get_config
from app.db import session_scope
from app.logging_setup import configure_logging, get_logger

log = get_logger("cli")


def bootstrap_admin_from_env() -> bool:
    """Create the first admin from MDM_ADMIN_USERNAME + MDM_ADMIN_PASSWORD_FILE (Docker secret).

    Only runs when no user exists; never overwrites an existing account.
    """
    from app.api.security import hash_password, validate_password_strength
    from app.models import User

    cfg = get_config()
    username, password = cfg.admin_username, cfg.admin_password
    if not username or not password:
        return False
    with session_scope() as s:
        if (s.scalar(select(func.count()).select_from(User)) or 0) > 0:
            return False
        validate_password_strength(password, username)
        s.add(User(username=username, password_hash=hash_password(password), is_admin=True,
                   password_changed_at=datetime.now(UTC)))
    log.info("admin_bootstrapped_from_secret", username=username)
    return True


def _read_password(password_file: str | None, confirm: bool = True) -> str:
    if password_file:
        with open(password_file, encoding="utf-8") as fh:
            return fh.read().strip()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    pw = getpass.getpass("New password: ")
    if confirm and getpass.getpass("Repeat password: ") != pw:
        raise click.ClickException("Passwords do not match")
    return pw


@click.group()
def cli() -> None:
    cfg = get_config()
    configure_logging(cfg.log_level, json=False)


@cli.command("init-db")
def init_db() -> None:
    """Run migrations and seed reference data (safe to run repeatedly)."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from app.languages import seed_languages

    for attempt in range(30):
        try:
            command.upgrade(AlembicConfig(_alembic_ini()), "head")
            break
        except Exception as exc:  # database still starting
            if attempt == 29:
                raise
            click.echo(f"Database not ready ({exc.__class__.__name__}); retrying...")
            time.sleep(2)
    with session_scope() as s:
        added = seed_languages(s)
    click.echo(f"Migrations applied; {added} language(s) added.")


def _alembic_ini() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent / "alembic.ini")


@cli.command("create-admin")
@click.argument("username")
@click.option("--password-file", type=click.Path(exists=True, dir_okay=False))
def create_admin(username: str, password_file: str | None) -> None:
    """Create an administrator account."""
    from app.api.security import hash_password, validate_password_strength
    from app.models import User

    pw = _read_password(password_file)
    try:
        validate_password_strength(pw, username)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with session_scope() as s:
        if s.scalar(select(User).where(func.lower(User.username) == username.lower())):
            raise click.ClickException("User already exists (use reset-password)")
        s.add(User(username=username, password_hash=hash_password(pw), is_admin=True,
                   password_changed_at=datetime.now(UTC)))
    click.echo(f"Administrator '{username}' created.")


@cli.command("reset-password")
@click.argument("username")
@click.option("--password-file", type=click.Path(exists=True, dir_okay=False))
def reset_password(username: str, password_file: str | None) -> None:
    """Reset a user's password, unlock it and sign out all of its sessions."""
    from sqlalchemy import delete

    from app.api.security import hash_password, revoke_user_sessions, validate_password_strength
    from app.models import LoginAttempt, User

    pw = _read_password(password_file)
    try:
        validate_password_strength(pw, username)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with session_scope() as s:
        user = s.scalar(select(User).where(func.lower(User.username) == username.lower()))
        if user is None:
            raise click.ClickException("No such user")
        user.password_hash = hash_password(pw)
        user.password_changed_at = datetime.now(UTC)
        user.is_active = True
        revoke_user_sessions(s, user.id)
        s.execute(delete(LoginAttempt).where(LoginAttempt.username == username.lower()))
    click.echo(f"Password for '{username}' reset; all sessions revoked and lockouts cleared.")


@cli.command("list-users")
def list_users() -> None:
    from app.models import User

    with session_scope() as s:
        for u in s.scalars(select(User).order_by(User.id)):
            click.echo(f"{u.id}\t{u.username}\tadmin={u.is_admin}\tactive={u.is_active}\tlast_login={u.last_login_at}")


@cli.command("check-library")
def check_library_cmd() -> None:
    """Show whether the library mount is usable."""
    from app.library.storage import check_library

    st = check_library()
    click.echo(json.dumps(st.to_dict(), indent=2))
    sys.exit(0 if st.ok else 1)


@cli.command("init-library")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
def init_library_cmd(yes: bool) -> None:
    """Create the library sentinel file (only while the NAS share is mounted!)."""
    from app.library.storage import StorageUnavailable, initialize_library, mount_fs_type

    cfg = get_config()
    from app.library.storage import sentinel_path

    click.echo(f"Library root: {cfg.downloads_root} (filesystem: {mount_fs_type(cfg.downloads_root)})")
    click.echo(f"Sentinel:     {sentinel_path()}")
    if not yes:
        click.confirm("Is the NAS share mounted at this path right now?", abort=True)
    try:
        st = initialize_library()
    except StorageUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(st.to_dict(), indent=2))


@cli.command("library-check")
@click.option("--now", "run_now", is_flag=True, help="Queue the check now instead of only showing the status")
def library_check_cmd(run_now: bool) -> None:
    """Weekly library check: show the schedule and last result, or queue it now (--now)."""
    from app.services.library_check import status as check_status
    from app.tasks import enqueue_library_scan

    if run_now:
        click.echo("queued" if enqueue_library_scan(None, queue_missing=False) else "already queued or running")
    with session_scope() as s:
        click.echo(json.dumps(check_status(s), indent=2))


@cli.command("clear-access-block")
def clear_access_block_cmd() -> None:
    """Acknowledge a MangaDex access restriction (after resolving the cause)."""
    from app.services.control import clear_access_block
    from app.services.jobs import resume_blocked_jobs

    with session_scope() as s:
        clear_access_block(s)
        n = resume_blocked_jobs(s)
    click.echo(f"Access block cleared; {n} job(s) resumed.")


@cli.command("health")
@click.argument("component", type=click.Choice(["api", "worker", "scheduler"]))
def health(component: str) -> None:
    """Container health check."""
    if component == "api":
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as r:  # noqa: S310
                sys.exit(0 if r.status == 200 else 1)
        except Exception:
            sys.exit(1)
    from app.services.control import last_heartbeat

    try:
        hb = last_heartbeat(component)
    except Exception:
        sys.exit(1)
    sys.exit(0 if hb and time.time() - hb < 90 else 1)



@cli.command("import-external")
@click.argument("plan_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--source", default="jam", show_default=True, help="Label stored with the owned chapters")
@click.option("--dry-run", is_flag=True, help="Only report what would be imported")
@click.option("--limit", type=int, default=None, help="Import at most this many new titles")
def import_external_cmd(plan_file: str, source: str, dry_run: bool, limit: int | None) -> None:
    """Follow titles already owned elsewhere (e.g. JAM) without re-downloading them.

    PLAN_FILE is a JSON list of {"id", "languages", "start_chapter", "external": [[release_id, lang, chapter], ...]}.
    Idempotent: titles already in the library are skipped.
    """
    from app.services.external_import import import_plan

    with open(plan_file, encoding="utf-8") as fh:
        plan = json.load(fh)
    report = import_plan(plan, source=source, dry_run=dry_run, limit=limit,
                         progress=lambda msg: click.echo(msg, err=True))
    click.echo(json.dumps(report, indent=2, ensure_ascii=False))


@cli.command("update-tiers")
@click.option("--list", "show", is_flag=True, help="Print every title with its level")
def update_tiers_cmd(show: bool) -> None:
    """Recompute the adaptive monitoring level (Lv.1/2/3) of every title."""
    from sqlalchemy import select as _select
    from sqlalchemy.orm import selectinload as _sel

    from app.models import Manga as _Manga
    from app.services.tiers import effective_interval, refresh_tiers
    from app.settings_service import load_settings as _load

    with session_scope() as s:
        report = refresh_tiers(s)
        if show:
            settings = _load(s)
            for m in s.scalars(_select(_Manga).options(_sel(_Manga.monitoring)).order_by(_Manga.title)):
                c = m.monitoring
                if c is None:
                    continue
                last = c.last_chapter_at.date().isoformat() if c.last_chapter_at else "-"
                prev = c.prev_chapter_at.date().isoformat() if c.prev_chapter_at else "-"
                done = "completed" if c.upload_complete else ""
                click.echo(f"Lv.{c.update_tier}\t{effective_interval(c, settings) / 60:g}h\t{last}\t{prev}\t"
                           f"{done}\t{m.display_title}")
    click.echo(json.dumps(report))


@cli.command("apply-group-exclusions")
def apply_group_exclusions_cmd() -> None:
    """Cancel queued jobs for excluded groups / official publisher links and queue alternatives."""
    from app.db import session_scope
    from app.services.monitor import apply_group_exclusions

    with session_scope() as s:
        report = apply_group_exclusions(s)
    click.echo(json.dumps(report, indent=2))


@cli.command("follow-sync")
@click.argument("kind", type=click.Choice(["list", "feed", "status"]), default="status")
def follow_sync_cmd(kind: str) -> None:
    """MangaDex account: show status, or run the followed-list / followed-feed sync now."""
    from app.services.follows import account_status, sync_follow_feed, sync_follow_list

    if kind == "status":
        with session_scope() as s:
            click.echo(json.dumps(account_status(s), indent=2, default=str))
        return
    from app.services.control import build_client

    client = build_client()
    result = sync_follow_list(client) if kind == "list" else sync_follow_feed(client)
    click.echo(json.dumps(result, indent=2, default=str))


@cli.command("archive")
@click.argument("action", type=click.Choice(["index", "status", "check", "auto-approve"]), default="status")
@click.option("--title", default=None, help="check: show what the archive has for this title")
@click.option("--apply", "do_apply", is_flag=True, help="auto-approve: approve them (default: only show the plan)")
def archive_cmd(action: str, title: str | None, do_apply: bool) -> None:
    """Read-only comics archive: rebuild the index, show its status, or check a title.

    auto-approve: titles waiting for approval that are already in the archive (by romaji or
    English title) are approved, continuing from the archive. Without --apply it only shows
    what would be approved and what stays in Approvals (and why).
    """
    from sqlalchemy import select as _select

    from app.models import ArchiveFolder
    from app.services import archive as arc

    if action == "auto-approve":
        from app.services import auto_approve
        from app.services.control import build_client

        with session_scope() as s:
            r = auto_approve.run(s, build_client(), None, do_apply=do_apply)
        for e in r["entries"]:
            if e["action"] == "approve":
                start = "new chapters only" if e.get("only_new") else f"from ch. {auto_approve._fmt(e['start_chapter'])}"
                extra = f"; removed from MangaDex, folder “{e['title_override']}”" if e.get("removed") else ""
                click.echo(f"{'APPROVED' if do_apply else 'approve '}  {e['language']}  {e['title'][:60]}  "
                           f"(archive up to ch. {auto_approve._fmt(e['last_chapter'])}; {start}{extra})")
        for e in r["entries"]:
            if e["action"] == "skip":
                click.echo(f"keep      {e['title'][:60]}  — {e['reason']}")
        click.echo(f"\n{r['approved'] if do_apply else r['would_approve']} {'approved' if do_apply else 'to approve'}, "
                   f"{r['skipped']} left in Approvals" + ("" if do_apply else " (dry run: add --apply)"))
        return
    with session_scope() as s:
        if action == "index":
            click.echo(json.dumps(arc.rebuild_index(s), indent=2, default=str))
        elif action == "status":
            click.echo(json.dumps({"root": str(arc.archive_root()), **arc.index_state(s)}, indent=2, default=str))
        else:
            if not title:
                raise click.ClickException("--title is required")
            root = arc.archive_root()
            rows = s.scalars(_select(ArchiveFolder).where(ArchiveFolder.title_norm == arc.norm_title(title)))
            for row in rows:
                click.echo(json.dumps({"folder": row.rel_path, **arc.folder_analysis(s, row, root).to_dict()},
                                      ensure_ascii=False, default=str))


@cli.command("chapter-numbering")
@click.option("--manga", "manga_id", default=None, help="Only this manga (UUID); default: every title")
@click.option("--set", "mode", type=click.Choice(["volume", "series"]), default=None,
              help="Force the numbering of --manga instead of detecting it")
@click.option("--apply", "do_apply", is_flag=True, help="Save the changes (default: only show them)")
def chapter_numbering_cmd(manga_id: str | None, mode: str | None, do_apply: bool) -> None:
    """Detect titles whose chapter numbering restarts every volume (anthologies) and re-key them.

    Their chapters are then identified as volume + chapter, so owning "Vol.3 Ch.2" no
    longer hides "Vol.15 Ch.2". Files and folders are not touched.
    """
    import uuid as _uuid

    from app.models import Manga
    from app.services.numbering import apply_numbering, detect_per_volume

    if mode and not manga_id:
        raise click.UsageError("--set needs --manga")
    with session_scope() as s:
        ids = [_uuid.UUID(manga_id)] if manga_id else list(s.scalars(select(Manga.id).order_by(Manga.title)))
    changed = 0
    for mid in ids:
        with session_scope() as s:
            m = s.get(Manga, mid)
            if m is None:
                raise click.ClickException(f"No manga {mid}")
            want = (mode == "volume") if mode else (m.chapters_per_volume or detect_per_volume(s, mid))
            if want == m.chapters_per_volume and not mode:
                continue
            rep = apply_numbering(s, m, want)
            if do_apply:
                from app.services.control import record_event

                record_event(s, "numbering", "Chapter numbering set to "
                             + ("volume + chapter" if want else "series-wide chapter numbers"), manga_id=mid)
            else:
                s.rollback()
            changed += 1
            click.echo(json.dumps({"manga": str(mid), "title": m.title, **rep}, ensure_ascii=False))
    click.echo(f"{changed} title(s) {'changed' if do_apply else 'would change (dry run, use --apply)'}")


@cli.command("chapter-names")
@click.option("--manga", "manga_id", default=None, help="Only this manga (UUID); default: every title with folders")
@click.option("--keep-groups", is_flag=True, help="Keep existing {Group} folder levels (only rename chapter folders)")
@click.option("--apply", "do_apply", is_flag=True, help="Rename/move the folders (default: only show what would change)")
def chapter_names_cmd(manga_id: str | None, keep_groups: bool, do_apply: bool) -> None:
    """Rename chapter folders in the library to the current naming rules.

    Adds the volume placeholder ("Volume TBD …") and the " - {Group}" suffix to older
    folders and, unless --keep-groups, removes {Group} folder levels that group names
    in the chapter folders make unnecessary. Only folders linked to a release are touched.
    """
    import uuid as _uuid

    from app.library.storage import require_library
    from app.models import ChapterRelease, Manga
    from app.services.layout import LayoutBusy, LayoutConflict, rename_chapter_folders

    root = require_library()
    with session_scope() as s:
        if manga_id:
            ids = [_uuid.UUID(manga_id)]
        else:
            ids = list(s.scalars(select(Manga.id).where(Manga.id.in_(
                select(ChapterRelease.manga_id).where(ChapterRelease.folder_rel_path.is_not(None))))))
    totals = {"titles_changed": 0, "folders_moved": 0, "errors": 0}
    for mid in ids:
        try:
            with session_scope() as s:
                rep = rename_chapter_folders(s, root, mid, dry_run=not do_apply, minimal_layout=not keep_groups)
        except (LayoutBusy, LayoutConflict) as exc:
            totals["errors"] += 1
            click.echo(f"!! {mid}: {exc}", err=True)
            continue
        if rep.get("unchanged"):
            continue
        totals["titles_changed"] += 1
        totals["folders_moved"] += len(rep["moves"])
        click.echo(f"{'RENAMED' if do_apply else 'WOULD RENAME'} {rep['manga']}: {len(rep['moves'])} folder(s)"
                   + (f", layout {rep['from']} -> {rep['to']}" if rep["from"] != rep["to"] else ""))
        for m in rep["moves"][:3]:
            click.echo(f"    {m['from']}  ->  {m['to']}")
        if len(rep["moves"]) > 3:
            click.echo(f"    ... {len(rep['moves']) - 3} more")
    click.echo(f"{totals['titles_changed']} title(s), {totals['folders_moved']} folder(s)"
               f"{'' if do_apply else ' (dry run: add --apply)'}; {totals['errors']} error(s)")


@cli.command("folder-layout")
@click.option("--manga", "manga_id", default=None, help="Only this manga (UUID); default: every title with folders")
@click.option("--apply", "do_apply", is_flag=True, help="Move the folders (default: only show what would change)")
def folder_layout_cmd(manga_id: str | None, do_apply: bool) -> None:
    """Recompute the minimal folder layout of existing titles and (with --apply) move their folders.

    The {Language} level is kept only when a title has more than one language, the
    {Group} level only when a language has chapters from more than one scanlation group.
    """
    import uuid as _uuid

    from app.library.storage import require_library
    from app.models import ChapterRelease, Manga
    from app.services.layout import LayoutBusy, LayoutConflict, recompute_layout

    root = require_library()
    with session_scope() as s:
        if manga_id:
            ids = [_uuid.UUID(manga_id)]
        else:
            ids = list(s.scalars(select(Manga.id).where(Manga.id.in_(
                select(ChapterRelease.manga_id).where(ChapterRelease.folder_rel_path.is_not(None))))))
    totals = {"titles_changed": 0, "folders_moved": 0, "errors": 0}
    for mid in ids:
        try:
            with session_scope() as s:
                rep = recompute_layout(s, root, mid, dry_run=not do_apply)
        except (LayoutBusy, LayoutConflict) as exc:
            totals["errors"] += 1
            click.echo(f"!! {mid}: {exc}", err=True)
            continue
        if rep.get("unchanged"):
            continue
        totals["titles_changed"] += 1
        totals["folders_moved"] += len(rep["moves"])
        click.echo(f"{'MOVED' if do_apply else 'WOULD MOVE'} {rep['manga']}: {rep['from']} -> {rep['to']}, "
                   f"{len(rep['moves'])} folder(s)")
        for m in rep["moves"][:3]:
            click.echo(f"    {m['from']}  ->  {m['to']}")
        if len(rep["moves"]) > 3:
            click.echo(f"    ... {len(rep['moves']) - 3} more")
    click.echo(json.dumps({**totals, "applied": do_apply}, indent=2))


@cli.command("staging")
@click.argument("action", type=click.Choice(["status", "tidy", "cleanup"]), default="status")
@click.option("--apply", "do_apply", is_flag=True, help="tidy/cleanup: do it (default: only show what would change)")
def staging_cmd(action: str, do_apply: bool) -> None:
    """Downloads-in-progress and replaced-copies folders (outside the library).

    status   paths and the folders in the staging directory, with their job
    tidy     move what older versions left inside the library out of it:
             .mdm-partial-* folders -> staging, .mdm-replaced/ contents -> replaced folder
    cleanup  delete staging folders whose download is finished, cancelled or gone
    """
    import shutil
    import uuid as _uuid
    from datetime import UTC, datetime

    from app.enums import JobStatus
    from app.library.marker import PARTIAL_PREFIX
    from app.library.storage import (
        ensure_work_dir,
        move_dir,
        replaced_root,
        require_library,
        safe_join,
        staging_root,
    )
    from app.models import DownloadJob

    root = require_library()
    staging, replaced = staging_root(root), replaced_root(root)
    open_statuses = [s.value for s in JobStatus if s not in (JobStatus.COMPLETED, JobStatus.CANCELLED)]

    def open_job(release_id: str) -> DownloadJob | None:
        try:
            rid = _uuid.UUID(release_id)
        except ValueError:
            return None
        with session_scope() as s:
            job = s.scalar(select(DownloadJob).where(DownloadJob.release_id == rid,
                                                     DownloadJob.status.in_(open_statuses))
                           .order_by(DownloadJob.id.desc()).limit(1))
            if job is not None:
                s.expunge(job)
            return job

    if action == "status":
        click.echo(f"Library:  {root}")
        click.echo(f"Staging:  {staging}{'' if staging.is_dir() else ' (not created yet)'}")
        click.echo(f"Replaced: {replaced}{'' if replaced.is_dir() else ' (not created yet)'}")
        if staging.is_dir():
            for d in sorted(e for e in staging.iterdir() if e.is_dir() and not e.name.startswith(".")):
                job = open_job(d.name)
                pages = sum(1 for f in d.iterdir() if f.is_file() and not f.name.startswith("."))
                click.echo(f"  {d.name}  {pages} page(s)  {job.status if job else 'no open job (cleanup removes it)'}")
        return

    if action == "cleanup":
        removed = 0
        if staging.is_dir():
            for d in sorted(e for e in staging.iterdir() if e.is_dir() and not e.name.startswith(".")):
                if open_job(d.name) is not None:
                    continue
                click.echo(f"{'REMOVED' if do_apply else 'WOULD REMOVE'} {d}")
                if do_apply:
                    shutil.rmtree(d)
                removed += 1
            # Old copies of revised chapters whose deletion was interrupted.
            for d in sorted(e for e in staging.iterdir() if e.is_dir() and e.name.startswith(".mdm-discard-")):
                click.echo(f"{'REMOVED' if do_apply else 'WOULD REMOVE'} {d}")
                if do_apply:
                    shutil.rmtree(d)
                removed += 1
        click.echo(json.dumps({"folders": removed, "applied": do_apply}))
        return

    # tidy
    moves: list[tuple[Path, Path, str | None]] = []  # (src, dst, release id to repoint)
    for dirpath, dirnames, _files in os.walk(root):
        here = Path(dirpath)
        for name in list(dirnames):
            src = here / name
            if name.startswith(PARTIAL_PREFIX):
                rid = name[len(PARTIAL_PREFIX):]
                job = open_job(rid)
                if job is not None and job.status == JobStatus.DOWNLOADING:
                    click.echo(f"skip (downloading now): {src.relative_to(root)}")
                else:
                    moves.append((src, staging / rid, rid if job is not None else None))
                dirnames.remove(name)
            elif name == ".mdm-replaced" and here != root:
                rel_parent = here.relative_to(root)
                for item in sorted(src.iterdir()):
                    moves.append((item, replaced.joinpath(rel_parent, item.name), None))
                dirnames.remove(name)
            elif name.startswith("."):
                dirnames.remove(name)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    done = 0
    for src, dst, rid in moves:
        if dst.exists():
            dst = dst.with_name(f"{dst.name} {stamp}")
        click.echo(f"{'MOVED' if do_apply else 'WOULD MOVE'} {src.relative_to(root)}  ->  {dst}")
        if not do_apply:
            continue
        base = staging if dst.is_relative_to(staging) else replaced
        ensure_work_dir(base)
        safe_join(base, *dst.relative_to(base).parts)  # stays inside its folder
        dst.parent.mkdir(parents=True, exist_ok=True)
        move_dir(src, dst)
        done += 1
        if rid is not None:
            with session_scope() as s:
                s.execute(update(DownloadJob).where(DownloadJob.release_id == _uuid.UUID(rid),
                                                    DownloadJob.status.in_(open_statuses))
                          .values(temp_dir_rel=dst.name))
        old = src.parent
        if old.name == ".mdm-replaced":
            try:
                old.rmdir()  # only when empty
            except OSError:
                pass
    click.echo(json.dumps({"found": len(moves), "moved": done, "applied": do_apply}))


if __name__ == "__main__":
    cli()
