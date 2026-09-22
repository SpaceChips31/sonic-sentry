from pathlib import Path

import pytest
from fastapi import HTTPException

from app.main import health, safe_upload_path
from validator.validate_release import classify


def test_health_exposes_version():
    payload = health()
    assert payload["status"] == "ok"
    assert payload["version"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("track.flac", Path("track.flac")),
        ("CD 01/track.flac", Path("CD 01/track.flac")),
        ("artwork/cover.jpg", Path("artwork/cover.jpg")),
    ],
)
def test_safe_upload_path_accepts_relative_paths(filename, expected):
    assert safe_upload_path(filename) == expected


@pytest.mark.parametrize(
    "filename",
    ["", ".", "../track.flac", "album/../../track.flac", "/etc/passwd"],
)
def test_safe_upload_path_rejects_unsafe_paths(filename):
    with pytest.raises(HTTPException):
        safe_upload_path(filename)


def test_classify_accepts_genuine_result():
    forensic = {"ok": True, "result": {"authenticity": {"spectral": {"verdict_label": "GENUINE"}}}}
    assert classify(forensic) == "PASS"


@pytest.mark.parametrize(
    "forensic",
    [
        {"ok": False},
        {"ok": True, "result": {"authenticity": {"spectral": {"verdict_label": "SUSPICIOUS"}}}},
    ],
)
def test_classify_quarantines_non_genuine_results(forensic):
    assert classify(forensic) == "QUARANTINE"
