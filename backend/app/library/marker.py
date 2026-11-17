"""Hidden marker files that tie library folders to MangaDex identities.

``.mdm-release.json`` inside each chapter folder records the chapter UUID and the
original page manifest, so the library can be recognised even with an empty
database and manual edits can be detected (and left alone).
``.mdm-manga.json`` at the top of a manga folder records the manga UUID.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.library.storage import atomic_write_bytes

RELEASE_MARKER = ".mdm-release.json"
MANGA_MARKER = ".mdm-manga.json"
PARTIAL_PREFIX = ".mdm-partial-"  # legacy: partial downloads inside the library (before the staging folder)


def is_legacy_partial(rel: str | None) -> bool:
    """A job temp path inside the library (``{Manga}/…/.mdm-partial-{uuid}``).

    New jobs use a flat folder in the staging directory (the chapter UUID, no ``/``).
    """
    return bool(rel) and "/" in rel and rel.rsplit("/", 1)[-1].startswith(PARTIAL_PREFIX)


def write_release_marker(folder: Path, data: dict[str, Any]) -> None:
    payload = {"schema": 1, "written_at": datetime.now(UTC).isoformat(), **data}
    atomic_write_bytes(folder / RELEASE_MARKER, json.dumps(payload, indent=2, ensure_ascii=False).encode())


def read_release_marker(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / RELEASE_MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("chapter_id") else None
    except (OSError, ValueError):
        return None


def write_manga_marker(folder: Path, manga_id: str, title: str) -> None:
    payload = {"schema": 1, "manga_id": manga_id, "title": title}
    atomic_write_bytes(folder / MANGA_MARKER, json.dumps(payload, indent=2, ensure_ascii=False).encode())


def read_manga_marker(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / MANGA_MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("manga_id") else None
    except (OSError, ValueError):
        return None


IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"}


def list_page_files(folder: Path) -> list[str]:
    out = []
    for entry in folder.iterdir():
        if entry.is_file() and not entry.name.startswith("."):
            ext = entry.suffix.lower().lstrip(".")
            if ext in IMAGE_EXTS:
                out.append(entry.name)
    return sorted(out)
