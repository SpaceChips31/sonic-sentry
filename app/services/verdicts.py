from sqlalchemy import case, or_, and_
from app.models import Release, Track


def effective_track_status(track) -> str:
    """Current decision, retaining the original analysis status on Track.status."""
    if not track.integrity_ok or track.status == "REJECTED" or track.human_review == "REJECTED":
        return "REJECTED"
    if track.status == "QUARANTINE" and track.human_review != "APPROVED":
        return "QUARANTINE"
    return "PASS"


def effective_track_status_sql():
    return case(
        (or_(Track.integrity_ok == 0, Track.status == "REJECTED", Track.human_review == "REJECTED"), "REJECTED"),
        (and_(Track.status == "QUARANTINE", Track.human_review != "APPROVED"), "QUARANTINE"),
        else_="PASS",
    )


def recalculate_release_status(release: Release) -> None:
    statuses = {effective_track_status(track) for track in release.tracks}
    if "REJECTED" in statuses:
        release.status = "REJECTED"
    elif not statuses or "QUARANTINE" in statuses:
        release.status = "QUARANTINE"
    else:
        release.status = "PASS"
