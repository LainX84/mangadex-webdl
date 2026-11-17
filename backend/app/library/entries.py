"""Chapter entries in the library: a folder of page images, or a single ``.cbz`` file.

With *Chapter CBZ* on (``AppSettings.chapter_cbz``) a finished chapter is stored as
``{Chapter folder name}.cbz`` instead of a folder. The archive holds the pages at its root,
the release marker ``.mdm-release.json`` and a ``ComicInfo.xml``; pages are *stored*
(no deflate: images are already compressed). Folders and CBZ files can be mixed in one
title (e.g. after switching the setting), so everything that handles chapter folders goes
through these helpers: they work on both kinds.

A CBZ is only ever written to a hidden temporary name next to its destination, synced and
renamed into place, so a half-written archive never carries its final name.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import uuid
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from xml.sax.saxutils import escape

from app.library.storage import fsync_dir, move_dir

CBZ_EXT = ".cbz"
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"}
TMP_PREFIX = ".mdm-writing-"


def is_cbz(path: Path | str) -> bool:
    return str(path).lower().endswith(CBZ_EXT)


def split_name(name: str) -> tuple[str, str]:
    """``"Volume 01 Chapter 02.cbz"`` -> ``("Volume 01 Chapter 02", ".cbz")``; folders keep ``""``."""
    if name.lower().endswith(CBZ_EXT):
        return name[: -len(CBZ_EXT)], name[-len(CBZ_EXT):]
    return name, ""


def entry_name(stem: str, as_cbz: bool) -> str:
    return f"{stem}{CBZ_EXT}" if as_cbz else stem


def entry_exists(path: Path) -> bool:
    """A chapter folder, or a chapter CBZ file."""
    try:
        return path.is_dir() or (is_cbz(path) and path.is_file() and not path.is_symlink())
    except OSError:
        return False


def _is_page_name(name: str) -> bool:
    if "/" in name or name.startswith(".") or "." not in name:
        return False
    return name.rsplit(".", 1)[1].lower() in IMAGE_EXTS


# --------------------------------------------------------------------- reading


def cbz_members(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as zf:
        return zf.infolist()


def cbz_read(path: Path, name: str) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                return zf.read(name)
            except KeyError:
                return None
    except (OSError, zipfile.BadZipFile):
        return None


def cbz_page_files(path: Path) -> list[str]:
    """Page images at the root of a chapter CBZ (sorted, like ``list_page_files`` for folders)."""
    try:
        return sorted(i.filename for i in cbz_members(path) if not i.is_dir() and _is_page_name(i.filename))
    except (OSError, zipfile.BadZipFile):
        return []


def is_cbz_file(path: Path) -> bool:
    return is_cbz(path) and not path.is_dir()


def page_sizes(path: Path) -> dict[str, int]:
    """Page file name -> size, for a chapter folder or a chapter CBZ."""
    if is_cbz_file(path):
        try:
            return {i.filename: i.file_size for i in cbz_members(path)
                    if not i.is_dir() and _is_page_name(i.filename)}
        except (OSError, zipfile.BadZipFile):
            return {}
    out = {}
    for entry in path.iterdir():
        if entry.is_file() and _is_page_name(entry.name):
            out[entry.name] = entry.stat().st_size
    return out


def iter_pages(path: Path, names: Iterable[str] | None = None) -> Iterator[tuple[str, bytes]]:
    """(name, bytes) of the pages of a chapter folder or CBZ, in name order."""
    if is_cbz_file(path):
        with zipfile.ZipFile(path) as zf:
            wanted = sorted(names) if names is not None else cbz_page_files(path)
            for n in wanted:
                yield n, zf.read(n)
        return
    wanted = sorted(names) if names is not None else sorted(page_sizes(path))
    for n in wanted:
        yield n, (path / n).read_bytes()


def untouched_cbz(path: Path, expected: dict[str, dict], marker_name: str,
                  ignored: frozenset[str] = frozenset({"comicinfo.xml"})) -> bool:
    """Every page listed in the marker is in the CBZ with the recorded size and SHA-256, and nothing else."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            for i in infos:
                if i.filename == marker_name or i.filename.lower() in ignored:
                    continue
                if i.filename not in expected:
                    return False
            names = {i.filename: i for i in infos}
            for name, m in expected.items():
                info = names.get(name)
                if info is None or info.file_size != m.get("size"):
                    return False
                if hashlib.sha256(zf.read(name)).hexdigest() != m.get("sha256"):
                    return False
    except (OSError, zipfile.BadZipFile):
        return False
    return True


# --------------------------------------------------------------------- writing


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_cbz(dest: Path, members: Iterable[tuple[str, bytes | Path]]) -> int:
    """Write a stored ZIP at ``dest`` (must not exist) atomically. Returns its size.

    ``members``: (name inside the archive, bytes or a file to copy). The archive is
    written to a hidden temporary file next to ``dest``, tested, synced and renamed.
    """
    if dest.exists():
        raise FileExistsError(errno.EEXIST, "Destination already exists", str(dest))
    tmp = dest.with_name(f"{TMP_PREFIX}{uuid.uuid4().hex[:12]}{CBZ_EXT}")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for name, data in members:
                if isinstance(data, Path):
                    zf.write(data, name)
                else:
                    zf.writestr(name, data)
        with zipfile.ZipFile(tmp) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise OSError(errno.EIO, f"CBZ verification failed at {bad}", str(tmp))
        _fsync_file(tmp)
        if dest.exists():
            raise FileExistsError(errno.EEXIST, "Destination already exists", str(dest))
        os.rename(tmp, dest)
        fsync_dir(dest.parent)
        return dest.stat().st_size
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def folder_to_cbz(folder: Path, dest: Path, extra: dict[str, bytes] | None = None) -> int:
    """Pack the files of a chapter folder (pages + marker) into ``dest``; ``extra`` adds members."""
    files = sorted(e for e in folder.iterdir() if e.is_file() and not e.name.endswith(".tmp"))
    members: list[tuple[str, bytes | Path]] = [(e.name, e) for e in files]
    members += sorted((extra or {}).items())
    return write_cbz(dest, members)


def cbz_replace_member(path: Path, name: str, data: bytes) -> None:
    """Replace (or add) one member of a CBZ: the archive is rewritten to a temp file and swapped in."""
    tmp = path.with_name(f"{TMP_PREFIX}{uuid.uuid4().hex[:12]}{CBZ_EXT}")
    try:
        with zipfile.ZipFile(path) as src, \
                zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as dst:
            for info in src.infolist():
                if info.filename == name:
                    continue
                dst.writestr(info, src.read(info.filename))
            dst.writestr(name, data)
        _fsync_file(tmp)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def move_entry(src: Path, dst: Path) -> None:
    """Move a chapter folder or chapter CBZ (``dst`` must not exist); atomic on one filesystem."""
    if src.is_dir():
        move_dir(src, dst)
        return
    if dst.exists():
        raise FileExistsError(errno.EEXIST, "Destination already exists", str(dst))
    try:
        os.rename(src, dst)
        fsync_dir(dst.parent)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = dst.with_name(f".mdm-incoming-{uuid.uuid4().hex[:12]}{dst.suffix}")
    try:
        shutil.copy2(src, tmp)
        _fsync_file(tmp)
        os.rename(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(dst.parent)
    src.unlink()


def remove_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


# --------------------------------------------------------------------- ComicInfo.xml


def comic_info_xml(fields: dict[str, object]) -> bytes:
    """A ComicInfo.xml (Anansi schema 2.0, read by Komga, Kavita, Mihon, …). Empty values are left out."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'xmlns:xsd="http://www.w3.org/2001/XMLSchema">']
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"  <{key}>{escape(str(value))}</{key}>")
    lines.append("</ComicInfo>")
    return ("\n".join(lines) + "\n").encode("utf-8")
