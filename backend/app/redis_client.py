from __future__ import annotations

from functools import lru_cache

import redis

from app.config import get_config


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_config().redis_url,
        socket_timeout=15,
        socket_connect_timeout=5,
        health_check_interval=30,
        decode_responses=False,
    )


def reset_redis() -> None:
    get_redis.cache_clear()
