from __future__ import annotations

import shutil
import json
import os
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import AnalysisJob, Release, ReleaseOperation, FileTransfer, TrashEntry, ReviewEvent
from app.services.files import MARKER, snapshot, same_content, file_digest, album_files
from app.services.locks import process_lock
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
    if kind not in {"STAGING", "QUARANTINE", "REJECTED", "TRASH"} or not target:
        raise FileOperationError(f"{kind.lower()} destination is not configured")
    return Path(target).resolve()


def validate_source(path: Path, settings: dict, *, allow_managed: bool = False) -> Path:
    source = path.resolve()
    if not settings["enabled"]:
        raise FileOperationError("file operations are disabled")
    if not source.is_dir():
        raise FileOperationError(f"source directory does not exist: {source}")
    roots = tuple(Path(p).resolve() for p in settings["movable_roots"])
    managed = tuple(Path(settings[k]).resolve() for k in ("staging", "quarantine", "rejected", "trash") if settings.get(k))
    protected = roots + managed
    if allow_managed:
        roots += managed
    if any(root.is_relative_to(source) for root in protected):
        raise FileOperationError("cannot move or delete a configured root or its parent")
    if not is_within(source, roots):
        raise FileOperationError(
            "source is outside LOSSLESS_MOVABLE_ROOTS"
        )
    return source


def destination_for(source: Path, kind: str, settings: dict, *, basename: str | None = None) -> Path:
    root = configured_target(kind, settings)
    if root.is_relative_to(source):
        raise FileOperationError("destination cannot be inside the source album")
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / (basename or source.name)).resolve()

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
    statement = select(AnalysisJob.source_path).where(AnalysisJob.status.in_({"QUEUED", "ANALYZING", "ROUTING"}))
    if current_job_id is not None:
        statement = statement.where(AnalysisJob.id != current_job_id)
    queued = session.scalars(statement)
    if any(Path(p).resolve().is_relative_to(source) or source.is_relative_to(Path(p).resolve()) for p in queued):
        raise FileOperationError("album has an active analysis; wait for it to finish")


def check_pending_transfer(session, release_id: int) -> None:
    release = session.get(Release, release_id)
    path = Path(release.source_path) if release else None
    for transfer in session.scalars(select(FileTransfer).where(FileTransfer.state.not_in({"COMPLETE", "ABANDONED"}))):
        if transfer.release_id == release_id or (path and any(
            path.is_relative_to(other) or other.is_relative_to(path)
            for other in (Path(transfer.source_path), Path(transfer.destination_path))
        )):
            raise FileOperationError("album has an unfinished transfer; inspect Activity")


def approve_release(release: Release) -> None:
    if not release.tracks or any(not track.integrity_ok or track.status == "REJECTED" for track in release.tracks):
        raise FileOperationError("an album with integrity failures cannot be approved")
    for track in release.tracks:
        if track.status == "QUARANTINE":
            track.human_review = "APPROVED"
    recalculate_release_status(release)
    if release.status != "PASS":
        raise FileOperationError("only approved albums can be sent to staging")


def rename_exclusive(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)  # Windows refuses existing destinations.
        return
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        raise FileOperationError("Atomic exclusive directory rename is unavailable")
    result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def transfer_state(transfer_id: str, state: str, error: str | None = None) -> None:
    with SessionLocal() as session:
        transfer = session.get(FileTransfer, transfer_id)
        transfer.state, transfer.error = state, error
        if state == "COMPLETE" and transfer.trash_entry_id:
            entry = session.get(TrashEntry, transfer.trash_entry_id)
            entry.state = "TRASHED" if transfer.target_kind == "TRASH" else "RESTORED"
            entry.finished_at = utc_now()
        session.commit()


def owned_directory(path: Path, transfer_id: str) -> bool:
    return (path.is_dir() and not path.is_symlink()
            and not (hasattr(path, "is_junction") and path.is_junction())
            and (path / MARKER).is_file()
            and (path / MARKER).read_text(encoding="utf-8") == transfer_id)


def run_transfer(transfer_id: str) -> Path:
    """Resume a journaled copy, publication, database update, and source cleanup."""
    if workflow_settings()["locked"]:
        raise FileOperationError("File operations are locked by the deployment")
    with SessionLocal() as session:
        transfer = session.get(FileTransfer, transfer_id)
        if transfer is None:
            raise FileOperationError("transfer not found")
        source = Path(transfer.source_path)
        destination = Path(transfer.destination_path)
        temporary = Path(transfer.temporary_path)
        expected = json.loads(transfer.manifest_json)
        if transfer.state == "COMPLETE":
            return destination
        if transfer.state == "ABANDONED":
            raise FileOperationError("this transfer was closed; start a new album action instead")
    try:
        if not destination.exists():
            if not source.is_dir() or not same_content(snapshot(source, hashes=True), expected):
                raise FileOperationError("source changed since the transfer started")
            if not temporary.exists():
                temporary.mkdir()
                (temporary / MARKER).write_text(transfer_id, encoding="utf-8")
            if not owned_directory(temporary, transfer_id):
                raise FileOperationError("temporary directory ownership cannot be verified")
            transfer_state(transfer_id, "COPYING")
            shutil.copytree(source, temporary, dirs_exist_ok=True, symlinks=True)
            if not same_content(snapshot(temporary, hashes=True), expected):
                raise FileOperationError("copied files failed verification")
            if not same_content(snapshot(source, hashes=True), expected):
                raise FileOperationError("source changed during the transfer")
            rename_exclusive(temporary, destination)
        if not owned_directory(destination, transfer_id) or not same_content(snapshot(destination, hashes=True), expected):
            raise FileOperationError("destination ownership or contents cannot be verified")

        # DB path changes and audit entry are committed together, before removing originals.
        with SessionLocal() as session:
            lock_file_operations(session)
            current = session.get(FileTransfer, transfer_id)
            release = session.scalar(select(Release).where(Release.id == current.release_id).options(selectinload(Release.tracks)))
            if release is None:
                raise FileOperationError("release no longer exists")
            if release.source_path == str(source):
                if current.manual_pass:
                    previous = {track.id: track.human_review for track in release.tracks}
                    approve_release(release)
                    for track in release.tracks:
                        if previous[track.id] != track.human_review:
                            session.add(ReviewEvent(release_id=release.id, track_path=track.display_path,
                                previous=previous[track.id], decision=track.human_review, note="Approved during manual staging",
                                actor=current.actor, created_at=utc_now()))
                update_paths(release, source, destination)
                session.add(ReleaseOperation(release_id=release.id,
                    action="MANUAL_PASS_TO_STAGING" if current.manual_pass else "RESTORE_FROM_TRASH" if current.target_kind == "RESTORE" else "MOVE_TO_TRASH" if current.target_kind == "TRASH" else f"MOVE_TO_{current.target_kind}",
                    source_path=str(source), destination_path=str(destination), created_at=utc_now(), actor=current.actor))
            elif release.source_path != str(destination):
                raise FileOperationError("release path changed independently of this transfer")
            current.state, current.error = "CLEANUP", None
            session.commit()

        # On restart the source may be partially cleaned. Never remove changed or added files.
        if source.exists():
            remaining = snapshot(source, hashes=True)
            if any(name not in expected or item["sha256"] != expected[name]["sha256"] for name, item in remaining.items()):
                raise FileOperationError("source contains new or changed files; originals were preserved")
            for path in album_files(source):
                name = path.relative_to(source).as_posix()
                if file_digest(path) != expected[name]["sha256"]:
                    raise FileOperationError("source changed during cleanup; remaining files were preserved")
                path.unlink()
            for directory, _, _ in os.walk(source, topdown=False):
                Path(directory).rmdir()
        transfer_state(transfer_id, "COMPLETE")
        (destination / MARKER).unlink(missing_ok=True)
        return destination
    except Exception as exc:
        transfer_state(transfer_id, "NEEDS_ATTENTION", str(exc))
        if isinstance(exc, FileOperationError):
            raise
        raise FileOperationError(f"could not move album: {exc}") from exc


def move_release(release_id: int, target_kind: str, *, manual_pass: bool = False,
                 current_job_id: int | None = None, trash_entry_id: str | None = None, actor: str = "local") -> Path:
    normalized = target_kind.upper()
    settings = workflow_settings()
    try:
        with process_lock("file-workflow"):
            with SessionLocal() as session:
                release = session.get(Release, release_id)
                if release is None:
                    raise FileOperationError("release not found")
                check_pending_transfer(session, release_id)
                source = validate_source(Path(release.source_path), settings, allow_managed=True)
                check_active_jobs(session, source, current_job_id)
                active_trash = session.scalar(select(TrashEntry).where(TrashEntry.release_id == release_id,
                    TrashEntry.state.in_({"PENDING", "TRASHED", "PURGING", "PURGE_FAILED"})))
                if active_trash and normalized != "RESTORE":
                    raise FileOperationError("restore this album from the trash before moving it")
                if normalized == "RESTORE":
                    entry = session.get(TrashEntry, trash_entry_id)
                    if entry is None or entry.release_id != release_id or entry.state != "TRASHED" or source != Path(entry.trash_path).resolve():
                        raise FileOperationError("trash entry is not ready to restore")
                    destination = Path(entry.original_path).resolve()
                    roots = tuple(Path(p).resolve() for p in settings["movable_roots"])
                    roots += tuple(Path(settings[k]).resolve() for k in ("staging", "quarantine", "rejected") if settings[k])
                    if not is_within(destination, roots) or not destination.parent.is_dir() or destination.exists():
                        raise FileOperationError("original destination is unavailable, unauthorized or already occupied")
            transfer_id = uuid4().hex
            if normalized != "RESTORE":
                destination = destination_for(source, normalized, settings,
                    basename=f"{source.name[:100]}--{transfer_id[:12]}" if normalized == "TRASH" else None)
                if normalized == "TRASH":
                    trash_entry_id = transfer_id
            # Slow disk reads must not hold SQLite's writer lock.
            manifest = snapshot(source, hashes=True)
            if not manifest:
                raise FileOperationError("album is empty")
            if shutil.disk_usage(destination.parent).free < sum(item["size"] for item in manifest.values()):
                raise FileOperationError("not enough free space for a verified copy")
            with SessionLocal() as session:
                lock_file_operations(session)
                release = session.scalar(select(Release).where(Release.id == release_id).options(selectinload(Release.tracks)))
                if release is None or Path(release.source_path).resolve() != source:
                    raise FileOperationError("release changed while preparing its transfer")
                check_pending_transfer(session, release_id)
                validate_source(source, settings, allow_managed=True)
                check_active_jobs(session, source, current_job_id)
                previous_status = release.status
                previous_reviews = [track.human_review for track in release.tracks]
                if manual_pass:
                    if normalized != "STAGING":
                        raise FileOperationError("manual approval requires staging")
                    approve_release(release)
                if normalized == "STAGING" and release.status != "PASS":
                    raise FileOperationError("only approved albums can be sent to staging")
                release.status = previous_status
                for track, review in zip(release.tracks, previous_reviews):
                    track.human_review = review
                if normalized == "RESTORE":
                    entry = session.get(TrashEntry, trash_entry_id)
                    if entry is None or entry.state != "TRASHED":
                        raise FileOperationError("trash entry changed during preparation")
                if normalized == "TRASH":
                    session.add(TrashEntry(id=trash_entry_id, release_id=release_id, original_path=str(source),
                        trash_path=str(destination), manifest_json=json.dumps(manifest), state="PENDING", created_at=utc_now(), actor=actor))
                session.add(FileTransfer(id=transfer_id, release_id=release_id, job_id=current_job_id, trash_entry_id=trash_entry_id, source_path=str(source),
                    destination_path=str(destination), temporary_path=str(destination.parent / f".sonic-sentry-{transfer_id}.partial"),
                    state="PREPARED", manual_pass=manual_pass, target_kind=normalized,
                    manifest_json=json.dumps(manifest), created_at=utc_now(), actor=actor))
                # Approval is applied only when publication has been verified.
                session.commit()
            return run_transfer(transfer_id)
    except (RuntimeError, OSError, ValueError) as exc:
        if isinstance(exc, FileOperationError):
            raise
        raise FileOperationError(str(exc)) from exc


def resume_transfer(transfer_id: str) -> Path:
    try:
        with process_lock("file-workflow"):
            return run_transfer(transfer_id)
    except RuntimeError as exc:
        raise FileOperationError(str(exc)) from exc


def close_transfer(transfer_id: str) -> None:
    """Explicit operator resolution: retain every file and release the album lock."""
    try:
        with process_lock("file-workflow"), SessionLocal() as session:
            lock_file_operations(session)
            transfer = session.get(FileTransfer, transfer_id)
            if transfer is None or transfer.state != "NEEDS_ATTENTION":
                raise FileOperationError("only a stopped transfer can be closed")
            release = session.scalar(select(Release).where(Release.id == transfer.release_id).options(selectinload(Release.tracks)))
            if release is None or not Path(release.source_path).is_dir() or any(not Path(t.path).is_file() for t in release.tracks):
                raise FileOperationError("restore the files referenced by this release before closing its transfer")
            transfer.state = "ABANDONED"
            if transfer.trash_entry_id:
                entry = session.get(TrashEntry, transfer.trash_entry_id)
                if transfer.target_kind == "TRASH":
                    entry.state = "TRASHED" if release.source_path == transfer.destination_path else "CANCELLED"
                else:
                    entry.state = "RESTORED" if release.source_path == transfer.destination_path else "TRASHED"
            transfer.error = "Closed by operator; all remaining source, destination and temporary files were preserved. " + (transfer.error or "")
            session.commit()
    except RuntimeError as exc:
        raise FileOperationError(str(exc)) from exc


def recover_transfers() -> int:
    with SessionLocal() as session:
        ids = list(session.scalars(select(FileTransfer.id).where(FileTransfer.state.in_({"PREPARED", "COPYING", "CLEANUP"}))))
    recovered = 0
    for transfer_id in ids:
        try:
            resume_transfer(transfer_id)
            recovered += 1
        except FileOperationError:
            continue
    return recovered


def delete_release_files(release_id: int, *, actor: str = "local") -> Path:
    """Compatibility name: deletion now moves to the recoverable trash."""
    return move_release(release_id, "TRASH", actor=actor)


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
    expected = settings.get(target.lower())
    if expected and Path(release.source_path).resolve().is_relative_to(Path(expected).resolve()) and Path(release.source_path).resolve() != Path(expected).resolve():
        return Path(release.source_path)
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
        "trash": describe(settings["trash"]),
        "locked": settings["locked"],
        "configuration_origin": settings["configuration_origin"],
    }


def os_access_writable(path: Path) -> bool:
    import tempfile
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
        return True
    except OSError:
        return False
