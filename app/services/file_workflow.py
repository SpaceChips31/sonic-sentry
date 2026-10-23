from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import AnalysisJob, Release, ReleaseOperation
from app.services.settings import workflow_settings
from app.services.verdicts import recalculate_release_status


class FileOperationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if relative != Path("."):
            return True
    return False


def configured_target(kind: str, settings: dict) -> Path:
    target = settings.get(kind.lower())
    if kind not in {"STAGING", "QUARANTINE", "REJECTED"} or not target:
        raise FileOperationError(f"{kind.lower()} destination is not configured")
    return Path(target).resolve()


def validate_source(path: Path, settings: dict) -> Path:
    source = path.resolve()
    if not settings["enabled"]:
        raise FileOperationError("file operations are disabled")
    if not source.is_dir():
        raise FileOperationError(f"source directory does not exist: {source}")
    roots = tuple(Path(p).resolve() for p in settings["movable_roots"])
    protected = roots + tuple(Path(settings[k]).resolve() for k in ("staging", "quarantine", "rejected") if settings[k])
    if any(root.is_relative_to(source) for root in protected):
        raise FileOperationError("cannot move or delete a configured root or its parent")
    if not is_within(source, roots):
        raise FileOperationError(
            "source is outside LOSSLESS_MOVABLE_ROOTS"
        )
    return source


def destination_for(source: Path, kind: str, settings: dict) -> Path:
    root = configured_target(kind, settings)
    if root.is_relative_to(source):
        raise FileOperationError("destination cannot be inside the source album")
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / source.name).resolve()

    if destination == source or source.is_relative_to(destination):
        raise FileOperationError("release is already in that destination")
    if destination.exists():
        raise FileOperationError(
            f"destination already exists; refusing to overwrite: {destination}"
        )
    return destination


def update_paths(
    release: Release,
    source: Path,
    destination: Path,
) -> None:
    release.source_path = str(destination)
    for track in release.tracks:
        old_path = Path(track.path)
        try:
            relative = old_path.relative_to(source)
        except ValueError as exc:
            raise FileOperationError("track path is outside the album directory") from exc
        track.path = str(destination / relative)


def lock_file_operations(session) -> None:
    # Web and worker are separate processes. Serialize file changes in SQLite.
    if session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def check_active_jobs(session, source: Path, current_job_id: int | None = None) -> None:
    statement = select(AnalysisJob.source_path).where(AnalysisJob.status.in_({"QUEUED", "ANALYZING"}))
    if current_job_id is not None:
        statement = statement.where(AnalysisJob.id != current_job_id)
    queued = session.scalars(statement)
    if any(Path(p).resolve().is_relative_to(source) or source.is_relative_to(Path(p).resolve()) for p in queued):
        raise FileOperationError("album has an active analysis; wait for it to finish")


def move_release(
    release_id: int,
    target_kind: str,
    *,
    manual_pass: bool = False,
    current_job_id: int | None = None,
) -> Path:
    normalized = target_kind.upper()
    settings = workflow_settings()
    with SessionLocal() as session:
        lock_file_operations(session)
        release = session.scalar(
            select(Release)
            .where(Release.id == release_id)
            .options(selectinload(Release.tracks))
        )
        if release is None:
            raise FileOperationError("release not found")

        source = validate_source(Path(release.source_path), settings)
        check_active_jobs(session, source, current_job_id)
        destination = destination_for(source, normalized, settings)

        if manual_pass:
            if normalized != "STAGING" or not release.tracks or any(
                not track.integrity_ok or track.status == "REJECTED" for track in release.tracks
            ):
                raise FileOperationError("an album with integrity failures cannot be approved")
            for track in release.tracks:
                if track.status == "QUARANTINE":
                    track.human_review = "APPROVED"
            recalculate_release_status(release)

        if normalized == "STAGING" and release.status != "PASS":
            raise FileOperationError("only approved albums can be sent to staging")

        update_paths(release, source, destination)
        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            raise FileOperationError(f"could not move album: {exc}") from exc
        session.add(
            ReleaseOperation(
                release_id=release.id,
                action=(
                    "MANUAL_PASS_TO_STAGING"
                    if manual_pass
                    else f"MOVE_TO_{normalized}"
                ),
                source_path=str(source),
                destination_path=str(destination),
                created_at=utc_now(),
            )
        )
        try:
            session.commit()
        except Exception:
            session.rollback()
            shutil.move(str(destination), str(source))
            raise
        return destination


def delete_release_files(release_id: int) -> None:
    settings = workflow_settings()
    with SessionLocal() as session:
        lock_file_operations(session)
        release = session.scalar(
            select(Release).where(Release.id == release_id)
        )
        if release is None:
            raise FileOperationError("release not found")

        source = validate_source(Path(release.source_path), settings)
        check_active_jobs(session, source)
        try:
            shutil.rmtree(source)
        except OSError as exc:
            raise FileOperationError(f"could not delete album: {exc}") from exc
        session.add(
            ReleaseOperation(
                release_id=release.id,
                action="DELETE",
                source_path=str(source),
                destination_path=None,
                created_at=utc_now(),
            )
        )
        session.commit()


def auto_route_release(release_id: int, *, current_job_id: int | None = None) -> Path | None:
    settings = workflow_settings()
    if not settings["enabled"] or not settings["auto_route"]:
        return None

    with SessionLocal() as session:
        release = session.get(Release, release_id)
        if release is None:
            raise FileOperationError("release not found")
        if release.status != "PASS" and not settings["route_nonpass"]:
            return None
        target = {
            "PASS": "STAGING",
            "QUARANTINE": "QUARANTINE",
            "REJECTED": "REJECTED",
        }.get(release.status)

    if target is None:
        raise FileOperationError("release has no routable verdict")
    return move_release(release_id, target, current_job_id=current_job_id)


def settings_snapshot() -> dict:
    settings = workflow_settings()
    def describe(path: Path | None) -> dict:
        if not path:
            return {
                "path": "Not configured",
                "exists": False,
                "writable": False,
            }
        path = Path(path)
        return {
            "path": str(path),
            "exists": path.is_dir(),
            "writable": path.is_dir() and os_access_writable(path),
        }

    return {
        "enabled": settings["enabled"],
        "auto_route": settings["auto_route"],
        "movable_roots": [describe(path) for path in settings["movable_roots"]],
        "staging": describe(settings["staging"]),
        "quarantine": describe(settings["quarantine"]),
        "rejected": describe(settings["rejected"]),
    }


def os_access_writable(path: Path) -> bool:
    import tempfile
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
        return True
    except OSError:
        return False
