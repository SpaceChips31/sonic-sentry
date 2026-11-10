"""Optional local accounts and signed sessions; CSRF protection also applies on private LANs."""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
import threading
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.database import DATA_DIR, SessionLocal, engine
from app.models import AuthUser


ROLES = {"viewer", "operator", "admin"}
router = APIRouter()
FAILED_LOGINS = {}
LOGIN_LOCK = threading.Lock()


def secret() -> bytes:
    path = DATA_DIR / "session.key"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(20):
            value = path.read_bytes()
            if len(value) >= 32:
                return value
            time.sleep(0.01)
        raise RuntimeError("The session key is empty or invalid")
    with os.fdopen(descriptor, "wb") as output:
        value = secrets.token_bytes(32)
        output.write(value)
        return value


def sign(value: str) -> str:
    return hmac.new(secret(), value.encode(), hashlib.sha256).hexdigest()


def csrf_valid(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    nonce, signature = value.rsplit(".", 1)
    return len(nonce) == 64 and hmac.compare_digest(signature, sign("csrf:" + nonce))


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2-sha256$600000${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2-sha256" or not 100000 <= int(iterations) <= 1000000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def session_token(user: AuthUser) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"user": user.username, "expires": time.time() + 43200,
        "stamp": hashlib.sha256((user.password_hash + user.role).encode()).hexdigest()}).encode()).decode()
    return payload + "." + sign("session:" + payload)


def session_user(token: str | None):
    try:
        payload, signature = (token or "").rsplit(".", 1)
        if not hmac.compare_digest(signature, sign("session:" + payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data["expires"] < time.time():
            return None
        with SessionLocal() as session:
            user = session.get(AuthUser, data["user"])
            if user and user.role in ROLES and data["stamp"] == hashlib.sha256((user.password_hash + user.role).encode()).hexdigest():
                return user
    except (ValueError, TypeError, KeyError, UnicodeError):
        pass
    return None


def admin_path(path: str) -> bool:
    return path.startswith(("/settings", "/maintenance")) and path != "/settings/language" or path.startswith("/trash/") and path.endswith("/purge")


async def security_middleware(request: Request, call_next):
    token = request.cookies.get("sonic_csrf")
    if not csrf_valid(token):
        nonce = secrets.token_hex(32)
        token = nonce + "." + sign("csrf:" + nonce)
    request.state.csrf_token = token
    request.state.actor, request.state.role = "local", "admin"
    public = request.url.path in {"/login", "/health"} or request.url.path.startswith("/static/")
    if config.AUTH_ENABLED:
        user = session_user(request.cookies.get("sonic_session"))
        request.state.actor = user.username if user else None
        request.state.role = user.role if user else None
        if not public and user is None:
            response = RedirectResponse("/login?" + urlencode({"next": request.url.path}), status_code=303) if request.method == "GET" else JSONResponse({"detail": "Login required"}, status_code=401)
        elif not public and (admin_path(request.url.path) and user.role != "admin" or request.method != "GET" and request.url.path not in {"/logout", "/settings/language"} and user.role == "viewer"):
            response = JSONResponse({"detail": "This account does not have permission for this action"}, status_code=403)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    if request.cookies.get("sonic_csrf") != token:
        response.set_cookie("sonic_csrf", token, httponly=True, samesite="lax",
                            secure=config.COOKIE_SECURE or request.url.scheme == "https")
    return response


async def check_csrf(request: Request):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    cookie = request.cookies.get("sonic_csrf")
    supplied = request.headers.get("x-csrf-token")
    if supplied is None:
        supplied = (await request.form()).get("csrf_token")
    if not isinstance(supplied, str) or not csrf_valid(cookie) or not hmac.compare_digest(supplied, cookie):
        raise HTTPException(status_code=403, detail="The form expired or failed CSRF validation. Reload the page and try again.")


def require_admin_configured():
    secret()
    if config.AUTH_ENABLED:
        from sqlalchemy import select
        with SessionLocal() as session:
            if session.scalar(select(AuthUser.username).where(AuthUser.role == "admin")) is None:
                raise RuntimeError("Create an admin account with python -m app.security add-user before enabling SONIC_AUTH_ENABLED")


def login_page(request, error=None):
    from app.presentation import configure_templates
    templates = configure_templates(Jinja2Templates(directory="app/templates"))
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error})


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return login_page(request) if config.AUTH_ENABLED else RedirectResponse("/", status_code=303)


@router.post("/login")
def authenticate(request: Request, username: str = Form(..., max_length=100), password: str = Form(..., max_length=1024), next: str = Form("/")):
    if not config.AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    address = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with LOGIN_LOCK:
        failures = [stamp for stamp in FAILED_LOGINS.get(address, []) if now - stamp < 300]
        if len(failures) >= 10:
            raise HTTPException(status_code=429, detail="Too many failed logins; try again in five minutes")
        if len(FAILED_LOGINS) > 10000:
            FAILED_LOGINS.clear()
        FAILED_LOGINS[address] = failures + [now]
    with SessionLocal() as session:
        user = session.get(AuthUser, username)
    encoded = user.password_hash if user else "pbkdf2-sha256$600000$00000000000000000000000000000000$" + "0" * 64
    valid = verify_password(password, encoded)
    if user is None or not valid:
        response = login_page(request, "Invalid username or password")
        response.status_code = 401
        return response
    with LOGIN_LOCK:
        FAILED_LOGINS.pop(address, None)
    destination = next if next.startswith("/") and not next.startswith("//") and "\\" not in next and not any(ord(c) < 32 for c in next) else "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie("sonic_session", session_token(user), max_age=43200, httponly=True, samesite="lax",
                        secure=config.COOKIE_SECURE or request.url.scheme == "https")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("sonic_session")
    return response


def main():
    from app.migrations import initialize_database
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["add-user"])
    parser.add_argument("username")
    parser.add_argument("--role", choices=sorted(ROLES), default="admin")
    args = parser.parse_args()
    if not args.username.strip() or len(args.username) > 100:
        parser.error("Use a nonempty username of at most 100 characters")
    password = getpass.getpass("Password (at least 12 characters): ")
    if len(password) < 12 or password != getpass.getpass("Repeat password: "):
        parser.error("Passwords must match and contain at least 12 characters")
    initialize_database(engine)
    with SessionLocal() as session:
        user = session.get(AuthUser, args.username)
        if user is None:
            user = AuthUser(username=args.username, password_hash="", role=args.role)
            session.add(user)
        user.password_hash, user.role = password_hash(password), args.role
        session.commit()
    print("Account saved. Existing sessions for this account are invalidated.")


if __name__ == "__main__":
    main()
