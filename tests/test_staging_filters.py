import errno
from pathlib import Path

import pytest
from sqlalchemy import select

from client import TestClient
from app import main
from app.models import AnalysisSource, FileTransfer, Release, ReviewEvent
from app.services import file_workflow, importer, inventory, queue, file_tasks
from app.services.files import contains_flac
from test_workflow import configure, report


def quarantine(env, title, status="QUARANTINE", integrity=True):
    path = report(env, status, integrity)
    import json
    data = json.loads(path.read_text())
    old = env.inbox / "Album"
    new = env.inbox / title
    old.rename(new)
    data["release"] = str(new)
    data["files"][0]["path"] = str(new / "track.flac")
    path.write_text(json.dumps(data))
    release = importer.import_report(path)
    file_workflow.move_release(release.id, "QUARANTINE")
    return release


def test_filter_finds_uppercase_flac_in_nested_discs_but_not_other_audio(setup):
    artist = setup.inbox / "Artist" / "Album" / "CD1"
    artist.mkdir(parents=True)
    (artist / "track.FLAC").write_bytes(b"audio")
    (setup.inbox / "MP3").mkdir()
    (setup.inbox / "MP3" / "track.mp3").write_bytes(b"audio")
    (setup.inbox / "Empty").mkdir()
    hidden = setup.inbox / "Hidden" / ".partial"
    hidden.mkdir(parents=True)
    (hidden / "track.flac").write_bytes(b"audio")
    assert contains_flac(setup.inbox / "Artist")
    assert not contains_flac(setup.inbox / "MP3")
    assert not contains_flac(setup.inbox / "Hidden")
    with setup.db() as session:
        source = AnalysisSource(name="Inbox",path=str(setup.inbox),kind="FILESYSTEM",enabled=True)
        session.add(source)
        session.commit()
        source_id = source.id
    with TestClient(main.app) as client:
        response = client.get(f"/api/sources/{source_id}/directories")
        assert [row["name"] for row in response.json()["directories"]] == ["Artist"]
        page = client.get(f"/analyses/new?source={source_id}").text
        assert "Artist" in page and "MP3" not in page and "Empty" not in page


def test_destination_filter_and_explicit_diagnostics(setup):
    configure(setup)
    release = quarantine(setup, "FLAC album")
    empty = setup.root / "quarantine" / "Empty"
    empty.mkdir()
    assert [row["name"] for row in inventory.destination_inventory("quarantine")["rows"]] == ["FLAC album"]
    assert len(inventory.destination_inventory("quarantine", show_all=True)["rows"]) == 2
    with TestClient(main.app) as client:
        assert "Empty" not in client.get("/destinations/quarantine").text
        assert "Empty" in client.get("/destinations/quarantine?show_all=true").text


def submit_batch(client, ids, mode):
    response=client.post("/destinations/quarantine/staging",data={"release_ids":ids,"mode":mode})
    assert response.status_code==200
    plan=file_tasks.preflight(ids,mode)
    if not plan["ready"]:
        return response
    queued=client.post("/file-batches",data={"release_ids":ids,"mode":mode,"token":plan["token"]},follow_redirects=False)
    assert queued.status_code==303
    while file_tasks.process_next_task():
        pass
    return client.get(queued.headers["location"])


def test_bulk_approval_moves_multiple_albums_and_audits_actor(setup):
    configure(setup)
    albums = [quarantine(setup, name) for name in ("One", "Two")]
    with TestClient(main.app) as client:
        response = submit_batch(client,[a.id for a in albums],"approve")
        assert response.status_code == 200
        assert "2 / 2" in response.text
        assert "One" in response.text and "Two" in response.text
    for album in albums:
        assert (setup.root / "staging" / album.title / "track.flac").is_file()
    with setup.db() as session:
        assert len(list(session.scalars(select(ReviewEvent)))) == 2
        assert all(event.actor == "local" for event in session.scalars(select(ReviewEvent)))
        assert all(album.status == "PASS" for album in session.scalars(select(Release)))


def test_bulk_does_not_approve_without_explicit_mode_and_continues_safe_albums(setup):
    configure(setup)
    uncertain = quarantine(setup, "Uncertain")
    approved = quarantine(setup, "Approved", "PASS")
    with TestClient(main.app) as client:
        response = submit_batch(client,[uncertain.id,approved.id],"approved")
        assert response.status_code == 200 and "1 / 1" in response.text
    assert (setup.root / "quarantine" / "Uncertain" / "track.flac").exists()
    assert (setup.root / "staging" / "Approved" / "track.flac").exists()
    with setup.db() as session:
        assert session.scalar(select(ReviewEvent)) is None


def test_bulk_integrity_failures_active_jobs_and_outside_paths_stay_put(setup):
    configure(setup)
    broken = quarantine(setup, "Broken", "REJECTED", False)
    busy = quarantine(setup, "Busy")
    outside = importer.import_report(report(setup))
    queue.enqueue_paths([setup.root / "quarantine" / "Busy"], "TEST")
    with TestClient(main.app) as client:
        response = client.post("/destinations/quarantine/staging", data={"release_ids":[broken.id,busy.id,outside.id],"mode":"approve"})
        assert response.status_code == 200 and "Blocked" in response.text
    assert (setup.root / "quarantine" / "Broken" / "track.flac").exists()
    assert (setup.root / "quarantine" / "Busy" / "track.flac").exists()
    assert (setup.inbox / "Album" / "track.flac").exists()


def unsupported(*args):
    raise OSError(errno.EINVAL, "filesystem does not support exclusive rename")


def test_portable_fallback_and_existing_failed_journal_resume(setup, monkeypatch):
    configure(setup)
    release = importer.import_report(report(setup))
    def stopped(*args):
        raise OSError(errno.EACCES, "not permitted")
    monkeypatch.setattr(file_workflow, "rename_exclusive", stopped)
    with pytest.raises(file_workflow.FileOperationError):
        file_workflow.move_release(release.id, "STAGING")
    with setup.db() as session:
        transfer = session.scalar(select(FileTransfer))
    monkeypatch.setattr(file_workflow, "rename_exclusive", unsupported)
    destination = file_workflow.resume_transfer(transfer.id)
    assert (destination / "track.flac").read_bytes() == b"test audio"
    assert not Path(transfer.source_path).exists()
    assert not Path(transfer.temporary_path).exists()
    with setup.db() as session:
        assert session.get(FileTransfer, transfer.id).state == "COMPLETE"


def test_portable_fallback_resumes_partial_publication(setup, monkeypatch):
    configure(setup)
    release = importer.import_report(report(setup))
    (setup.inbox / "Album" / "cover.jpg").write_bytes(b"cover")
    monkeypatch.setattr(file_workflow, "rename_exclusive", unsupported)
    original = file_workflow.shutil.copystat
    def crash(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination) == setup.root / "staging" / "Album" / "cover.jpg":
            raise SystemExit("power loss")
        return result
    monkeypatch.setattr(file_workflow.shutil, "copystat", crash)
    with pytest.raises(SystemExit):
        file_workflow.move_release(release.id, "STAGING")
    monkeypatch.setattr(file_workflow.shutil, "copystat", original)
    with setup.db() as session:
        transfer = session.scalar(select(FileTransfer))
    assert (setup.inbox / "Album" / "track.flac").exists()
    destination = file_workflow.resume_transfer(transfer.id)
    assert (destination / "cover.jpg").read_bytes() == b"cover"
    assert (destination / "track.flac").read_bytes() == b"test audio"


def test_portable_fallback_never_adopts_an_unowned_destination(setup, monkeypatch):
    configure(setup)
    release = importer.import_report(report(setup))
    def race(source, destination):
        destination.mkdir()
        (destination / "mine.txt").write_bytes(b"keep")
        unsupported()
    monkeypatch.setattr(file_workflow, "rename_exclusive", race)
    with pytest.raises(file_workflow.FileOperationError, match="ownership"):
        file_workflow.move_release(release.id, "STAGING")
    assert (setup.root / "staging" / "Album" / "mine.txt").read_bytes() == b"keep"
    assert (setup.inbox / "Album" / "track.flac").exists()


def test_portable_resume_preserves_modified_destination_and_all_source_files(setup, monkeypatch):
    configure(setup)
    release = importer.import_report(report(setup))
    monkeypatch.setattr(file_workflow, "rename_exclusive", unsupported)
    original = file_workflow.shutil.copystat
    def crash(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination) == setup.root / "staging" / "Album" / "track.flac":
            raise SystemExit("power loss")
        return result
    monkeypatch.setattr(file_workflow.shutil, "copystat", crash)
    with pytest.raises(SystemExit):
        file_workflow.move_release(release.id, "STAGING")
    monkeypatch.setattr(file_workflow.shutil, "copystat", original)
    changed = setup.root / "staging" / "Album" / "track.flac"
    changed.write_bytes(b"new user contents")
    with setup.db() as session:
        transfer = session.scalar(select(FileTransfer))
    with pytest.raises(file_workflow.FileOperationError, match="changed or added"):
        file_workflow.resume_transfer(transfer.id)
    assert changed.read_bytes() == b"new user contents"
    assert (setup.inbox / "Album" / "track.flac").read_bytes() == b"test audio"
    assert (Path(transfer.temporary_path) / "track.flac").read_bytes() == b"test audio"


def test_permission_errors_do_not_trigger_portable_publication(setup, monkeypatch):
    configure(setup)
    release = importer.import_report(report(setup))
    def denied(*args):
        raise OSError(errno.EACCES, "permission denied")
    monkeypatch.setattr(file_workflow, "rename_exclusive", denied)
    with pytest.raises(file_workflow.FileOperationError):
        file_workflow.move_release(release.id, "STAGING")
    assert not (setup.root / "staging" / "Album").exists()
    assert (setup.inbox / "Album" / "track.flac").exists()
