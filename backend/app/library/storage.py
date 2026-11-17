"""Library root safety: mount checks, sentinel file, contained paths, atomic writes."""

from __future__ import annotations

import errno
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import get_config

# errno values that mean "the storage is gone/unreachable", not "this file is bad".
STORAGE_UNAVAILABLE_ERRNOS = {
    errno.ENOENT,
    errno.EIO,
    errno.ENOTCONN,
    errno.ESTALE,
    errno.EHOSTDOWN,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
    errno.ENODEV,
}
# errno values that need a human (permissions, full disk): never auto-retried quickly.
STORAGE_FATAL_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC, errno.EDQUOT}


class StorageUnavailable(Exception):
    """The library destination cannot be used right now (NAS down, not mounted, ...)."""


class UnsafePath(Exception):
    """A computed path would escape the library root."""


@dataclass
class MountStatus:
    ok: bool
    path: str
    exists: bool = False
    is_dir: bool = False
    sentinel_present: bool = False
    fs_type: str | None = None
    fs_type_ok: bool = True
    writable: bool = False
    free_bytes: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def mount_fs_type(path: Path, mountinfo: str = "/proc/self/mountinfo") -> str | None:
    """Filesystem type of the mount containing ``path`` (longest mountpoint match)."""
    try:
        target = os.path.realpath(path)
        best, best_type = "", None
        with open(mountinfo, encoding="utf-8") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                fields = left.split()
                if len(fields) < 5:
                    continue
                mnt = fields[4].replace("\\040", " ")
                if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best = mnt
                    best_type = right.split()[0] if right else None
        return best_type
    except OSError:
        return None


def sentinel_path(root: Path | None = None) -> Path:
    """Where the sentinel lives: ``MDM_SENTINEL_DIR`` if set, else the library root."""
    cfg = get_config()
    base = Path(cfg.sentinel_dir) if cfg.sentinel_dir else Path(root or cfg.downloads_root)
    return base / cfg.library_sentinel


def _sentinel_found(root: Path) -> tuple[bool, str | None]:
    """(present, error). The configured location counts; the legacy one in the library root too."""
    cfg = get_config()
    path = sentinel_path(root)
    legacy = root / cfg.library_sentinel
    if path.is_file():
        if path.parent != root and os.stat(path.parent).st_dev != os.stat(root).st_dev:
            return False, (f"Sentinel folder {path.parent} is not on the same filesystem as the library {root}: "
                           "is the NAS mounted?")
        return True, None
    if path != legacy and legacy.is_file():
        return True, None  # older location, still accepted (init-library moves it)
    return False, (f"Library sentinel '{path}' not found – the NAS share is probably not mounted "
                   "(or the library has not been initialised yet)")


def check_library(root: Path | None = None, write_probe: bool = True) -> MountStatus:
    """Verify the library root is the real (mounted) library and writable.

    Downloads only start if:
    1. the root exists and is a directory;
    2. the sentinel file (``.mdm-library``) exists in it – an unmounted NFS
       mountpoint is an empty local directory and will not contain it;
    3. the filesystem type matches ``MDM_EXPECTED_FS_TYPES`` when configured;
    4. a probe file can be created, fsync'd and removed.
    """
    cfg = get_config()
    root = Path(root or cfg.downloads_root)
    st = MountStatus(ok=False, path=str(root))
    try:
        st.exists = root.exists()
        st.is_dir = root.is_dir()
        if not st.is_dir:
            st.error = "Library directory does not exist"
            return st
        st.sentinel_present, sentinel_error = _sentinel_found(root)
        st.fs_type = mount_fs_type(root)
        expected = cfg.expected_fs_type_set
        st.fs_type_ok = not expected or (st.fs_type in expected)
        if not st.sentinel_present:
            st.error = sentinel_error
            return st
        if not st.fs_type_ok:
            st.error = f"Library filesystem is '{st.fs_type}', expected one of {sorted(expected)}"
            return st
        if write_probe:
            probe = root / f".mdm-probe-{uuid.uuid4().hex}"
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, b"ok")
                os.fsync(fd)
            finally:
                os.close(fd)
                os.unlink(probe)
            st.writable = True
        vfs = os.statvfs(root)
        st.free_bytes = vfs.f_bavail * vfs.f_frsize
        st.ok = True
        return st
    except OSError as exc:
        st.error = f"Library not accessible: {exc.strerror or exc}"
        return st


def initialize_library(root: Path | None = None) -> MountStatus:
    """Create the sentinel file. Refuses when the fs type does not match expectations."""
    cfg = get_config()
    root = Path(root or cfg.downloads_root)
    if not root.is_dir():
        raise StorageUnavailable("Library directory does not exist")
    expected = cfg.expected_fs_type_set
    fs_type = mount_fs_type(root)
    if expected and fs_type not in expected:
        raise StorageUnavailable(
            f"Refusing to initialise: filesystem is '{fs_type}', expected {sorted(expected)}. Is the NAS mounted?"
        )
    sentinel = sentinel_path(root)
    if not sentinel.parent.is_dir():
        raise StorageUnavailable(f"Sentinel folder {sentinel.parent} does not exist")
    if sentinel.parent != root and os.stat(sentinel.parent).st_dev != os.stat(root).st_dev:
        raise StorageUnavailable(f"Sentinel folder {sentinel.parent} is not on the same filesystem as the library")
    if not sentinel.exists():
        atomic_write_bytes(sentinel, json.dumps({"app": "mangadex-download-manager", "version": 1}).encode())
    legacy = root / cfg.library_sentinel
    if legacy != sentinel and legacy.is_file():
        legacy.unlink()  # moved to the configured folder
    return check_library(root)


def require_library(root: Path | None = None) -> Path:
    st = check_library(root)
    if not st.ok:
        raise StorageUnavailable(st.error or "Library unavailable")
    return Path(st.path)


def safe_join(root: Path, *parts: str) -> Path:
    """Join path components under ``root``; refuse anything that escapes it."""
    root_real = Path(os.path.realpath(root))
    for p in parts:
        if not p or p in {".", ".."} or "/" in p or "\\" in p or "\x00" in p:
            raise UnsafePath(f"Unsafe path component: {p!r}")
    candidate = root_real.joinpath(*parts)
    resolved = Path(os.path.realpath(candidate))
    if os.path.commonpath([str(root_real), str(resolved)]) != str(root_real):
        raise UnsafePath("Path escapes the library root")
    return candidate


def resolve_rel(root: Path, rel: str) -> Path:
    """Resolve a stored relative path (``a/b/c``) safely under ``root``."""
    parts = [p for p in rel.split("/") if p]
    return safe_join(root, *parts)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + fsync + rename)."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.replace(tmp, path)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def is_storage_error(exc: BaseException) -> bool:
    """True when the library is (temporarily) unreachable – pause and resume later."""
    return isinstance(exc, StorageUnavailable) or (
        isinstance(exc, OSError) and exc.errno in STORAGE_UNAVAILABLE_ERRNOS
    )


def is_fatal_storage_error(exc: BaseException) -> bool:
    """True for permission / read-only / disk-full errors that need a human."""
    return isinstance(exc, OSError) and exc.errno in STORAGE_FATAL_ERRNOS


# ----------------------------------------------------------- working directories


def staging_root(library_root: Path | None = None) -> Path:
    """Where downloads in progress live (``MDM_STAGING_ROOT``)."""
    cfg = get_config()
    if cfg.staging_root:
        return Path(cfg.staging_root)
    return Path(library_root or cfg.downloads_root) / ".mdm-staging"


def replaced_root(library_root: Path | None = None) -> Path:
    """Where previous copies of re-downloaded chapters are kept (``MDM_REPLACED_ROOT``)."""
    cfg = get_config()
    if cfg.replaced_root:
        return Path(cfg.replaced_root)
    return Path(library_root or cfg.downloads_root) / ".mdm-replaced"


def ensure_work_dir(path: Path) -> Path:
    """Create (if missing) and check a working directory of the app.

    Only called after the library check passed. The directory itself may be
    created, never its parents: if the NAS share disappeared, its parent is an
    empty local mountpoint and nothing is written there.
    """
    cfg = get_config()
    try:
        path.mkdir(exist_ok=True)
    except FileNotFoundError as exc:
        raise StorageUnavailable(f"Cannot create {path}: its parent folder does not exist") from exc
    if not path.is_dir():
        raise StorageUnavailable(f"{path} is not a directory")
    expected = cfg.expected_fs_type_set
    if expected:
        fs_type = mount_fs_type(path)
        if fs_type not in expected:
            raise StorageUnavailable(f"{path} is on '{fs_type}', expected one of {sorted(expected)}")
    return path


def work_dir_status(path: Path) -> dict:
    out = {"path": str(path), "exists": False, "fs_type": None, "entries": None, "error": None}
    try:
        out["exists"] = path.is_dir()
        out["fs_type"] = mount_fs_type(path) if out["exists"] else None
        if out["exists"]:
            out["entries"] = sum(1 for e in path.iterdir() if not e.name.startswith("."))
    except OSError as exc:
        out["error"] = exc.strerror or str(exc)
    return out


def _copy_fsync(src: str, dst: str) -> str:
    shutil.copy2(src, dst)
    fd = os.open(dst, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return dst


def move_dir(src: Path, dst: Path) -> None:
    """Move the directory ``src`` to ``dst`` (which must not exist).

    On one filesystem (same mount) this is a single atomic rename. Across
    filesystems the tree is copied to a hidden sibling of ``dst``, synced, renamed
    into place and only then removed from ``src``, so ``dst`` never appears half-filled.
    """
    if dst.exists():
        raise FileExistsError(errno.EEXIST, "Destination already exists", str(dst))
    try:
        os.rename(src, dst)
        fsync_dir(dst.parent)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = dst.with_name(f".mdm-incoming-{uuid.uuid4().hex[:12]}")
    try:
        shutil.copytree(src, tmp, copy_function=_copy_fsync)
        fsync_dir(tmp)
        os.rename(tmp, dst)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    fsync_dir(dst.parent)
    shutil.rmtree(src)
