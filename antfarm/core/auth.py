"""Bearer token authentication for the Antfarm Colony API.

Provides HMAC-based token generation and FastAPI middleware for verifying
Authorization: Bearer <token> headers. When enabled, all endpoints except
GET /status require a valid token.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request
from fastapi.responses import JSONResponse


def generate_token(secret: str) -> str:
    """Generate a bearer token from a shared secret.

    Uses HMAC-SHA256 with the secret as both key and message,
    producing a deterministic hex token. This is intentionally simple —
    the token is a static credential derived from the secret, not a
    time-limited JWT.

    Args:
        secret: Shared secret string.

    Returns:
        Hex-encoded HMAC-SHA256 token.
    """
    return hmac.new(secret.encode(), secret.encode(), hashlib.sha256).hexdigest()


def verify_token(token: str, secret: str) -> bool:
    """Verify a bearer token against a shared secret.

    Args:
        token: The token from the Authorization header.
        secret: The shared secret used to generate valid tokens.

    Returns:
        True if the token is valid.
    """
    expected = generate_token(secret)
    return hmac.compare_digest(token, expected)


def create_auth_middleware(secret: str):
    """Create a FastAPI middleware that enforces bearer token auth.

    GET /status is exempt — it is the only unauthenticated endpoint.

    Args:
        secret: The shared secret for token verification.

    Returns:
        An async middleware function.
    """

    async def auth_middleware(request: Request, call_next):
        # GET /status is always public
        if request.method == "GET" and request.url.path == "/status":
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # strip "Bearer "
        if not verify_token(token, secret):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid bearer token"},
            )

        return await call_next(request)

    return auth_middleware
