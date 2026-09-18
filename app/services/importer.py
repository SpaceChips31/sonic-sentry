import json
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Release, Track


def import_report(report_path: Path) -> Release:
    report_path = report_path.resolve()
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported or missing report schema_version")

    source_path = payload["release"]
    source = Path(source_path)
    summary = payload["summary"]

    with SessionLocal() as session:
        existing = session.scalar(
            select(Release).where(Release.source_path == source_path)
        )

        if existing is not None:
            session.delete(existing)
            session.flush()

        release = Release(
            source_path=source_path,
            title=source.name,
            status=payload["status"],
            analysed_at=payload.get("analysed_at"),
            total_tracks=summary["total"],
            pass_tracks=summary["pass"],
            quarantine_tracks=summary["quarantine"],
            rejected_tracks=summary["rejected"],
        )

        session.add(release)
        session.flush()

        for item in payload["files"]:
            path = Path(item["path"])

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
            session.add(track)

        session.commit()
        session.refresh(release)
        return release
