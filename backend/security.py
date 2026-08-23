# backend/security.py
"""Password hashing and JWT issuing.

Uses only the standard library so there is nothing extra to install and nothing
that can break on a new Python release the week before a review.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

SECRET_KEY = os.getenv("SECRET_KEY", "mediconnect-dev-secret-change-me")
TOKEN_TTL_SECONDS = 60 * 60 * 12  # 12 hours

_PBKDF2_ROUNDS = 120_000


# ------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    """Return 'salt$hash', both hex encoded."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return hmac.compare_digest(digest.hex(), expected)


# ------------------------------------------------------------------------ jwt


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def create_token(user_id: int, role: str, name: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "role": role,
        "name": name,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = hmac.new(
        SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64(signature)}"


def decode_token(token: str) -> dict | None:
    """Return the payload, or None if the token is malformed/forged/expired."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return None

    signing_input = f"{header_b64}.{payload_b64}"
    expected = hmac.new(
        SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_unb64(signature_b64), expected):
        return None

    try:
        payload = json.loads(_unb64(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None

    if payload.get("exp", 0) < time.time():
        return None
    return payload
