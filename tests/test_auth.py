from app.auth import create_session, hash_password, session_user_id, verify_password
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import User
from fastapi.testclient import TestClient


def test_password_hash_round_trip():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)
    assert "correct horse" not in encoded


def test_signed_session_round_trip(monkeypatch):
    monkeypatch.setenv("SONIC_SENTRY_SECRET_KEY", "test-secret")
    value = create_session(42, now=1_000)
    assert session_user_id(value, now=1_001) == 42


def test_tampered_session_is_rejected(monkeypatch):
    monkeypatch.setenv("SONIC_SENTRY_SECRET_KEY", "test-secret")
    value = create_session(42, now=1_000)
    assert session_user_id(value.replace("42:", "7:", 1), now=1_001) is None


def test_expired_session_is_rejected(monkeypatch):
    monkeypatch.setenv("SONIC_SENTRY_SECRET_KEY", "test-secret")
    value = create_session(42, now=1_000)
    assert session_user_id(value, now=1_000 + 60 * 60 * 24 * 31) is None


def test_enabled_auth_guides_first_user_through_setup(monkeypatch):
    monkeypatch.setenv("SONIC_SENTRY_AUTH_ENABLED", "true")
    monkeypatch.setenv("SONIC_SENTRY_SECRET_KEY", "integration-secret")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        session.query(User).delete()
        session.commit()

    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/").headers["location"] == "/setup"
        response = client.post(
            "/setup",
            data={"username": "admin", "password": "a secure password"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert client.get("/").status_code == 200
        settings = client.get("/settings")
        assert settings.status_code == 200
        assert 'id="users"' in settings.text
        assert "admin" in settings.text
        assert client.post("/logout").headers["location"] == "/login"

    with SessionLocal() as session:
        session.query(User).delete()
        session.commit()
