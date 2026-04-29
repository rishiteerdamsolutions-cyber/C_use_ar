"""
Rate Limiter — Redis-based per-key request throttling.
cusear™ Platform · API Layer

Limits:
  - 60 requests / minute  per key  (burst protection)
  - 1000 requests / day   per key  (fair use)
Falls back to in-memory dict if Redis is unavailable.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))
RATE_LIMIT_PER_DAY    = int(os.environ.get("RATE_LIMIT_PER_DAY", "1000"))

# In-memory fallback (used when Redis unavailable)
_memory_store: dict[str, list[float]] = defaultdict(list)


# ─── Redis connection ─────────────────────────────────────────────────────────
_redis_client = None

def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis  # type: ignore
        _redis_client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=0,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        _redis_client.ping()
        logger.info("Redis connected for rate limiting")
        return _redis_client
    except Exception as exc:
        logger.warning("Redis unavailable — using in-memory rate limiter: %s", exc)
        _redis_client = None
        return None


# ─── Redis-backed limiter ─────────────────────────────────────────────────────
def _check_redis(key_id: str) -> tuple[bool, str]:
    """Use Redis sliding window. Returns (allowed, reason)."""
    r = _get_redis()
    if r is None:
        return None, ""   # signal to use fallback

    now     = time.time()
    min_key = f"rl:min:{key_id}"
    day_key = f"rl:day:{key_id}"

    pipe = r.pipeline()

    # Sliding window — minute
    pipe.zremrangebyscore(min_key, 0, now - 60)
    pipe.zcard(min_key)
    pipe.zadd(min_key, {str(now): now})
    pipe.expire(min_key, 120)

    # Sliding window — day
    pipe.zremrangebyscore(day_key, 0, now - 86400)
    pipe.zcard(day_key)
    pipe.zadd(day_key, {f"{now:.6f}": now})
    pipe.expire(day_key, 172800)

    results = pipe.execute()
    # results: [removed_min, count_min, added_min, expire_min,
    #           removed_day, count_day, added_day, expire_day]
    count_min = results[1]
    count_day = results[5]

    if count_min >= RATE_LIMIT_PER_MINUTE:
        return False, f"Rate limit: {RATE_LIMIT_PER_MINUTE} requests/minute exceeded"
    if count_day >= RATE_LIMIT_PER_DAY:
        return False, f"Rate limit: {RATE_LIMIT_PER_DAY} requests/day exceeded"

    return True, ""


# ─── In-memory fallback limiter ───────────────────────────────────────────────
def _check_memory(key_id: str) -> tuple[bool, str]:
    """Simple in-memory sliding window (single-process only)."""
    now = time.time()
    timestamps = _memory_store[key_id]

    # Prune old entries
    _memory_store[key_id] = [t for t in timestamps if now - t < 86400]
    timestamps = _memory_store[key_id]

    last_minute = [t for t in timestamps if now - t < 60]
    if len(last_minute) >= RATE_LIMIT_PER_MINUTE:
        return False, f"Rate limit: {RATE_LIMIT_PER_MINUTE} requests/minute exceeded"
    if len(timestamps) >= RATE_LIMIT_PER_DAY:
        return False, f"Rate limit: {RATE_LIMIT_PER_DAY} requests/day exceeded"

    _memory_store[key_id].append(now)
    return True, ""


# ─── Public API ───────────────────────────────────────────────────────────────
def check_rate_limit(key_id: str) -> tuple[bool, str]:
    """
    Check if a request from key_id is within rate limits.

    Args:
        key_id: The API key MongoDB _id string.

    Returns:
        (allowed, reason)
        allowed=True  → request can proceed
        allowed=False → HTTP 429 should be returned with `reason`
    """
    result, reason = _check_redis(key_id)
    if result is None:
        return _check_memory(key_id)
    return result, reason


def get_rate_limit_headers(key_id: str) -> dict[str, str]:
    """
    Return X-RateLimit headers to include in every response.
    """
    return {
        "X-RateLimit-Limit-Minute": str(RATE_LIMIT_PER_MINUTE),
        "X-RateLimit-Limit-Day":    str(RATE_LIMIT_PER_DAY),
    }
