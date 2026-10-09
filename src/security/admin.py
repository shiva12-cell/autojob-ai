"""
Administrative access control and API key validation guards.
Protects dangerous system endpoints (shutdown, mass data wipe, server-wide exports).
"""

import os
import secrets
from typing import Optional
from fastapi import Request, HTTPException, status, Header, Depends


def get_admin_key() -> str:
    """Retrieves the configured administrative API key from environment."""
    return os.getenv("ADMIN_API_KEY", "").strip()


def verify_admin_access(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")
) -> bool:
    """
    FastAPI dependency validating administrative credentials.
    Protects sensitive operations like remote shutdown, database wipe, and mass reset.
    """
    configured_key = get_admin_key()
    is_prod = os.getenv("ENV", "").lower() == "production"

    # Extract key string safely whether injected via Header dependency or called directly
    key_val = x_admin_key if isinstance(x_admin_key, str) else request.headers.get("x-admin-key")

    # If configured key is provided, validate via constant-time compare
    if configured_key:
        if key_val and secrets.compare_digest(key_val.strip(), configured_key):
            return True
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing administrative key (X-Admin-Key)."
        )

    # In production without an admin key configured, refuse all calls for safety
    if is_prod:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Administrative key (ADMIN_API_KEY) must be configured in production."
        )

    # In local development without key, only permit loopback requests
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )
    if client_ip in ["127.0.0.1", "localhost", "::1", "testclient"]:
        return True

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Forbidden: Admin operations require X-Admin-Key or loopback access."
    )
