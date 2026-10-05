"""Password hashing and signed-cookie sessions. Stdlib scrypt + itsdangerous."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, db

COOKIE = "dp_session"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$%s$%s" % (salt.hex(), dk.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def _secret() -> str:
    """Signing key, generated once and kept on disk, so it is never in the repo."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not config.SECRET_FILE.exists():
        config.SECRET_FILE.write_text(secrets.token_hex(32))
    return config.SECRET_FILE.read_text().strip()


_serializer: Optional[URLSafeTimedSerializer] = None


def _ser() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_secret(), salt="dp-session")
    return _serializer


def make_token(user_id: int) -> str:
    return _ser().dumps({"uid": user_id})


def seed_demo_user() -> None:
    if db.get_user_by_name(config.DEMO_USER) is None:
        db.create_user(config.DEMO_USER, hash_password(config.DEMO_PASSWORD))


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    user = db.get_user_by_name(username)
    if user and verify_password(password, user["password_hash"]):
        return user
    # Burn comparable time for an unknown user so timing does not reveal which exist.
    if user is None:
        verify_password(password, "scrypt$00$00")
    return None


def require_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: the logged-in user, or 401."""
    token = request.cookies.get(COOKIE)
    if token:
        try:
            data = _ser().loads(token, max_age=config.SESSION_HOURS * 3600)
            user = db.get_user(int(data["uid"]))
            if user:
                return user
        except (BadSignature, KeyError, ValueError):
            pass
    raise HTTPException(status_code=401, detail="Not signed in")
