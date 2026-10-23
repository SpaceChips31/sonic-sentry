import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker, selectinload

from app import config, main, worker, workflow
from app.database import Base
from app.models import AnalysisJob, AnalysisSource, Release, ReleaseOperation
from app.services import file_workflow, importer, settings, sources




def configure(env, policy="pass"):
    settings.save_workflow_settings(enabled=True, policy=policy, movable_roots=str(env.inbox), **env.targets)


def report(env, status="PASS", integrity=True):
    album = env.inbox / "Album"
    album.mkdir(exist_ok=True)
    audio = album / "track.flac"
    audio.write_bytes(b"test audio")
    path = env.root / "report.json"
    path.write_text(json.dumps({"schema_version": 1, "release": str(album), "status": status,
                               "files": [{"path": str(audio), "status": status, "integrity": {"ok": integrity}}]}))
    return path


def test_settings_default_to_environment_then_persist_for_new_sessions(setup):
    assert settings.workflow_settings()["enabled"] is False
    configure(setup)
    assert settings.workflow_settings()["auto_route"] is True
    assert settings.workflow_settings()["route_nonpass"] is False
    with setup.db() as session:
        assert session.get(settings.WorkflowSettings, 1) is not None


@pytest.mark.parametrize("policy", ["pass", "all"])
def test_approved_album_is_routed_without_confirmation(setup, policy):
    configure(setup, policy)
    release = importer.import_report(report(setup))
    destination = file_workflow.auto_route_release(release.id)
    assert destination == setup.root / "staging" / "Album"
    assert (destination / "track.flac").read_bytes() == b"test audio"
    assert not (setup.inbox / "Album").exists()
    with setup.db() as session:
        saved = session.scalar(select(Release).options(selectinload(Release.tracks), selectinload(Release.operations)))
        assert saved.source_path == str(destination)
        assert saved.tracks[0].path == str(destination / "track.flac")
        assert saved.operations[0].action == "MOVE_TO_STAGING"


@pytest.mark.parametrize("status", ["QUARANTINE", "REJECTED"])
def test_pass_only_policy_leaves_nonpass_albums_in_place(setup, status):
    configure(setup)
    release = importer.import_report(report(setup, status))
    assert file_workflow.auto_route_release(release.id) is None
    assert (setup.inbox / "Album").is_dir()


@pytest.mark.parametrize("status, folder", [("QUARANTINE", "quarantine"), ("REJECTED", "rejected")])
def test_all_verdict_policy_uses_separate_destinations(setup, status, folder):
    configure(setup, "all")
    release = importer.import_report(report(setup, status))
    assert file_workflow.auto_route_release(release.id) == setup.root / folder / "Album"


def test_manual_policy_does_not_move_files(setup):
    configure(setup, "manual")
    release = importer.import_report(report(setup))
    assert file_workflow.auto_route_release(release.id) is None
    assert (setup.inbox / "Album").exists()


def test_existing_destination_is_never_overwritten(setup):
    configure(setup)
    release = importer.import_report(report(setup))
    destination = setup.root / "staging" / "Album"
    destination.mkdir()
    with pytest.raises(file_workflow.FileOperationError, match="already exists"):
        file_workflow.auto_route_release(release.id)
    assert (setup.inbox / "Album" / "track.flac").exists()
    assert list(destination.iterdir()) == []


def test_corrupt_tracks_cannot_be_manually_approved(setup):
    configure(setup)
    release = importer.import_report(report(setup, "PASS", integrity=False))
    assert release.status == "REJECTED"
    with pytest.raises(file_workflow.FileOperationError, match="integrity"):
        file_workflow.move_release(release.id, "STAGING", manual_pass=True)
    assert (setup.inbox / "Album").exists()


def test_manual_approval_keeps_automatic_verdict(setup):
    configure(setup)
    release = importer.import_report(report(setup, "QUARANTINE"))
    file_workflow.move_release(release.id, "STAGING", manual_pass=True)
    with setup.db() as session:
        saved = session.scalar(select(Release).options(selectinload(Release.tracks)))
        assert saved.status == "PASS"
        assert saved.tracks[0].status == "QUARANTINE"
        assert saved.tracks[0].human_review == "APPROVED"


def test_root_folders_and_active_jobs_are_protected(setup):
    configure(setup)
    with pytest.raises(file_workflow.FileOperationError, match="configured root"):
        file_workflow.validate_source(setup.inbox, settings.workflow_settings())
    release = importer.import_report(report(setup))
    with setup.db() as session:
        job = AnalysisJob(source_type="FILESYSTEM", source_path=release.source_path,
                          display_name="Album", status="ANALYZING", created_at="now")
        session.add(job)
        session.commit()
        job_id = job.id
    with pytest.raises(file_workflow.FileOperationError, match="active analysis"):
        file_workflow.move_release(release.id, "STAGING")
    assert file_workflow.auto_route_release(release.id, current_job_id=job_id).exists()


def test_invalid_settings_leave_previous_settings_unchanged(setup):
    configure(setup)
    before = settings.workflow_settings()
    with pytest.raises(settings.WorkflowConfigurationError):
        settings.save_workflow_settings(enabled=True, policy="all", movable_roots=str(setup.inbox),
                                        **{**setup.targets, "staging": setup.targets["quarantine"]})
    assert settings.workflow_settings() == before


def test_destination_can_also_be_an_authorized_root(setup):
    settings.save_workflow_settings(enabled=True, policy="all",
                                    movable_roots=f"{setup.inbox}\n{setup.targets['quarantine']}", **setup.targets)
    assert setup.targets["quarantine"] in settings.workflow_settings()["movable_roots"]


def test_automation_can_be_disabled_when_a_mount_disappears(setup):
    configure(setup)
    (setup.root / "staging").rmdir()
    settings.save_workflow_settings(enabled=False, policy="manual", movable_roots=str(setup.inbox), **setup.targets)
    assert settings.workflow_settings()["enabled"] is False


def test_move_failure_rolls_back_manual_approval(setup, monkeypatch):
    configure(setup)
    release = importer.import_report(report(setup, "QUARANTINE"))
    def fail(*args, **kwargs):
        raise OSError("read only")
    monkeypatch.setattr(file_workflow.shutil, "copytree", fail)
    with pytest.raises(file_workflow.FileOperationError, match="could not move"):
        file_workflow.move_release(release.id, "STAGING", manual_pass=True)
    with setup.db() as session:
        saved = session.scalar(select(Release).options(selectinload(Release.tracks)))
        assert saved.status == "QUARANTINE"
        assert saved.tracks[0].human_review == "NONE"
        assert saved.source_path == release.source_path


def test_manual_deletion_records_history_without_deleting_results(setup):
    configure(setup)
    release = importer.import_report(report(setup))
    file_workflow.delete_release_files(release.id)
    assert not (setup.inbox / "Album").exists()
    with setup.db() as session:
        assert session.get(Release, release.id) is not None
        assert session.scalar(select(ReleaseOperation)).action == "DELETE"


def test_reimport_preserves_release_identity_and_audit(setup):
    path = report(setup)
    first = importer.import_report(path)
    with setup.db() as session:
        session.add(ReleaseOperation(release_id=first.id, action="TEST", source_path=first.source_path, created_at="now"))
        session.commit()
    second = importer.import_report(path)
    assert second.id == first.id
    assert second.total_tracks == 1
    with setup.db() as session:
        assert session.scalar(select(ReleaseOperation)).action == "TEST"


def test_report_with_external_track_is_rejected(setup):
    path = report(setup)
    payload = json.loads(path.read_text())
    payload["files"][0]["path"] = str(setup.root / "outside.flac")
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="inside the release"):
        importer.import_report(path)


def test_pages_settings_validation_search_retry_and_upload(setup, monkeypatch):
    release = importer.import_report(report(setup))
    monkeypatch.setattr(main, "UPLOAD_ROOT", setup.root / "uploads")
    with setup.db() as session:
        session.add(AnalysisSource(name="Inbox", path=str(setup.inbox), kind="FILESYSTEM", enabled=True))
        session.add(AnalysisJob(source_type="FILESYSTEM", source_path=release.source_path,
                                display_name="Album", status="FAILED", created_at="now"))
        session.commit()
    with TestClient(main.app) as client:
        for url in ("/", "/settings", "/analyses", "/analyses/new", "/releases", f"/releases/{release.id}"):
            response = client.get(url)
            assert response.status_code == 200, response.text
        assert "Album" in client.get("/releases?q=Album&status=PASS").text
        assert "No matching releases." in client.get("/releases?q=missing", headers={"Accept-Language": "en"}).text
        response = client.post("/settings/workflow", data={"enabled": "true", "policy": "pass",
                               "movable_roots": str(setup.inbox), **setup.targets})
        assert response.status_code == 200
        assert settings.workflow_settings()["auto_route"]
        response = client.post("/settings/workflow", data={"enabled": "true", "policy": "all"})
        assert response.status_code == 400
        assert 'role="alert"' in response.text
        assert client.post("/analyses/1/retry", follow_redirects=False).status_code == 303
        assert client.post("/analyses/1/retry").status_code == 409
        body = (b'--boundary\r\nContent-Disposition: form-data; name="files"; filename="track.flac"\r\n'
                b'Content-Type: audio/flac\r\n\r\naudio\r\n--boundary\r\n'
                b'Content-Disposition: form-data; name="files"; filename=""\r\n'
                b'Content-Type: application/octet-stream\r\n\r\n\r\n--boundary--\r\n')
        response = client.post("/analyses/upload", content=body,
                               headers={"Content-Type": "multipart/form-data; boundary=boundary"}, follow_redirects=False)
        assert response.status_code == 303


def test_routing_failure_keeps_analysis_completed_with_warning(setup, monkeypatch):
    configure(setup)
    path = report(setup)
    payload = path.read_text()
    (setup.root / "staging" / "Album").mkdir()
    monkeypatch.setattr(worker, "REPORT_ROOT", setup.root)
    def run(*args, **kwargs):
        (setup.root / "job-1.json").write_text(payload)
        return SimpleNamespace(stdout="OK", returncode=0)
    monkeypatch.setattr(worker, "run_validator", run)
    with setup.db() as session:
        session.add(AnalysisJob(id=1, source_type="FILESYSTEM", source_path=str(setup.inbox / "Album"),
                                display_name="Album", status="ANALYZING", created_at="now"))
        session.commit()
    worker.process_job({"id": 1, "source_path": str(setup.inbox / "Album")})
    with setup.db() as session:
        job = session.get(AnalysisJob, 1)
        assert job.status == "COMPLETED"
        assert "automatic routing needs attention" in job.error
