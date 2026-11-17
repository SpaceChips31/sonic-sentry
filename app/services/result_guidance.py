"""Presentation guidance derived from existing results; never changes analysis decisions."""
from app.services.review_reasons import classify

REASON_TEXT = {
    "integrity": "The FLAC integrity check failed. Try a fresh copy of this track before accepting the album.",
    "lossy": "The analysis found a signature compatible with lossy compression. Check the source and the detailed evidence before deciding.",
    "transcode": "The spectrum suggests earlier lossy compression or a resolution increase. This is an indicator, not proof of the recording's origin.",
    "cutoff": "A frequency limit was detected. Mastering and older recordings can also explain it; a cutoff alone does not prove lossy compression.",
    "uncertain": "The available result is inconclusive. Check the source and the evidence before deciding.",
    "pass": "The automatic checks passed. This does not certify the recording's origin or mastering.",
}


def needs_review(track):
    return bool(track.integrity_ok and track.status == "QUARANTINE" and track.human_review == "NONE")


def track_guidance(track):
    reason = classify(track.status, track.integrity_ok, track.forensic_verdict, track.forensic_json)
    if not track.integrity_ok:
        title, action, tone = "Damaged or unreadable file", "Obtain a fresh copy, then analyze it again.", "rejected"
    elif track.status == "REJECTED":
        title, action, tone = "Rejected by automatic checks", "Inspect the evidence or obtain a different source. This result cannot be manually approved.", "rejected"
    elif track.human_review == "REJECTED":
        title, action, tone = "Rejected by your review", "Your decision blocks album approval. You can change or reset the review below.", "rejected"
    elif track.human_review == "APPROVED":
        title, action, tone = "Approved by your review", "Your approval is recorded; the automatic evidence remains available below.", "pass"
    elif track.status == "PASS":
        title, action, tone = "Automatic checks passed", "No manual decision is required for this track.", "pass"
    else:
        title, action, tone = "Needs your review", "Approve only if the evidence and source are acceptable; otherwise reject or leave it pending.", "quarantine"
    return {"title": title, "explanation": REASON_TEXT[reason], "action": action, "tone": tone}


def album_guidance(release):
    tracks = list(release.tracks)
    broken = [t for t in tracks if not t.integrity_ok]
    rejected = [t for t in tracks if t.status == "REJECTED"]
    declined = [t for t in tracks if t.human_review == "REJECTED"]
    pending = [t for t in tracks if needs_review(t)]
    if not tracks:
        title, action, tone = "No track results available", "Run a fresh analysis before making a decision.", "quarantine"
    elif broken:
        title, action, tone = "Some files failed integrity checks", "Replace the damaged files and analyze the album again. Approval is blocked.", "rejected"
    elif rejected:
        title, action, tone = "Some tracks were automatically rejected", "Inspect the highlighted tracks and consider a different source. Manual approval cannot override these results.", "rejected"
    elif declined:
        title, action, tone = "You rejected tracks in this album", "Review your decisions below if you want to reconsider this album.", "rejected"
    elif pending:
        title, action, tone = "This album needs your review", "Start with the highlighted tracks. You can approve the album after checking their evidence and source.", "quarantine"
    elif release.status == "PASS":
        title, action, tone = "This album is approved", "No further review is needed. Check the file location below before moving it to staging.", "pass"
    else:
        title, action, tone = "Check the available results", "The stored album result is incomplete or inconsistent. Consider a fresh analysis.", "quarantine"
    priority = lambda t: (0 if not t.integrity_ok else 1 if t.status == "REJECTED" or t.human_review == "REJECTED" else 2 if needs_review(t) else 3, t.path)
    ordered = sorted(tracks, key=priority)
    issues = broken or rejected or declined or pending
    return {"title":title,"action":action,"tone":tone,"pending":len(pending),"broken":len(broken),
        "approved":sum(t.human_review == "APPROVED" for t in tracks), "tracks":ordered,
        "first_issue":issues[0].id if issues else None,
        "can_approve":bool(pending) and not (broken or rejected or declined),
        "can_stage":bool(tracks) and not (broken or rejected or declined)}
