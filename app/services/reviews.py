from app.models import ReviewEvent
from app.services.file_workflow import utc_now


def set_review(session, track, decision: str, note: str = "", actor: str = "local") -> None:
    if decision not in {"NONE", "APPROVED", "REJECTED"} or len(note) > 2000:
        raise ValueError("Invalid review decision or note longer than 2000 characters")
    previous = track.human_review
    if previous != decision or note.strip():
        session.add(ReviewEvent(release_id=track.release_id, track_path=track.display_path,
            previous=previous, decision=decision, note=note.strip(), actor=actor, created_at=utc_now()))
    track.human_review = decision
