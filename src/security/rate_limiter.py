"""
In-memory sliding-window rate limiter for FastAPI.
Provides brute-force and Denial-of-Service mitigation without external dependencies.
"""

import time
import threading
from typing import Dict, List, Tuple
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window rate limiter."""

    def __init__(self):
        self._lock = threading.Lock()
        # Storage format: { (ip, bucket_name): [timestamp_1, timestamp_2, ...] }
        self._requests: Dict[Tuple[str, str], List[float]] = {}
        self._last_cleanup = time.time()

    def is_allowed(self, client_ip: str, bucket: str, limit: int, window_seconds: int = 60) -> Tuple[bool, int]:
        """
        Checks if the request is permitted within the sliding window.
        Returns (is_allowed, retry_after_seconds).
        """
        now = time.time()
        key = (client_ip, bucket)

        with self._lock:
            # Periodic cleanup every 120 seconds
            if now - self._last_cleanup > 120:
                self._cleanup_expired(now)

            timestamps = self._requests.setdefault(key, [])
            cutoff = now - window_seconds
            # Remove timestamps outside current window
            while timestamps and timestamps[0] < cutoff:
                timestamps.pop(0)

            if len(timestamps) >= limit:
                oldest = timestamps[0]
                retry_after = max(1, int(oldest + window_seconds - now))
                return False, retry_after

            timestamps.append(now)
            return True, 0

    def _cleanup_expired(self, now: float):
        """Prunes stale keys whose timestamps are older than 120 seconds."""
        stale_keys = []
        for key, timestamps in self._requests.items():
            while timestamps and timestamps[0] < now - 120:
                timestamps.pop(0)
            if not timestamps:
                stale_keys.append(key)
        for k in stale_keys:
            self._requests.pop(k, None)
        self._last_cleanup = now

    def reset(self):
        """Wipes all tracked timestamps (useful for unit tests)."""
        with self._lock:
            self._requests.clear()
            self._last_cleanup = time.time()


rate_limiter = SlidingWindowRateLimiter()


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """Starlette middleware intercepting requests to enforce rate limits."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)

        # Allow static files and root without heavy limiting
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            return await call_next(request)

        # Determine client IP (handles standard proxy headers)
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or request.headers.get("x-real-ip", "").strip()
            or (request.client.host if request.client else "127.0.0.1")
        )

        # Map path to bucket and limit
        bucket = "general"
        limit = 120
        window = 60

        if path == "/api/auth/login":
            bucket = "auth_login"
            limit = 5
        elif path == "/api/auth/register":
            bucket = "auth_register"
            limit = 3
        elif path in ["/api/user/trigger", "/api/trigger"]:
            bucket = "trigger_action"
            limit = 5
        elif path in ["/api/profile/upload-resume", "/api/user/upload-resume"]:
            bucket = "resume_upload"
            limit = 10

        allowed, retry_after = rate_limiter.is_allowed(client_ip, bucket, limit, window)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "status": "error",
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": f"Too many requests to '{bucket}'. Please wait {retry_after} seconds before trying again.",
                    "retry_after": retry_after
                },
                headers={"Retry-After": str(retry_after)}
            )

        return await call_next(request)
