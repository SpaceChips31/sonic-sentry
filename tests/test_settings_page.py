from fastapi.testclient import TestClient
import pytest

from app.database import SessionLocal
from app.main import app
from app.models import ApplicationSetting
from app.services.runtime_settings import describe


@pytest.fixture(autouse=True)
def clean_application_settings():
    with SessionLocal() as session:
        session.query(ApplicationSetting).delete()
        session.commit()
    yield
    with SessionLocal() as session:
        session.query(ApplicationSetting).delete()
        session.commit()


def test_settings_page_has_single_save_and_navigation(monkeypatch):
    monkeypatch.delenv("SONIC_SENTRY_AUTH_ENABLED", raising=False)
    with TestClient(app) as client:
        response = client.get("/settings")
    assert response.status_code == 200
    assert 'id="settings-form"' in response.text
    assert 'id="advanced-toggle"' in response.text
    assert 'href="#workflow"' in response.text


def test_general_settings_save_updates_multiple_values(monkeypatch):
    for name in ("LOSSLESS_FILE_OPERATIONS", "SONIC_SENTRY_WORKER_CONCURRENCY"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/settings/save",
            data={
                "language": "it",
                "setting_file_operations": "true",
                "setting_worker_concurrency": "2",
            },
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"
    assert describe("file_operations")["value"] == "true"
    assert describe("worker_concurrency")["value"] == "2"
