from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import Release, ReleaseOperation, Track
from app.services.runtime_settings import (
    bool_value,
    describe as describe_setting,
    path_value,
    paths_value,
)


TARGET_KEYS = {
    "STAGING": "staging_root",
    "QUARANTINE": "quarantine_root",
    "REJECTED": "rejected_root",
}


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


def configured_target(kind: str) -> Path:
    key = TARGET_KEYS.get(kind)
    target = path_value(key) if key else None
    if target is None:
        raise FileOperationError(f"{kind.lower()} destination is not configured")
    return target


def validate_source(path: Path) -> Path:
    source = path.resolve()
    if not bool_value("file_operations"):
        raise FileOperationError("file operations are disabled")
    if not source.is_dir():
        raise FileOperationError(f"source directory does not exist: {source}")
    if not is_within(source, paths_value("movable_roots")):
        raise FileOperationError(
            "source is outside LOSSLESS_MOVABLE_ROOTS"
        )
    return source


def destination_for(source: Path, kind: str) -> Path:
    root = configured_target(kind)
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / source.name).resolve()

    if destination == source:
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
        except ValueError:
            relative = Path(track.display_path)
        track.path = str(destination / relative)


def move_release(
    release_id: int,
    target_kind: str,
    *,
    manual_pass: bool = False,
) -> Path:
    normalized = target_kind.upper()
    with SessionLocal() as session:
        release = session.scalar(
            select(Release)
            .where(Release.id == release_id)
            .options(selectinload(Release.tracks))
        )
        if release is None:
            raise FileOperationError("release not found")

        source = validate_source(Path(release.source_path))
        destination = destination_for(source, normalized)

        if manual_pass:
            for track in release.tracks:
                if track.status == "QUARANTINE":
                    track.human_review = "APPROVED"
            release.status = "PASS"

        shutil.move(str(source), str(destination))
        update_paths(release, source, destination)
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
        session.commit()
        return destination


def delete_release_files(release_id: int) -> None:
    with SessionLocal() as session:
        release = session.scalar(
            select(Release).where(Release.id == release_id)
        )
        if release is None:
            raise FileOperationError("release not found")

        source = validate_source(Path(release.source_path))
        shutil.rmtree(source)
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


def undo_move(release_id: int, operation_id: int) -> Path:
    with SessionLocal() as session:
        release = session.scalar(
            select(Release)
            .where(Release.id == release_id)
            .options(selectinload(Release.tracks), selectinload(Release.operations))
        )
        if release is None:
            raise FileOperationError("release not found")
        operation = next((item for item in release.operations if item.id == operation_id), None)
        if operation is None or not operation.action.startswith(("MOVE_TO_", "MANUAL_PASS_TO_")):
            raise FileOperationError("operation cannot be undone")
        if release.operations and release.operations[0].id != operation.id:
            raise FileOperationError("only the latest file operation can be undone")
        current = validate_source(Path(release.source_path))
        if operation.destination_path is None or current != Path(operation.destination_path).resolve():
            raise FileOperationError("release is no longer at the recorded destination")
        original = Path(operation.source_path).resolve()
        if not is_within(original, paths_value("movable_roots")):
            raise FileOperationError("original location is outside the authorized folders")
        if original.exists():
            raise FileOperationError("original location is no longer available")
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(current), str(original))
        update_paths(release, current, original)
        session.add(
            ReleaseOperation(
                release_id=release.id,
                action=f"UNDO_{operation.action}",
                source_path=str(current),
                destination_path=str(original),
                created_at=utc_now(),
            )
        )
        session.commit()
        return original


def auto_route_release(release_id: int) -> Path | None:
    if not bool_value("file_operations") or not bool_value("auto_route"):
        return None

    with SessionLocal() as session:
        release = session.get(Release, release_id)
        if release is None:
            raise FileOperationError("release not found")
        target = {
            "PASS": "STAGING",
            "QUARANTINE": "QUARANTINE",
            "REJECTED": "REJECTED",
        }.get(release.status)

    if target is None:
        raise FileOperationError("release has no routable verdict")
    return move_release(release_id, target)


def settings_snapshot() -> dict:
    def describe_path(path: Path | None) -> dict:
        if path is None:
            return {
                "path": "Not configured",
                "exists": False,
                "writable": False,
            }
        return {
            "path": str(path),
            "exists": path.is_dir(),
            "writable": path.is_dir() and os_access_writable(path),
        }

    return {
        "enabled": bool_value("file_operations"),
        "auto_route": bool_value("auto_route"),
        "movable_roots": [describe_path(path) for path in paths_value("movable_roots")],
        "staging": describe_path(path_value("staging_root")),
        "quarantine": describe_path(path_value("quarantine_root")),
        "rejected": describe_path(path_value("rejected_root")),
        "fields": {key: describe_setting(key) for key in (
            "file_operations", "auto_route", "movable_roots",
            "staging_root", "quarantine_root", "rejected_root", "auth_enabled",
            "worker_concurrency",
        )},
    }


def os_access_writable(path: Path) -> bool:
    try:
        probe = path / ".lossless-validator-write-test"
        probe.touch(exist_ok=False)
        probe.unlink()
        return True
    except OSError:
        return False
