import json
from pathlib import Path

from app.main import spectral_comparison, spectrogram_command
from app.models import Release, Track


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
