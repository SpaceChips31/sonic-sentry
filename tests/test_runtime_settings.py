import os

import pytest

from app.database import Base, SessionLocal, engine
from app.models import ApplicationSetting
from app.services.runtime_settings import SettingError, describe, set_value


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    Base.metadata.create_all(bind=engine)
    for spec_env in (
        "LOSSLESS_FILE_OPERATIONS", "LOSSLESS_AUTO_ROUTE",
        "LOSSLESS_MOVABLE_ROOTS", "LOSSLESS_STAGING_ROOT",
        "LOSSLESS_QUARANTINE_ROOT", "LOSSLESS_REJECTED_ROOT",
        "SONIC_SENTRY_WORKER_CONCURRENCY",
    ):
        monkeypatch.delenv(spec_env, raising=False)
    with SessionLocal() as session:
        session.query(ApplicationSetting).delete()
        session.commit()


def test_unset_environment_setting_is_editable():
    set_value("staging_root", "/tmp/staging")
    field = describe("staging_root")
    assert field["locked"] is False
    assert field["source"] == "database"
    assert field["value"] == "/tmp/staging"


def test_environment_setting_is_locked(monkeypatch):
    monkeypatch.setenv("LOSSLESS_STAGING_ROOT", "/data/fixed")
    field = describe("staging_root")
    assert field["locked"] is True
    assert field["source"] == "environment"
    assert field["value"] == "/data/fixed"
    with pytest.raises(SettingError):
        set_value("staging_root", "/tmp/other")


def test_worker_concurrency_is_bounded():
    set_value("worker_concurrency", "4")
    assert describe("worker_concurrency")["value"] == "4"
    with pytest.raises(SettingError):
        set_value("worker_concurrency", "9")
