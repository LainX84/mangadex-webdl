"""Retry policy: exponential backoff with jitter."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from app.enums import TRANSIENT_ERRORS, ErrorKind


def backoff_seconds(
    attempt: int,
    initial_s: float,
    max_s: float,
    rng: random.Random | None = None,
) -> float:
    """Delay before retry number ``attempt`` (1-based).

    Uses "equal jitter": half of the exponential delay is fixed, half random, so
    retries from many jobs spread out while still growing predictably.
    """
    attempt = max(1, attempt)
    exp = min(max_s, initial_s * (2 ** (attempt - 1)))
    rng = rng or random
    return min(max_s, exp / 2 + rng.uniform(0, exp / 2))


def is_transient(kind: ErrorKind | str | None) -> bool:
    if kind is None:
        return True
    try:
        return ErrorKind(kind) in TRANSIENT_ERRORS
    except ValueError:
        return False


def next_attempt_time(
    attempt: int, initial_s: float, max_s: float, now: datetime | None = None, rng: random.Random | None = None
) -> datetime:
    now = now or datetime.now(UTC)
    return now + timedelta(seconds=backoff_seconds(attempt, initial_s, max_s, rng))
