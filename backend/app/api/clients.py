from __future__ import annotations

import threading

from app.mangadex.client import MangaDexClient

_lock = threading.Lock()
_client: MangaDexClient | None = None


def get_client() -> MangaDexClient:
    """FastAPI dependency: one shared MangaDex client (rate limits are global anyway)."""
    global _client
    with _lock:
        if _client is None:
            from app.services.control import build_client

            _client = build_client()
        return _client


def reset_client() -> None:
    global _client
    with _lock:
        _client = None
