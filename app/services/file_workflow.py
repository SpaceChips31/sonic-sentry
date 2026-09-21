from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import (
    AUTO_ROUTE_RELEASES,
    FILE_OPERATIONS_ENABLED,
    MOVABLE_ROOTS,
    QUARANTINE_ROOT,
    REJECTED_ROOT,
    STAGING_ROOT,
)
from app.database import SessionLocal
from app.models import Release, ReleaseOperation, Track


TARGET_ROOTS = {
    "STAGING": STAGING_ROOT,
    "QUARANTINE": QUARANTINE_ROOT,
    "REJECTED": REJECTED_ROOT,
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
    target = TARGET_ROOTS.get(kind)
    if target is None:
        raise FileOperationError(f"{kind.lower()} destination is not configured")
    return target


def validate_source(path: Path) -> Path:
    source = path.resolve()
    if not FILE_OPERATIONS_ENABLED:
        raise FileOperationError("file operations are disabled")
    if not source.is_dir():
        raise FileOperationError(f"source directory does not exist: {source}")
    if not is_within(source, MOVABLE_ROOTS):
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


def auto_route_release(release_id: int) -> Path | None:
    if not FILE_OPERATIONS_ENABLED or not AUTO_ROUTE_RELEASES:
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
    def describe(path: Path | None) -> dict:
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
        "enabled": FILE_OPERATIONS_ENABLED,
        "auto_route": AUTO_ROUTE_RELEASES,
        "movable_roots": [describe(path) for path in MOVABLE_ROOTS],
        "staging": describe(STAGING_ROOT),
        "quarantine": describe(QUARANTINE_ROOT),
        "rejected": describe(REJECTED_ROOT),
    }


def os_access_writable(path: Path) -> bool:
    try:
        probe = path / ".lossless-validator-write-test"
        probe.touch(exist_ok=False)
        probe.unlink()
        return True
    except OSError:
        return False
