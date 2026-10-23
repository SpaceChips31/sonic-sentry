from app.models import Release


def recalculate_release_status(release: Release) -> None:
    if not release.tracks:
        release.status = "QUARANTINE"
    elif any(not t.integrity_ok or t.status == "REJECTED" or t.human_review == "REJECTED"
             for t in release.tracks):
        release.status = "REJECTED"
    elif any(t.status == "QUARANTINE" and t.human_review != "APPROVED" for t in release.tracks):
        release.status = "QUARANTINE"
    else:
        release.status = "PASS"
