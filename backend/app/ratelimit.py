"""Centralised, Redis-backed request scheduling shared by every process.

All outbound MangaDex traffic (API container, every worker, the scheduler) goes
through this module, so limits apply to the deployment as a whole rather than
per process:

* token buckets (rate + minimum interval) per traffic class: ``api``,
  ``at_home``, ``image``, ``cover``;
* distributed semaphores for concurrent page downloads and per-host concurrency,
  implemented as leases that expire, so a crashed worker cannot leak a slot;
* a global cooldown triggered by HTTP 429 responses, honouring ``Retry-After``
  and growing with repeated strikes;
* a hard "access blocked" stop for HTTP 401/403 from the API that is never
  lifted automatically.

Time is taken from the Redis server clock so that containers with skewed clocks
still agree.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import redis

from app.logging_setup import get_logger

log = get_logger(__name__)

PREFIX = "mdm:"
COOLDOWN_KEY = PREFIX + "cooldown"  # hash: until_ms, reason, strikes
STRIKES_KEY = PREFIX + "ratelimit:strikes"
BLOCK_KEY = PREFIX + "access_block"

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local rate = tonumber(ARGV[1])         -- tokens per millisecond
local burst = tonumber(ARGV[2])
local min_interval = tonumber(ARGV[3]) -- milliseconds
local data = redis.call('HMGET', key, 'tokens', 'ts', 'last')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
local last = tonumber(data[3])
if tokens == nil then tokens = burst; ts = now; last = 0 end
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens < 1 then wait = math.ceil((1 - tokens) / rate) end
local wait_interval = last + min_interval - now
if wait_interval > wait then wait = wait_interval end
if wait <= 0 then
  tokens = tokens - 1
  last = now
  redis.call('HSET', key, 'tokens', tokens, 'ts', now, 'last', last)
  redis.call('PEXPIRE', key, 3600000)
  return 0
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now, 'last', last)
redis.call('PEXPIRE', key, 3600000)
return wait
"""

_SEMAPHORE_ACQUIRE_LUA = """
local key = KEYS[1]
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local ttl = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local token = ARGV[3]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now + ttl, token)
  redis.call('PEXPIRE', key, ttl * 2)
  return 1
end
return 0
"""

_NOW_LUA = """
local t = redis.call('TIME')
return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
"""


class CooldownActive(Exception):
    """Raised instead of sleeping when a long global cooldown is active."""

    def __init__(self, until_ms: int, reason: str, kind: str = "rate_limit"):
        if kind == "maintenance":
            super().__init__("MangaDex is down for maintenance")
        else:
            super().__init__(f"Rate-limit cooldown active ({reason})")
        self.until_ms = until_ms
        self.reason = reason
        self.kind = kind

    @property
    def is_maintenance(self) -> bool:
        return self.kind == "maintenance"

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.until_ms / 1000 - time.time())


class AccessBlocked(Exception):
    """Raised while a MangaDex access restriction (403) is unacknowledged."""

    def __init__(self, info: dict):
        super().__init__(info.get("message") or "MangaDex access restricted")
        self.info = info


class SlotTimeout(Exception):
    pass


@dataclass
class BucketConfig:
    rate_per_second: float
    min_interval_ms: int
    burst: int | None = None

    @property
    def effective_burst(self) -> int:
        return self.burst or max(1, math.floor(self.rate_per_second))


class RateLimiter:
    def __init__(self, client: redis.Redis, sleep: Callable[[float], None] = time.sleep):
        self.r = client
        self._sleep = sleep
        self._bucket = client.register_script(_TOKEN_BUCKET_LUA)
        self._sem_acquire = client.register_script(_SEMAPHORE_ACQUIRE_LUA)
        self._now = client.register_script(_NOW_LUA)

    # ------------------------------------------------------------ time / state
    def now_ms(self) -> int:
        return int(self._now())

    def cooldown_state(self) -> dict | None:
        data = self.r.hgetall(COOLDOWN_KEY)
        if not data:
            return None
        until = int(data.get(b"until_ms", b"0"))
        if until <= self.now_ms():
            return None
        return {
            "until_ms": until,
            "reason": data.get(b"reason", b"").decode(),
            "kind": data.get(b"kind", b"rate_limit").decode() or "rate_limit",
            "strikes": int(self.r.get(STRIKES_KEY) or 0),
        }

    def block_state(self) -> dict | None:
        raw = self.r.get(BLOCK_KEY)
        return json.loads(raw) if raw else None

    def check_gates(self, max_inline_wait_s: float = 5.0) -> None:
        """Raise if MangaDex traffic must not happen right now."""
        block = self.block_state()
        if block:
            raise AccessBlocked(block)
        cd = self.cooldown_state()
        if cd:
            remaining = (cd["until_ms"] - self.now_ms()) / 1000
            if remaining > max_inline_wait_s:
                raise CooldownActive(cd["until_ms"], cd["reason"], cd.get("kind", "rate_limit"))
            self._sleep(max(0.0, remaining))

    # ------------------------------------------------------------ token buckets
    def acquire(self, bucket: str, cfg: BucketConfig, max_wait_s: float = 300.0) -> float:
        """Block until a request token is available. Returns seconds waited."""
        waited = 0.0
        rate_per_ms = cfg.rate_per_second / 1000.0
        while True:
            self.check_gates()
            wait_ms = int(self._bucket(keys=[f"{PREFIX}bucket:{bucket}"], args=[rate_per_ms, cfg.effective_burst, cfg.min_interval_ms]))
            if wait_ms <= 0:
                return waited
            delay = min(wait_ms / 1000.0, 5.0)
            if waited + delay > max_wait_s:
                raise SlotTimeout(f"Timed out waiting for rate limit bucket {bucket}")
            self._sleep(delay)
            waited += delay

    # ------------------------------------------------------------ semaphores
    @contextmanager
    def slot(self, name: str, limit: int, ttl_s: float = 180.0, max_wait_s: float = 600.0) -> Iterator[None]:
        token = uuid.uuid4().hex
        key = f"{PREFIX}sem:{name}"
        waited = 0.0
        while not int(self._sem_acquire(keys=[key], args=[int(ttl_s * 1000), max(1, limit), token])):
            if waited >= max_wait_s:
                raise SlotTimeout(f"Timed out waiting for slot {name}")
            self._sleep(0.25)
            waited += 0.25
        try:
            yield
        finally:
            try:
                self.r.zrem(key, token)
            except redis.RedisError:  # lease expires on its own
                pass

    def slot_usage(self, name: str) -> int:
        key = f"{PREFIX}sem:{name}"
        self.r.zremrangebyscore(key, "-inf", self.now_ms())
        return int(self.r.zcard(key))

    # ------------------------------------------------------------ 429 handling
    def register_rate_limit(
        self,
        retry_after_s: float | None,
        base_cooldown_s: int,
        max_cooldown_s: int,
        reason: str = "HTTP 429 from MangaDex",
    ) -> float:
        """Start (or extend) a global cooldown. Returns cooldown seconds."""
        strikes = int(self.r.incr(STRIKES_KEY))
        self.r.expire(STRIKES_KEY, 3600)
        escalated = base_cooldown_s * (2 ** max(0, strikes - 1))
        cooldown = max(float(retry_after_s or 0), float(escalated))
        cooldown = min(cooldown, float(max_cooldown_s))
        if retry_after_s and retry_after_s > cooldown:
            cooldown = float(retry_after_s)  # never undercut the server's instruction
        until = self.now_ms() + int(cooldown * 1000)
        existing = self.cooldown_state()
        if existing and existing["until_ms"] > until:
            until = existing["until_ms"]
        self.r.hset(COOLDOWN_KEY, mapping={"until_ms": until, "reason": reason, "kind": "rate_limit"})
        self.r.pexpire(COOLDOWN_KEY, max(1000, until - self.now_ms() + 60_000))
        log.warning("rate_limit_cooldown", cooldown_s=round(cooldown, 1), strikes=strikes, reason=reason)
        return cooldown

    def set_cooldown(self, seconds: float, reason: str, kind: str = "rate_limit") -> None:
        until = self.now_ms() + int(seconds * 1000)
        self.r.hset(COOLDOWN_KEY, mapping={"until_ms": until, "reason": reason, "kind": kind})
        self.r.pexpire(COOLDOWN_KEY, int(seconds * 1000) + 60_000)

    def set_maintenance(self, seconds: float, reason: str = "MangaDex is down for maintenance") -> int:
        """Pause all MangaDex traffic while MangaDex shows its maintenance page.

        Never shortens a longer cooldown that is already running. Returns the
        time (ms) when the next request may check whether MangaDex is back.
        """
        until = self.now_ms() + int(seconds * 1000)
        existing = self.cooldown_state()
        if existing and existing["until_ms"] >= until:
            return existing["until_ms"]
        self.set_cooldown(seconds, reason, kind="maintenance")
        log.warning("mangadex_maintenance", recheck_in_s=int(seconds))
        return until

    def clear_cooldown(self) -> None:
        self.r.delete(COOLDOWN_KEY, STRIKES_KEY)

    # ------------------------------------------------------------ access block
    def set_block(self, info: dict) -> None:
        self.r.set(BLOCK_KEY, json.dumps(info))

    def clear_block(self) -> None:
        self.r.delete(BLOCK_KEY)


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.redis_client import get_redis

        _limiter = RateLimiter(get_redis())
    return _limiter


def reset_limiter() -> None:
    global _limiter
    _limiter = None
