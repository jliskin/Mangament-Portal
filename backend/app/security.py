"""Authentication (argon2id + signed session cookie), role checks and CSRF."""
from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

_ph = PasswordHasher()
ROLE_RANK = {"viewer": 0, "manager": 1, "admin": 2}


def hash_password(pw: str) -> str:
    if len(pw) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres")
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.query(User).filter(User.email == email.strip().lower(), User.is_active.is_(True)).first()
    if not user or not verify_password(user.password_hash, password):
        # equalize timing for unknown users
        if not user:
            _ph.hash("timing-equalizer")
        return None
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return user


class NotAuthenticated(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        raise NotAuthenticated()
    request.state.user = user
    return user


def require_role(min_role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise HTTPException(403, "Permisos insuficientes")
        return user
    return dep


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


async def verify_csrf(request: Request) -> None:
    """Every state-changing request must echo the session token, either in a
    form field (`csrf`) or in the `X-CSRF-Token` header (set globally for HTMX)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        sent = (await request.form()).get("csrf")
    if not expected or not sent or not hmac.compare_digest(str(expected), str(sent)):
        raise HTTPException(403, "CSRF token inválido. Recargue la página.")
