import json
import os
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import TrashEntry, Release, ReleaseOperation
from app.services.file_workflow import (FileOperationError, move_release, utc_now, lock_file_operations,
                                        check_active_jobs, check_pending_transfer, is_within)
from app.services.files import snapshot, album_files, file_digest
from app.services.locks import process_lock
from app.services.settings import workflow_settings


def restore_trash(entry_id: str, *, actor: str = "local") -> Path:
    with SessionLocal() as session:
        entry = session.get(TrashEntry, entry_id)
        if entry is None:
            raise FileOperationError("trash entry not found")
        release_id = entry.release_id
    return move_release(release_id, "RESTORE", trash_entry_id=entry_id, actor=actor)


def purge_trash(entry_id: str, *, resume: bool = False, actor: str = "local") -> None:
    settings = workflow_settings()
    if not settings["enabled"] or settings["locked"] or not settings["trash"]:
        raise FileOperationError("file operations are disabled or locked")
    with process_lock("file-workflow"):
        with SessionLocal() as session:
            lock_file_operations(session)
            entry = session.get(TrashEntry, entry_id)
            allowed = {"PURGING"} if resume else {"TRASHED", "PURGE_FAILED"}
            if entry is None or entry.state not in allowed:
                raise FileOperationError("trash entry is not ready for permanent deletion")
            source = Path(entry.trash_path).resolve()
            if not is_within(source, (Path(settings["trash"]).resolve(),)):
                raise FileOperationError("trash path is outside the configured trash root")
            check_active_jobs(session, source)
            check_pending_transfer(session, entry.release_id)
            expected = json.loads(entry.manifest_json)
            entry.state, entry.error = "PURGING", None
            if not resume:
                entry.purge_actor = actor
            session.commit()
        try:
            if source.exists():
                remaining = snapshot(source, hashes=True)
                if any(name not in expected or item["sha256"] != expected[name]["sha256"] for name, item in remaining.items()):
                    raise FileOperationError("trash contains changed or added files; nothing further was deleted")
                for path in album_files(source):
                    if file_digest(path) != expected[path.relative_to(source).as_posix()]["sha256"]:
                        raise FileOperationError("trash contents changed during deletion")
                    path.unlink()
                for folder, _, _ in os.walk(source, topdown=False):
                    Path(folder).rmdir()
            with SessionLocal() as session:
                entry = session.get(TrashEntry, entry_id)
                entry.state, entry.finished_at = "PURGED", utc_now()
                session.add(ReleaseOperation(release_id=entry.release_id, action="PURGE_TRASH",
                    source_path=str(source), destination_path=None, created_at=utc_now(), actor=entry.purge_actor or "local"))
                session.commit()
        except Exception as exc:
            with SessionLocal() as session:
                entry = session.get(TrashEntry, entry_id)
                entry.state, entry.error = "PURGE_FAILED", str(exc)
                session.commit()
            raise FileOperationError(str(exc)) from exc


def recover_purges() -> int:
    with SessionLocal() as session:
        ids = list(session.scalars(select(TrashEntry.id).where(TrashEntry.state == "PURGING")))
    count = 0
    for entry_id in ids:
        try:
            purge_trash(entry_id, resume=True)
            count += 1
        except (FileOperationError, RuntimeError):
            continue
    return count
