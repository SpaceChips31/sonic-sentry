import json
from pathlib import Path

from app.main import spectral_comparison, spectrogram_command
from app.models import Release, Track
from app.database import Base, SessionLocal, engine
from app.models import ReleaseOperation
from app.services.file_workflow import move_release, undo_move


def forensic(cutoff: float, confidence: float) -> str:
    return json.dumps({
        "result": {"authenticity": {"spectral": {
            "cutoff_hz": cutoff,
            "net_confidence_pct": confidence,
        }}}
    })


def track(identifier: int, cutoff: float, confidence: float, status: str = "PASS") -> Track:
    return Track(
        id=identifier,
        release_id=1,
        path=f"/{identifier}.flac",
        display_path=f"{identifier}.flac",
        status=status,
        integrity_ok=1,
        forensic_json=forensic(cutoff, confidence),
        human_review="NONE",
    )


def test_album_comparison_uses_album_medians():
    current = track(1, 16_000, 70, "QUARANTINE")
    peers = [current, track(2, 22_000, 10), track(3, 21_000, 20)]
    result = spectral_comparison(current, peers)
    assert result["cutoff_median"] == 21_000
    assert result["confidence_median"] == 20
    assert result["cutoff_delta"] == -5_000


def test_spectrogram_command_is_non_interactive(tmp_path):
    command = spectrogram_command(Path("/music/track.flac"), tmp_path / "track.png")
    assert command[0] == "ffmpeg"
    assert "-y" in command
    assert "showspectrumpic" in " ".join(command)
    assert command[-1].endswith("track.png")


def test_album_move_can_be_undone_when_original_location_is_free(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    quarantine = tmp_path / "quarantine"
    album = source_root / "Album"
    album.mkdir(parents=True)
    audio = album / "track.flac"
    audio.write_bytes(b"fLaC")
    quarantine.mkdir()
    monkeypatch.setenv("LOSSLESS_FILE_OPERATIONS", "true")
    monkeypatch.setenv("LOSSLESS_MOVABLE_ROOTS", f"{source_root}:{quarantine}")
    monkeypatch.setenv("LOSSLESS_QUARANTINE_ROOT", str(quarantine))
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        release = Release(source_path=str(album), title="Album", status="QUARANTINE", total_tracks=1, pass_tracks=0, quarantine_tracks=1, rejected_tracks=0)
        release.tracks.append(Track(path=str(audio), display_path="track.flac", status="QUARANTINE", integrity_ok=1, human_review="NONE"))
        session.add(release)
        session.commit()
        release_id = release.id

    moved = move_release(release_id, "QUARANTINE")
    with SessionLocal() as session:
        operation = session.query(ReleaseOperation).filter_by(release_id=release_id).order_by(ReleaseOperation.id.desc()).first()
        operation_id = operation.id
    restored = undo_move(release_id, operation_id)
    assert moved.exists() is False
    assert restored == album
    assert audio.exists()

    with SessionLocal() as session:
        session.query(ReleaseOperation).filter_by(release_id=release_id).delete()
        session.query(Track).filter_by(release_id=release_id).delete()
        session.query(Release).filter_by(id=release_id).delete()
        session.commit()
