"""Run inside the built image: real FLAC, real validator, DB import and verified routing."""
import math
import os
import struct
import subprocess
import tempfile
import wave
from pathlib import Path


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    inbox = root / "inbox"
    album = inbox / "Smoke album"
    album.mkdir(parents=True)
    for name in ("data", "reports", "uploads", "staging", "quarantine", "rejected"):
        (root / name).mkdir()
    os.environ.update(
        LOSSLESS_VALIDATOR_DATA_DIR=str(root / "data"),
        LOSSLESS_SOURCE_ROOTS=str(inbox), LOSSLESS_MOVABLE_ROOTS=str(inbox),
        LOSSLESS_REPORT_ROOT=str(root / "reports"), LOSSLESS_UPLOAD_ROOT=str(root / "uploads"),
        LOSSLESS_STAGING_ROOT=str(root / "staging"), LOSSLESS_QUARANTINE_ROOT=str(root / "quarantine"),
        LOSSLESS_REJECTED_ROOT=str(root / "rejected"), LOSSLESS_FILE_OPERATIONS="true", LOSSLESS_AUTO_ROUTE="true",
    )
    with wave.open(str(album / "track.wav"), "wb") as audio:
        audio.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<hh", int(12000 * math.sin(i * 2 * math.pi * 440 / 44100)),
                                             int(10000 * math.sin(i * 2 * math.pi * 880 / 44100))) for i in range(44100)))
    subprocess.run(["flac", "--silent", str(album / "track.wav")], check=True)

    from sqlalchemy import select
    from app.database import engine, SessionLocal
    from app.migrations import initialize_database
    from app.models import AnalysisJob, FileTransfer, Release
    from app.services.queue import enqueue_paths
    from app.worker import claim_job, process_job

    initialize_database(engine)
    job_id = enqueue_paths([album], "SMOKE")[0]
    job = claim_job()
    assert job["id"] == job_id
    process_job(job)
    with SessionLocal() as session:
        result = session.get(AnalysisJob, job_id)
        assert result.status == "COMPLETED", result.error
        assert result.error is None, result.error
        release = session.get(Release, result.release_id)
        assert release.total_tracks == 1
        assert release.tracks[0].integrity_ok
        assert session.scalar(select(FileTransfer)).state == "COMPLETE"
        assert Path(release.source_path, "track.flac").is_file()
        assert {p.name for p in Path(release.source_path).iterdir()} == {"track.flac", "track.wav"}
        import json
        graph = Path(json.loads(release.tracks[0].forensic_json)["result"]["spectrogram_path"])
        assert graph.is_relative_to(root / "reports") and graph.is_file()
        assert not album.exists()
        print(f"Container smoke test passed: job #{job_id}, release {release.status}")
    from app.services.file_workflow import delete_release_files
    from app.services.trash import restore_trash
    from app.models import TrashEntry
    from app.maintenance import export_backup
    release_id = release.id
    original = Path(release.source_path)
    from app.services import file_workflow
    import errno
    native_rename = file_workflow.rename_exclusive
    def unsupported_rename(*args):
        raise OSError(errno.EINVAL, "simulated filesystem without exclusive rename")
    file_workflow.rename_exclusive = unsupported_rename
    delete_release_files(release_id)
    with SessionLocal() as session:
        entry = session.scalar(select(TrashEntry))
        assert entry.state == "TRASHED" and not original.exists()
        entry_id = entry.id
    restore_trash(entry_id)
    file_workflow.rename_exclusive = native_rename
    assert (original / "track.flac").is_file()
    from app.models import FileTask, AnalysisSource, ReconcileScan
    from app.services.file_tasks import preflight, enqueue_batch
    from app.services.reconciliation import enqueue_scan, apply_proposal
    if original.parent != root / "quarantine":
        file_workflow.move_release(release_id,"QUARANTINE")
    plan = preflight([release_id],"approve")
    assert plan["ready"], plan
    batch_id=enqueue_batch([release_id],"approve",plan["token"],"smoke")
    with SessionLocal() as session:
        assert session.scalar(select(FileTask)).state=="QUEUED"
    import time
    worker = subprocess.Popen(["python", "-m", "app.worker"])
    try:
        for _ in range(200):
            with SessionLocal() as session:
                task=session.scalar(select(FileTask).where(FileTask.batch_id==batch_id))
                if task.state=="COMPLETE":
                    break
                assert task.state not in {"FAILED","NEEDS_ATTENTION"}, task.error
            assert worker.poll() is None, "Worker exited during file task"
            time.sleep(0.1)
        else:
            raise AssertionError("Background task did not finish")
        with SessionLocal() as session:
            release=session.get(Release,release_id)
            track=release.tracks[0]
            identity,review=track.content_hash,track.human_review
            assert identity
            original=Path(release.source_path)
            tagged=root/"tagged"
            tagged.mkdir()
            session.add(AnalysisSource(name="Tagged",path=str(tagged),kind="FILESYSTEM",enabled=True))
            session.commit()
        new_album=tagged/"Tagged album"
        original.rename(new_album)
        new_track=new_album/"01 renamed.FLAC"
        (new_album/"track.flac").rename(new_track)
        subprocess.run(["metaflac","--set-tag=TITLE=Updated",str(new_track)],check=True)
        scan_id=enqueue_scan(str(tagged),"smoke")
        for _ in range(200):
            with SessionLocal() as session:
                scan=session.get(ReconcileScan,scan_id)
                if scan.state=="COMPLETE":
                    assert json.loads(scan.results_json)[0]["state"]=="FOUND",scan.results_json
                    break
                assert scan.state!="FAILED",scan.error
            assert worker.poll() is None
            time.sleep(0.1)
        else:
            raise AssertionError("Background reconciliation did not finish")
        apply_proposal(scan_id,release_id,"smoke")
        with SessionLocal() as session:
            release=session.get(Release,release_id)
            assert release.source_path==str(new_album)
            assert release.tracks[0].path==str(new_track)
            assert release.tracks[0].human_review==review
            assert release.tracks[0].content_hash==identity
        export_backup(root / "metadata.zip")
        subprocess.run(["python", "-m", "app.health"], check=True)
    finally:
        worker.terminate()
        worker.wait(timeout=20)
    print("Container real FLAC, fallback, background batch, tagged reconciliation, backup and health checks passed")
    engine.dispose()
