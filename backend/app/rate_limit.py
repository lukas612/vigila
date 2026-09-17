"""Simple in-memory rate limiting for the anonymous free-check endpoint.

Good enough for a single-process MVP. If the backend ever runs with more
than one worker/instance, swap this for a shared store (Redis) — the
interface (`is_allowed`, `hit`) stays the same.
"""
from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock

DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_MAX_REQUESTS = 5


@dataclass
class _Bucket:
    timestamps: list[float] = field(default_factory=list)


class RateLimiter:
    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS, window_seconds: int = DEFAULT_WINDOW_SECONDS):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket)
        self._lock = Lock()

    def _prune(self, bucket: _Bucket, now: float) -> None:
        cutoff = now - self.window_seconds
        bucket.timestamps = [t for t in bucket.timestamps if t > cutoff]

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._buckets[key]
            self._prune(bucket, now)
            return len(bucket.timestamps) < self.max_requests

    def hit(self, key: str) -> None:
        now = time.time()
        with self._lock:
            bucket = self._buckets[key]
            self._prune(bucket, now)
            bucket.timestamps.append(now)

    def remaining(self, key: str) -> int:
        now = time.time()
        with self._lock:
            bucket = self._buckets[key]
            self._prune(bucket, now)
            return max(0, self.max_requests - len(bucket.timestamps))


def hash_identifier(value: str) -> str:
    """One-way hash for storing IP/fingerprint/DNI without keeping the raw value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
