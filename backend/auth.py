"""
auth.py — JWT validation for Supabase Auth tokens.

Validates the access_token from Supabase Auth (Google OAuth),
extracts user_id and email, and determines role.
"""
import logging
import os
from typing import Optional

import jwt

logger = logging.getLogger(__name__)

ADMIN_EMAILS = set(
    e.strip()
    for e in os.environ.get("ADMIN_EMAILS", "jay98shinde@gmail.com,kadirlofca@outlook.com").split(",")
    if e.strip()
)


def verify_token(token: str) -> Optional[dict]:
    """
    Verify a Supabase JWT access token.

    Returns dict with: user_id, email, role
    Returns None if token is invalid.
    """
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET")
    if not jwt_secret:
        logger.error("SUPABASE_JWT_SECRET not configured")
        return None

    try:
        payload = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
        email = payload.get("email", "")
        user_id = payload.get("sub")
        if not user_id:
            return None

        return {
            "user_id": user_id,
            "email": email,
            "role": "admin" if email in ADMIN_EMAILS else "user",
        }
    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug("Invalid token: %s", e)
        return None
