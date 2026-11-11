import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from fastapi.testclient import TestClient as RawClient

from client import TestClient
from app import config, health, main, security, worker, maintenance
from app.models import AnalysisJob, AuthUser, FileTransfer, Release, ReviewEvent, TrashEntry, Track, AnalysisSource
from app.services import file_workflow, importer, inventory, queue, retention, settings, trash
from app.services.files import snapshot
from test_workflow import configure, report


def trashed(env):
    configure(env)
    release = importer.import_report(report(env))
    destination = file_workflow.delete_release_files(release.id)
    with env.db() as session:
        entry = session.scalar(select(TrashEntry))
    return release, destination, entry.id


def old(path):
    os.utime(path, (time.time() - 90 * 86400, time.time() - 90 * 86400))


def test_trash_and_restore_preserve_bytes_and_quality(setup):
    release, destination, entry_id = trashed(setup)
    assert (destination / "track.flac").read_bytes() == b"test audio"
    with setup.db() as session:
        assert session.get(Release, release.id).status == "PASS"
        assert session.get(TrashEntry, entry_id).state == "TRASHED"
    restored = trash.restore_trash(entry_id)
    assert restored == setup.inbox / "Album"
    assert (restored / "track.flac").read_bytes() == b"test audio"
    assert not destination.exists()
    with setup.db() as session:
        assert session.get(TrashEntry, entry_id).state == "RESTORED"
        assert session.get(Release, release.id).source_path == str(restored)


def test_restore_never_overwrites_original_location(setup):
    release, destination, entry_id = trashed(setup)
    original = setup.inbox / "Album"
    original.mkdir()
    (original / "new.flac").write_bytes(b"new content")
    with pytest.raises(file_workflow.FileOperationError, match="occupied"):
        trash.restore_trash(entry_id)
    assert (original / "new.flac").read_bytes() == b"new content"
    assert (destination / "track.flac").exists()


def test_permanent_deletion_requires_typed_confirmation(setup):
    release, destination, entry_id = trashed(setup)
    with TestClient(main.app) as client:
        assert client.post(f"/trash/{entry_id}/purge", data={"confirmation": "wrong"}).status_code == 400
        assert destination.exists()
        assert client.post(f"/trash/{entry_id}/purge", data={"confirmation": "ELIMINA"}, follow_redirects=False).status_code == 303
    assert not destination.exists()
    with setup.db() as session:
        assert session.get(TrashEntry, entry_id).state == "PURGED"
        assert session.get(Release, release.id) is not None


def test_modified_trash_is_preserved_on_purge(setup):
    _, destination, entry_id = trashed(setup)
    (destination / "new.flac").write_bytes(b"keep me")
    with pytest.raises(file_workflow.FileOperationError, match="changed or added"):
        trash.purge_trash(entry_id)
    assert (destination / "new.flac").read_bytes() == b"keep me"
    assert (destination / "track.flac").exists()


def test_interrupted_purge_resumes_only_already_confirmed_work(setup, monkeypatch):
    _, destination, entry_id = trashed(setup)
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == destination / "track.flac":
            raise SystemExit("power loss")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(SystemExit):
        trash.purge_trash(entry_id)
    monkeypatch.setattr(Path, "unlink", original)
    assert trash.recover_purges() == 1
    assert not destination.exists()


def test_deployment_lock_overrides_saved_configuration(setup, monkeypatch):
    configure(setup)
    release = importer.import_report(report(setup))
    monkeypatch.setattr(config, "FILE_OPERATIONS_LOCKED", True)
    values = settings.workflow_settings()
    assert values["locked"] and not values["enabled"] and not values["auto_route"]
    with pytest.raises(settings.WorkflowConfigurationError, match="locked"):
        configure(setup)
    with pytest.raises(file_workflow.FileOperationError):
        file_workflow.delete_release_files(release.id)
    assert (setup.inbox / "Album" / "track.flac").exists()


def test_deployment_lock_stops_resuming_existing_transfers(setup, monkeypatch):
    from test_recovery_ingestion import interrupted_transfer
    _, transfer = interrupted_transfer(setup, monkeypatch, "before_publish")
    monkeypatch.setattr(config, "FILE_OPERATIONS_LOCKED", True)
    with pytest.raises(file_workflow.FileOperationError, match="locked"):
        file_workflow.resume_transfer(transfer.id)


def test_inventory_distinguishes_physical_location_from_quality(setup):
    configure(setup)
    release = importer.import_report(report(setup))
    file_workflow.move_release(release.id, "QUARANTINE")
    unknown = setup.root / "quarantine" / "Unregistered"
    unknown.mkdir()
    (unknown / "track.FLAC").write_bytes(b"audio")
    data = inventory.destination_inventory("quarantine")
    assert any(row["release"] and row["release"].status == "PASS" for row in data["rows"])
    assert any(row["name"] == "Unregistered" and not row["release"] for row in data["rows"])
    (setup.root / "quarantine" / "Album" / "track.flac").unlink()
    (setup.root / "quarantine" / "Album").rmdir()
    assert any(row["release"] and not row["exists"] for row in inventory.destination_inventory("quarantine", show_all=True)["rows"])
    with TestClient(main.app) as client:
        assert client.get("/destinations/quarantine").status_code == 200


def test_report_cleanup_protects_live_spectrograms_and_active_jobs(setup):
    root = setup.root / "reports"
    root.mkdir()
    graph_dir = root / "job-1-spectrograms"
    graph_dir.mkdir()
    graph = graph_dir / ("a" * 24 + ".png")
    graph.write_bytes(b"graph")
    old(graph)
    path = report(setup)
    payload = json.loads(path.read_text())
    payload["files"][0]["forensics"] = {"result": {"spectrogram_path": str(graph)}}
    path.write_text(json.dumps(payload))
    importer.import_report(path)
    for job_id, status in [(1, "COMPLETED"), (2, "ANALYZING")]:
        with setup.db() as session:
            session.add(AnalysisJob(id=job_id, source_type="TEST", source_path=str(setup.inbox / "Album"), display_name="Album", status=status, created_at="now"))
            session.commit()
        file = root / f"job-{job_id}.json"
        file.write_text("{}")
        old(file)
    candidates = retention.cleanup_preview()
    assert [Path(row["path"]).name for row in candidates] == ["job-1.json"]
    assert retention.execute_cleanup([candidates[0]["token"]]) == (1, 2)
    assert graph.exists() and (root / "job-2.json").exists()


def test_upload_cleanup_never_removes_referenced_audio(setup):
    root = setup.root / "uploads"
    root.mkdir()
    for name in ("a" * 32, "b" * 32):
        album = root / name
        album.mkdir()
        (album / "track.flac").write_bytes(b"audio")
        old(album / "track.flac")
        old(album)
    with setup.db() as session:
        session.add(AnalysisJob(source_type="UPLOAD", source_path=str(root / ("a"*32)), display_name="Album", status="FAILED", created_at="now"))
        session.commit()
    candidates = retention.cleanup_preview()
    assert len(candidates) == 1 and candidates[0]["path"] == str(root / ("b"*32))
    retention.execute_cleanup([candidates[0]["token"]])
    assert (root / ("a"*32) / "track.flac").exists()


def test_cleanup_rejects_stale_preview_before_any_deletion(setup):
    root = setup.root / "reports"
    root.mkdir()
    path = root / ("a"*32 + "-import.json")
    path.write_text("{}")
    old(path)
    token = retention.cleanup_preview()[0]["token"]
    path.write_text("new report")
    with pytest.raises(file_workflow.FileOperationError, match="changed"):
        retention.execute_cleanup([token])
    assert path.read_text() == "new report"


def test_csrf_is_required_even_without_login(setup):
    with RawClient(main.app) as client:
        assert client.post("/settings/language", data={"language": "it"}).status_code == 403
        response = client.get("/settings")
        assert 'name="csrf_token"' in response.text
        token = client.cookies.get("sonic_csrf")
        assert client.post("/settings/language", data={"language": "it", "csrf_token": token}, follow_redirects=False).status_code == 303
        assert client.post("/settings/language", data={"language": "it", "csrf_token": "forged"}).status_code == 403


def add_user(env, username, role):
    with env.db() as session:
        session.add(AuthUser(username=username, role=role, password_hash=security.password_hash("a-test-password-123")))
        session.commit()


@pytest.mark.parametrize("role,queue_status,settings_status", [("viewer",403,403), ("operator",404,403), ("admin",404,200)])
def test_optional_auth_roles_are_enforced_on_server(setup, monkeypatch, role, queue_status, settings_status):
    add_user(setup, "admin", "admin")
    if role != "admin":
        add_user(setup, role, role)
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    with TestClient(main.app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        user = "admin" if role == "admin" else role
        assert client.post("/login", data={"username": user, "password": "a-test-password-123"}, follow_redirects=False).status_code == 303
        assert client.get("/").status_code == 200
        assert client.get("/settings").status_code == settings_status
        assert client.post("/analyses/999/cancel").status_code == queue_status
        if role != "admin":
            assert client.post("/trash/unknown/purge", data={"confirmation":"ELIMINA"}).status_code == 403
        assert client.post("/destinations/quarantine/staging", data={"release_ids":[999],"mode":"approved"}).status_code == (403 if role == "viewer" else 200)
        assert client.post("/logout", follow_redirects=False).status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


def test_password_change_invalidates_existing_sessions(setup, monkeypatch):
    add_user(setup, "admin", "admin")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    with TestClient(main.app) as client:
        client.post("/login", data={"username":"admin", "password":"a-test-password-123"})
        assert client.get("/settings").status_code == 200
        with setup.db() as session:
            session.get(AuthUser, "admin").password_hash = security.password_hash("changed-password-123")
            session.commit()
        assert client.get("/", follow_redirects=False).status_code == 303


def test_review_notes_and_actor_are_audited(setup):
    release = importer.import_report(report(setup, "QUARANTINE"))
    with TestClient(main.app) as client:
        response = client.post("/releases/review", data={"release_ids":[release.id], "decision":"approve", "note":"Checked original release"}, follow_redirects=False)
        assert response.status_code == 303
    with setup.db() as session:
        event = session.scalar(select(ReviewEvent))
        assert event.previous == "NONE" and event.decision == "APPROVED"
        assert event.note == "Checked original release" and event.actor == "local"


def test_heartbeat_distinguishes_idle_stale_and_stalled(setup):
    path = setup.root / "worker-health.json"
    data = {"pid":os.getpid(), "heartbeat_at":time.time(), "phase":"IDLE"}
    path.write_text(json.dumps(data))
    assert health.worker_status()["status"] == "ok"
    data.update(phase="VALIDATING", last_progress=time.time()-100, stall_timeout=10, deadline=time.time()+100)
    path.write_text(json.dumps(data))
    assert health.worker_status()["status"] == "blocked"
    data["heartbeat_at"] = time.time()-100
    path.write_text(json.dumps(data))
    assert health.worker_status()["status"] == "unavailable"


def test_timeout_retries_are_delayed_and_bounded(setup, monkeypatch):
    report(setup)
    (setup.root / "reports").mkdir()
    job_id = queue.enqueue_paths([setup.inbox / "Album"], "TEST")[0]
    def timeout(*args):
        raise worker.AnalysisTimeout("test timeout")
    monkeypatch.setattr(worker, "run_validator", timeout)
    worker.process_job(worker.claim_job())
    with setup.db() as session:
        job = session.get(AnalysisJob, job_id)
        assert job.status == "QUEUED" and job.next_run_at > time.time()
        job.next_run_at = time.time()-1
        session.commit()
    worker.process_job(worker.claim_job())
    with setup.db() as session:
        assert session.get(AnalysisJob, job_id).status == "FAILED"
        assert session.get(AnalysisJob, job_id).attempts == 2


def test_backup_restore_preserves_metadata_but_pauses_work(setup):
    release = importer.import_report(report(setup))
    with setup.db() as session:
        session.add(AnalysisSource(name="Inbox",path=str(setup.inbox),kind="FILESYSTEM",enabled=True,watch_enabled=True))
        session.commit()
    queue.enqueue_paths([setup.inbox / "Album"], "TEST")
    archive = setup.root / "backup.zip"
    maintenance.export_backup(archive)
    with setup.db() as session:
        session.get(Release, release.id).title = "Changed title"
        session.commit()
    retained = maintenance.restore_backup(archive)
    assert retained.is_file()
    with setup.db() as session:
        assert session.get(Release, release.id).title == "Album"
        assert session.scalar(select(AnalysisJob)).status == "CANCELLED"
        assert not session.scalar(select(AnalysisSource)).watch_enabled
    assert (setup.inbox / "Album" / "track.flac").exists()


def test_restore_refuses_a_live_web_process(setup):
    report(setup)
    archive = setup.root / "backup.zip"
    maintenance.export_backup(archive)
    with TestClient(main.app):
        with pytest.raises(RuntimeError, match="already running"):
            maintenance.restore_backup(archive)


def test_maintenance_and_destination_pages_render(setup):
    with TestClient(main.app) as client:
        for url in ("/maintenance", "/destinations/quarantine", "/destinations/rejected", "/destinations/staging", "/destinations/trash"):
            assert client.get(url).status_code == 200
        response = client.get("/maintenance/backup")
        assert response.status_code == 200 and response.content.startswith(b"PK")


@pytest.mark.parametrize("timeout_key", ["analysis_timeout", "stall_timeout"])
def test_actual_stalled_validator_is_stopped(setup, monkeypatch, timeout_key):
    script = setup.root / "stalled.py"
    script.write_text("import time; time.sleep(60)")
    monkeypatch.setattr(worker, "VALIDATOR", script)
    monkeypatch.setattr(worker, "workflow_settings", lambda: {"analysis_timeout": 100, "stall_timeout": 100, timeout_key: 0.05})
    monkeypatch.setattr(worker, "cancellation_requested", lambda _: False)
    started = time.monotonic()
    with pytest.raises(worker.AnalysisTimeout):
        worker.run_validator(1, setup.inbox, setup.root / "report.json")
    assert time.monotonic() - started < 15


def test_modified_backup_is_rejected_before_replacing_database(setup):
    import zipfile
    report(setup)
    archive = setup.root / "backup.zip"
    maintenance.export_backup(archive)
    altered = setup.root / "altered.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(altered, "w") as target:
        target.writestr("database.sqlite", source.read("database.sqlite") + b"changed")
        target.writestr("manifest.json", source.read("manifest.json"))
    with pytest.raises(ValueError, match="checksum"):
        maintenance.restore_backup(altered)
    with setup.db() as session:
        assert session.scalar(select(AnalysisJob)) is None
