import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app import main, worker
from app.migrations import initialize_database
from app.models import AnalysisJob, AnalysisSource, FileTransfer, Release, ReleaseOperation, SourceObservation, AnalyzedContent
from app.services import file_workflow, ingestion, queue, settings
from app.services.files import MARKER, audio_content_hash, snapshot
from test_workflow import configure, report


def test_upgrade_existing_database_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE analysis_jobs (id INTEGER PRIMARY KEY, source_type VARCHAR, source_path VARCHAR, display_name VARCHAR, status VARCHAR, created_at VARCHAR, error TEXT)"))
        conn.execute(text("INSERT INTO analysis_jobs VALUES (1,'FILESYSTEM','/music/Album','Album','COMPLETED','now',NULL)"))
        conn.execute(text("CREATE TABLE analysis_sources (id INTEGER PRIMARY KEY, name VARCHAR, path VARCHAR, kind VARCHAR, enabled BOOLEAN)"))
    initialize_database(engine)
    initialize_database(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT display_name, hidden, progress, cancel_requested FROM analysis_jobs")).one()
        assert tuple(row) == ("Album", 0, 0, 0)
    assert "file_transfers" in inspect(engine).get_table_names()
    engine.dispose()


def watch_source(env):
    with env.db() as session:
        source = AnalysisSource(name="Inbox", path=str(env.inbox), kind="FILESYSTEM", enabled=True, watch_enabled=True)
        session.add(source)
        session.commit()
        return source


def test_watcher_waits_for_stability_and_queues_once(setup):
    report(setup)
    watch_source(setup)
    now = time.time() + 1
    assert ingestion.scan_sources(now=now) == 0
    assert ingestion.scan_sources(now=now + 299) == 0
    assert ingestion.scan_sources(now=now + 301) == 1
    assert ingestion.scan_sources(now=now + 900) == 0
    with setup.db() as session:
        assert len(list(session.scalars(select(AnalysisJob)))) == 1
        assert session.scalar(select(SourceObservation)).state == "QUEUED"


def test_changed_or_partial_download_is_not_queued(setup):
    report(setup)
    watch_source(setup)
    now = time.time() + 1
    part = setup.inbox / "Album" / "track2.flac.part"
    part.write_bytes(b"partial")
    ingestion.scan_sources(now=now)
    assert ingestion.scan_sources(now=now + 600) == 0
    part.unlink()
    assert ingestion.scan_sources(now=now + 601) == 0
    assert ingestion.scan_sources(now=now + 901) == 1


def test_output_folders_and_hidden_partial_copies_are_excluded(setup):
    settings.save_workflow_settings(enabled=False, policy="manual", movable_roots=str(setup.inbox),
                                    staging=str(setup.inbox / "staging"), quarantine="", rejected="")
    (setup.inbox / "staging" / "Routed").mkdir(parents=True)
    (setup.inbox / "staging" / "Routed" / "track.flac").write_bytes(b"test")
    (setup.inbox / ".sonic-sentry-test.partial").mkdir()
    (setup.inbox / ".sonic-sentry-test.partial" / "track.flac").write_bytes(b"test")
    watch_source(setup)
    ingestion.scan_sources(now=time.time())
    assert ingestion.scan_sources(now=time.time() + 600) == 0
    with setup.db() as session:
        assert session.scalar(select(SourceObservation)) is None


def test_exact_duplicates_are_reported_without_touching_files(setup):
    path = report(setup)
    source = setup.inbox / "Album"
    watch_source(setup)
    from app.services.importer import import_report
    release = import_report(path)
    with setup.db() as session:
        session.add(AnalyzedContent(content_hash=audio_content_hash(source), release_id=release.id))
        session.commit()
    now = time.time() + 1
    ingestion.scan_sources(now=now)
    assert ingestion.scan_sources(now=now + 600) == 0
    with setup.db() as session:
        assert session.scalar(select(SourceObservation)).state == "DUPLICATE"
    assert (source / "track.flac").read_bytes() == b"test audio"


def test_pending_identical_album_waits_and_failed_content_can_still_be_analyzed(setup):
    report(setup)
    twin = setup.inbox / "Twin"
    twin.mkdir()
    (twin / "renamed.flac").write_bytes(b"test audio")
    watch_source(setup)
    now = time.time() + 1
    ingestion.scan_sources(now=now)
    assert ingestion.scan_sources(now=now + 600) == 1
    with setup.db() as session:
        job = session.scalar(select(AnalysisJob))
        job.status = "FAILED"
        session.commit()
    assert ingestion.scan_sources(now=now + 900) == 1


def test_queue_prevents_overlapping_parent_and_child_jobs(setup):
    report(setup)
    ids = queue.enqueue_paths([setup.inbox], "FILESYSTEM")
    assert ids
    assert queue.enqueue_paths([setup.inbox / "Album"], "FILESYSTEM") == []


def interrupted_transfer(env, monkeypatch, failure):
    configure(env)
    from app.services.importer import import_report
    release = import_report(report(env))
    original = file_workflow.rename_exclusive
    def interrupt(*args):
        if failure == "after_publish":
            original(*args)
        raise SystemExit("simulated power loss")
    monkeypatch.setattr(file_workflow, "rename_exclusive", interrupt)
    with pytest.raises(SystemExit):
        file_workflow.move_release(release.id, "STAGING")
    monkeypatch.setattr(file_workflow, "rename_exclusive", original)
    with env.db() as session:
        transfer = session.scalar(select(FileTransfer))
        assert transfer.state == "COPYING"
    return release, transfer


@pytest.mark.parametrize("failure", ["before_publish", "after_publish"])
def test_transfer_recovers_after_power_loss_without_duplicate_audit(setup, monkeypatch, failure):
    release, transfer = interrupted_transfer(setup, monkeypatch, failure)
    assert file_workflow.recover_transfers() == 1
    assert file_workflow.resume_transfer(transfer.id) == setup.root / "staging" / "Album"
    assert not (setup.inbox / "Album").exists()
    with setup.db() as session:
        assert session.get(FileTransfer, transfer.id).state == "COMPLETE"
        assert session.get(Release, release.id).source_path == str(setup.root / "staging" / "Album")
        assert len(list(session.scalars(select(ReleaseOperation)))) == 1


def test_transfer_preserves_changed_source_after_interruption(setup, monkeypatch):
    release, transfer = interrupted_transfer(setup, monkeypatch, "after_publish")
    (setup.inbox / "Album" / "track.flac").write_bytes(b"new user content")
    with pytest.raises(file_workflow.FileOperationError, match="changed files"):
        file_workflow.resume_transfer(transfer.id)
    assert (setup.inbox / "Album" / "track.flac").read_bytes() == b"new user content"
    assert (setup.root / "staging" / "Album" / "track.flac").read_bytes() == b"test audio"


def test_cleanup_resumes_after_partial_source_removal(setup, monkeypatch):
    configure(setup)
    from app.services.importer import import_report
    path = report(setup)
    (setup.inbox / "Album" / "cover.jpg").write_bytes(b"cover")
    release = import_report(path)
    original = Path.unlink
    def interrupt(path, *args, **kwargs):
        if path.name == "track.flac" and path.parent == setup.inbox / "Album":
            raise SystemExit("power loss during cleanup")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", interrupt)
    with pytest.raises(SystemExit):
        file_workflow.move_release(release.id, "STAGING")
    monkeypatch.setattr(Path, "unlink", original)
    assert file_workflow.recover_transfers() == 1
    assert not (setup.inbox / "Album").exists()
    assert (setup.root / "staging" / "Album" / "cover.jpg").read_bytes() == b"cover"


def test_cancel_retry_bulk_review_activity_and_hidden_history(setup):
    from app.services.importer import import_report
    release = import_report(report(setup, "QUARANTINE"))
    job_id = queue.enqueue_paths([setup.inbox / "Album"], "FILESYSTEM")[0]
    with TestClient(main.app) as client:
        assert client.post(f"/analyses/{job_id}/cancel", follow_redirects=False).status_code == 303
        with setup.db() as session:
            assert session.get(AnalysisJob, job_id).status == "CANCELLED"
        assert client.post("/releases/review", data={"release_ids": [release.id], "decision": "approve"}, follow_redirects=False).status_code == 303
        with setup.db() as session:
            assert session.get(Release, release.id).status == "PASS"
        assert client.get("/activity").status_code == 200
        assert client.post("/analyses/clear/cancelled", follow_redirects=False).status_code == 303
        with setup.db() as session:
            assert session.get(AnalysisJob, job_id).hidden
        assert client.post(f"/analyses/{job_id}/retry", follow_redirects=False).status_code == 303
        with setup.db() as session:
            assert not session.get(AnalysisJob, job_id).hidden
            assert not session.get(AnalysisJob, job_id).cancel_requested


def test_settings_updates_preserve_other_settings(setup):
    configure(setup)
    settings.save_ingestion_settings(90, 600)
    configure(setup, "manual")
    assert settings.workflow_settings()["settle_seconds"] == 600
    assert settings.workflow_settings()["scan_interval"] == 90
    assert settings.workflow_settings()["auto_route"] is False


def test_cancelled_job_never_runs_validator(setup, monkeypatch):
    report(setup)
    job_id = queue.enqueue_paths([setup.inbox / "Album"], "FILESYSTEM")[0]
    with setup.db() as session:
        job = session.get(AnalysisJob, job_id)
        job.status, job.cancel_requested = "ANALYZING", True
        session.commit()
    def fail(*args):
        pytest.fail("validator must not start after cancellation")
    monkeypatch.setattr(worker, "run_validator", fail)
    worker.process_job({"id": job_id, "source_path": str(setup.inbox / "Album")})
    with setup.db() as session:
        assert session.get(AnalysisJob, job_id).status == "CANCELLED"


def test_completed_analysis_records_explicit_result_and_content_hash(setup, monkeypatch):
    path = report(setup)
    payload = path.read_text()
    job_id = queue.enqueue_paths([setup.inbox / "Album"], "FILESYSTEM")[0]
    monkeypatch.setattr(worker, "REPORT_ROOT", setup.root)
    def run(job_id, source, destination):
        destination.write_text(payload)
        return SimpleNamespace(stdout="OK", returncode=0)
    monkeypatch.setattr(worker, "run_validator", run)
    worker.process_job({"id": job_id, "source_path": str(setup.inbox / "Album")})
    with setup.db() as session:
        job = session.get(AnalysisJob, job_id)
        assert job.status == "COMPLETED"
        assert job.release_id is not None
        assert job.progress == job.progress_total == 1
        assert session.scalar(select(AnalyzedContent)).release_id == job.release_id


def test_changed_album_is_not_imported_or_routed(setup, monkeypatch):
    path = report(setup)
    payload = path.read_text()
    job_id = queue.enqueue_paths([setup.inbox / "Album"], "FILESYSTEM")[0]
    monkeypatch.setattr(worker, "REPORT_ROOT", setup.root)
    def run(job_id, source, destination):
        destination.write_text(payload)
        (source / "track.flac").write_bytes(b"still downloading")
        return SimpleNamespace(stdout="OK", returncode=0)
    monkeypatch.setattr(worker, "run_validator", run)
    worker.process_job({"id": job_id, "source_path": str(setup.inbox / "Album")})
    with setup.db() as session:
        job = session.get(AnalysisJob, job_id)
        assert job.status == "FAILED"
        assert "changed during analysis" in job.error
        assert session.scalar(select(Release)) is None


def test_running_validator_can_be_cancelled(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import update
    fake = setup.root / "fake_validator.py"
    fake.write_text('import sys, json, time\nfrom pathlib import Path\n'
                    'path = Path(sys.argv[sys.argv.index("--progress") + 1])\n'
                    'path.write_text(json.dumps({"completed": 1, "total": 2}))\n'
                    'time.sleep(30)\n')
    report(setup)
    job_id = queue.enqueue_paths([setup.inbox / "Album"], "FILESYSTEM")[0]
    monkeypatch.setattr(worker, "VALIDATOR", fake)
    output = setup.root / "result.json"
    with ThreadPoolExecutor() as pool:
        future = pool.submit(worker.run_validator, job_id, setup.inbox / "Album", output)
        try:
            deadline = time.monotonic() + 10
            while not output.with_suffix(".progress.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert output.with_suffix(".progress.json").exists()
        finally:
            with setup.db() as session:
                session.execute(update(AnalysisJob).where(AnalysisJob.id == job_id).values(cancel_requested=True))
                session.commit()
        with pytest.raises(worker.AnalysisCancelled):
            future.result(timeout=10)
    assert not output.exists()


def test_worker_recovery_does_not_reanalyze_a_moved_album(setup, monkeypatch):
    configure(setup)
    from app.services.importer import import_report
    release = import_report(report(setup))
    job_id = queue.enqueue_paths([setup.inbox / "Album"], "FILESYSTEM")[0]
    with setup.db() as session:
        job = session.get(AnalysisJob, job_id)
        job.status, job.release_id = "ROUTING", release.id
        session.commit()
    file_workflow.move_release(release.id, "STAGING", current_job_id=job_id)
    assert worker.recover_interrupted_jobs() == 1
    with setup.db() as session:
        job = session.get(AnalysisJob, job_id)
        assert job.status == "COMPLETED"
        assert job.error is None


def test_operator_can_close_stopped_transfer_while_preserving_every_file(setup, monkeypatch):
    release, transfer = interrupted_transfer(setup, monkeypatch, "after_publish")
    (setup.inbox / "Album" / "track.flac").write_bytes(b"new user content")
    with pytest.raises(file_workflow.FileOperationError):
        file_workflow.resume_transfer(transfer.id)
    file_workflow.close_transfer(transfer.id)
    assert (setup.inbox / "Album" / "track.flac").read_bytes() == b"new user content"
    assert (setup.root / "staging" / "Album" / "track.flac").read_bytes() == b"test audio"
    with setup.db() as session:
        assert session.get(FileTransfer, transfer.id).state == "ABANDONED"
        file_workflow.check_pending_transfer(session, release.id)
    with pytest.raises(file_workflow.FileOperationError, match="closed"):
        file_workflow.resume_transfer(transfer.id)


def test_reanalysis_invalidates_outdated_content_identity(setup):
    from app.services.importer import import_report
    path = report(setup)
    release = import_report(path)
    with setup.db() as session:
        session.add(AnalyzedContent(content_hash="old-content", release_id=release.id))
        session.commit()
    import_report(path)
    with setup.db() as session:
        assert session.get(AnalyzedContent, "old-content") is None
