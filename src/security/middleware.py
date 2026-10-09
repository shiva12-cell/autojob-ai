"""
Security Headers middleware for FastAPI.
Injects defensive HTTP headers according to OWASP guidelines to prevent clickjacking,
MIME sniffing, XSS, and unauthorized framing.
"""

import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds hardened security headers to all HTTP responses."""

    def __init__(self, app):
        super().__init__(app)
        self.is_prod = os.getenv("ENV", "").lower() == "production"

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # 1. Prevent MIME-sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # 2. Prevent clickjacking / frame embedding
        response.headers["X-Frame-Options"] = "DENY"

        # 3. Enable legacy browser XSS filters
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # 4. Strict referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # 5. Disable unused browser device features
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"

        # 6. Content Security Policy (allows Tailwind CDN & FontAwesome)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "font-src 'self' https://cdnjs.cloudflare.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )

        # 7. Strict-Transport-Security if HTTPS or production
        if self.is_prod or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response
