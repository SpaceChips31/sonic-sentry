from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import config, main, worker, workflow, security, health, maintenance, operations
from app.database import Base
from app.services import file_workflow, importer, settings, sources, ingestion, queue, locks, retention, inventory, trash, file_tasks, reconciliation, history_reset


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    factory = sessionmaker(engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    for module in (main, worker, workflow, file_workflow, importer, settings, sources, ingestion, queue, security, retention, inventory, trash, operations, file_tasks, reconciliation, history_reset):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(locks, "DATA_DIR", tmp_path)
    monkeypatch.setattr(security, "DATA_DIR", tmp_path)
    monkeypatch.setattr(health, "DATA_DIR", tmp_path)
    monkeypatch.setattr(maintenance, "DATA_DIR", tmp_path)
    monkeypatch.setattr(maintenance, "engine", engine)
    monkeypatch.setattr(retention, "DATA_DIR", tmp_path)
    monkeypatch.setattr(retention, "REPORT_ROOT", tmp_path / "reports")
    monkeypatch.setattr(retention, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(main, "REPORT_ROOT", tmp_path / "reports")
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(worker, "REPORT_ROOT", tmp_path / "reports")
    monkeypatch.setattr(config, "FILE_OPERATIONS_LOCKED", False)
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "TRASH_ROOT", None)
    security.FAILED_LOGINS.clear()
    worker.STOP.clear()
    monkeypatch.setattr(sources, "SOURCE_ROOTS", ())
    monkeypatch.setattr(sources, "SOURCE_BASE", tmp_path)
    monkeypatch.setattr(config, "FILE_OPERATIONS_ENABLED", False)
    monkeypatch.setattr(config, "AUTO_ROUTE_RELEASES", False)
    monkeypatch.setattr(config, "MOVABLE_ROOTS", ())
    monkeypatch.setattr(config, "STAGING_ROOT", None)
    monkeypatch.setattr(config, "QUARANTINE_ROOT", None)
    monkeypatch.setattr(config, "REJECTED_ROOT", None)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    targets = {}
    for key in ("staging", "quarantine", "rejected"):
        path = tmp_path / key
        path.mkdir()
        targets[key] = str(path)
    yield SimpleNamespace(db=factory, root=tmp_path, inbox=inbox, targets=targets)
    engine.dispose()

