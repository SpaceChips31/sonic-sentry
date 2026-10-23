import json
from pathlib import Path

from sqlalchemy import select, delete

from app.database import SessionLocal
from app.models import Release, Track, AnalyzedContent
from app.services.verdicts import recalculate_release_status
from app.services.file_workflow import lock_file_operations, check_pending_transfer


def import_report(report_path: Path) -> Release:
    report_path = report_path.resolve()
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported or missing report schema_version")

    source_path = str(Path(payload["release"]).resolve())
    source = Path(source_path)
    files = payload["files"]
    if not files:
        raise ValueError("Report contains no tracks")
    paths = set()
    for item in files:
        path = Path(item["path"]).resolve()
        if not path.is_relative_to(source) or path == source or path in paths:
            raise ValueError("Track paths must be unique and inside the release")
        if item["status"] not in {"PASS", "QUARANTINE", "REJECTED"}:
            raise ValueError("Invalid track status")
        if not isinstance(item["integrity"]["ok"], bool):
            raise ValueError("Invalid integrity result")
        if not item["integrity"]["ok"]:
            item["status"] = "REJECTED"
        paths.add(path)

    with SessionLocal() as session:
        lock_file_operations(session)
        existing = session.scalar(
            select(Release).where(Release.source_path == source_path)
        )

        if existing is not None:
            check_pending_transfer(session, existing.id)
            session.execute(delete(AnalyzedContent).where(AnalyzedContent.release_id == existing.id))
            # Keep release identity and file-operation history across reanalysis.
            existing.tracks.clear()
            session.flush()
        release = existing if existing is not None else Release(source_path=source_path, title=source.name, status="QUARANTINE")
        release.analysed_at = payload.get("analysed_at")
        release.total_tracks = len(files)
        release.pass_tracks = sum(item["status"] == "PASS" for item in files)
        release.quarantine_tracks = sum(item["status"] == "QUARANTINE" for item in files)
        release.rejected_tracks = sum(item["status"] == "REJECTED" for item in files)

        session.add(release)
        session.flush()

        for item in files:
            path = Path(item["path"]).resolve()

            try:
                display_path = str(path.relative_to(source))
            except ValueError:
                display_path = path.name

            forensics = item.get("forensics")
            result = forensics.get("result", {}) if forensics else {}
            spectral = (
                result.get("authenticity", {})
                .get("spectral", {})
            )

            track = Track(
                release_id=release.id,
                path=str(path),
                display_path=display_path,
                status=item["status"],
                integrity_ok=int(bool(item["integrity"]["ok"])),
                forensic_verdict=spectral.get("verdict_label"),
                human_review="NONE",
                forensic_json=(
                    json.dumps(forensics, ensure_ascii=False)
                    if forensics is not None
                    else None
                ),
            )
            release.tracks.append(track)

        recalculate_release_status(release)
        session.commit()
        session.refresh(release)
        return release
