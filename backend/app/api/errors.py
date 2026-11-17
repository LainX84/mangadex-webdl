from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status

from app.library.storage import StorageUnavailable
from app.mangadex.errors import (
    AccessDeniedError,
    MaintenanceError,
    MangaDexError,
    NotFoundError,
    RateLimitedError,
)
from app.ratelimit import AccessBlocked, CooldownActive, SlotTimeout


@contextmanager
def mangadex_errors() -> Iterator[None]:
    """Translate MangaDex/rate-limit failures into helpful HTTP errors for the UI."""
    try:
        yield
    except CooldownActive as exc:
        secs = int(exc.seconds_remaining) + 1
        if exc.is_maintenance:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"MangaDex is down for maintenance; next check in {secs}s.",
                headers={"Retry-After": str(secs)},
            ) from exc
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"MangaDex requests are cooling down after rate limiting; try again in {secs}s.",
            headers={"Retry-After": str(secs)},
        ) from exc
    except AccessBlocked as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"MangaDex access is restricted: {exc}. See Settings → System.") from exc
    except MaintenanceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "MangaDex is down for maintenance; try again later.") from exc
    except RateLimitedError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Rate limited by MangaDex; try again later.") from exc
    except AccessDeniedError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found on MangaDex") from exc
    except SlotTimeout as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MangaDex request queue is busy; try again.") from exc
    except MangaDexError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"MangaDex error: {exc}") from exc
    except StorageUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
