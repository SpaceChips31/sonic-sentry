from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import DATA_DIR, SessionLocal
from app.models import User
from app.presentation import configure_templates
from app.services.runtime_settings import bool_value


COOKIE_NAME = "sonic_sentry_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
PUBLIC_PATHS = {"/health", "/login", "/setup"}
templates = configure_templates(Jinja2Templates(directory="app/templates"))
router = APIRouter()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_secret() -> bytes:
    configured = os.getenv("SONIC_SENTRY_SECRET_KEY", "").strip()
    if configured:
        return configured.encode()
    path = Path(DATA_DIR) / ".session-secret"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip().encode()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_session(user_id: int, *, now: int | None = None) -> str:
    issued = now or int(time.time())
    payload = f"{user_id}:{issued}"
    signature = hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def session_user_id(value: str, *, now: int | None = None) -> int | None:
    try:
        user_id, issued, signature = value.split(":", 2)
        payload = f"{user_id}:{issued}"
        expected = hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()
        current = now or int(time.time())
        if not hmac.compare_digest(signature, expected):
            return None
        if current - int(issued) > SESSION_MAX_AGE or int(issued) > current + 60:
            return None
        return int(user_id)
    except (ValueError, TypeError):
        return None


def user_count() -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count(User.id))) or 0)


def authenticated_user(request: Request) -> User | None:
    value = request.cookies.get(COOKIE_NAME, "")
    user_id = session_user_id(value) if value else None
    if user_id is None:
        return None
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None or not user.active:
            return None
        session.expunge(user)
        return user


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        if not bool_value("auth_enabled"):
            return await call_next(request)

        path = request.url.path
        if path.startswith("/static/") or path == "/health":
            return await call_next(request)
        if user_count() == 0:
            if path != "/setup":
                return RedirectResponse("/setup", status_code=303)
            return await call_next(request)
        if path == "/setup":
            return RedirectResponse("/login", status_code=303)
        if path == "/login":
            return await call_next(request)

        user = authenticated_user(request)
        if user is None:
            target = request.url.path
            return RedirectResponse(f"/login?next={target}", status_code=303)
        request.state.user = user

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return HTMLResponse("Invalid request origin", status_code=403)
        return await call_next(request)


def require_admin(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if not bool_value("auth_enabled") or user_count() > 0:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="setup.html", context={})


@router.post("/setup")
def setup_admin(request: Request, username: str = Form(...), password: str = Form(...)):
    if not bool_value("auth_enabled") or user_count() > 0:
        raise HTTPException(status_code=409, detail="Initial setup is already complete")
    clean = username.strip()
    if len(clean) < 3:
        raise HTTPException(status_code=400, detail="Username is too short")
    try:
        encoded = hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with SessionLocal() as session:
        user = User(username=clean, password_hash=encoded, is_admin=True, active=True, created_at=utc_now())
        session.add(user)
        session.commit()
        session.refresh(user)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, create_session(user.id), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict")
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request=request, name="login.html", context={"next": next if next.startswith("/") else "/"})


@router.post("/login")
def login(username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == username.strip()))
        if user is None or not user.active or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        user_id = user.id
    destination = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(COOKIE_NAME, create_session(user_id), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.post("/settings/users")
def add_user(request: Request, username: str = Form(...), password: str = Form(...), admin: bool = Form(False)):
    require_admin(request)
    clean = username.strip()
    if len(clean) < 3:
        raise HTTPException(status_code=400, detail="Username is too short")
    try:
        encoded = hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with SessionLocal() as session:
        if session.scalar(select(User).where(User.username == clean)):
            raise HTTPException(status_code=409, detail="Username already exists")
        session.add(User(username=clean, password_hash=encoded, is_admin=admin, active=True, created_at=utc_now()))
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int):
    administrator = require_admin(request)
    if administrator.id == user_id:
        raise HTTPException(status_code=409, detail="You cannot disable your own account")
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        user.active = not user.active
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    current = getattr(request.state, "user", None)
    if current is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    with SessionLocal() as session:
        user = session.get(User, current.id)
        if user is None or not verify_password(current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        try:
            user.password_hash = hash_password(new_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.commit()
    return RedirectResponse("/settings", status_code=303)
