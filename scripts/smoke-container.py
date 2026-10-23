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
    engine.dispose()
