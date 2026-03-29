"""
auth.py — Token verification for Supabase Auth.

Verifies the access_token from Supabase Auth (Google OAuth),
extracts user_id and email, and determines role.

Primary method: Supabase API verification (reliable, algorithm-agnostic).
Fallback: local PyJWT decode (fast, no network, requires matching algorithm).
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


def verify_token(token: str, supabase_client=None) -> Optional[dict]:
    """
    Verify a Supabase Auth access token.

    Returns dict with: user_id, email, role
    Returns None if token is invalid.
    """
    # ── Primary: verify via Supabase API ─────────────────────────────────
    # This works regardless of JWT algorithm (HS256, RS256, EdDSA, etc.)
    # and also catches revoked tokens.
    if supabase_client:
        try:
            response = supabase_client.auth.get_user(token)
            if response and response.user:
                user = response.user
                email = user.email or ""
                logger.info("Auth OK via Supabase API: user=%s", user.id[:8])
                return {
                    "user_id": user.id,
                    "email": email,
                    "role": "admin" if email in ADMIN_EMAILS else "user",
                }
        except Exception as e:
            logger.warning("Supabase API verification failed: %s", e)

    # ── Fallback: local JWT decode ───────────────────────────────────────
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET")
    if not jwt_secret:
        logger.error("SUPABASE_JWT_SECRET not configured and Supabase API unavailable")
        return None

    try:
        # Log the token's algorithm for debugging
        try:
            header = jwt.get_unverified_header(token)
            logger.info("JWT header: alg=%s typ=%s", header.get("alg"), header.get("typ"))
        except Exception:
            pass

        payload = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256", "HS384", "HS512"],
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
        logger.warning("Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("Local JWT verification failed: %s", e)
        return None
