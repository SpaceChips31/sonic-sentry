"""Persistent, preflighted file operations processed by the worker, not HTTP requests."""
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import SessionLocal
from app.models import Release, FileBatch, FileTask, FileTransfer
from app.security import sign
from app.services.files import snapshot, fingerprint, contains_flac, album_files
from app.services.settings import workflow_settings
from app.services.file_workflow import (FileOperationError, validate_source, configured_target, is_within,
    check_pending_transfer, check_active_jobs, approve_release, lock_file_operations, move_release,
    resume_transfer, utc_now)


def preflight(release_ids, mode):
    if mode not in {"approved", "approve"} or not 1 <= len(release_ids) <= 50:
        raise FileOperationError("Select between 1 and 50 albums and a valid staging action")
    settings = workflow_settings()
    rows = []
    roots = {key: Path(settings[key]).resolve() if settings[key] else None for key in ("quarantine", "staging")}
    for release_id in dict.fromkeys(release_ids):
        row = {"id":release_id,"title":str(release_id),"ok":False,"error":None,"bytes":0}
        try:
            with SessionLocal() as session:
                release = session.scalar(select(Release).where(Release.id == release_id).options(selectinload(Release.tracks)))
                if release is None:
                    raise FileOperationError("Release not found")
                row["title"] = release.title
                source = validate_source(Path(release.source_path), settings, allow_managed=True)
                if not roots["quarantine"] or not is_within(source, (roots["quarantine"],)) or not contains_flac(source):
                    raise FileOperationError("album is no longer a FLAC folder in quarantine")
                check_pending_transfer(session, release_id)
                check_active_jobs(session, source)
                if mode == "approve":
                    approve_release(release)  # Session rolls this simulation back.
                elif release.status != "PASS":
                    raise FileOperationError("only approved albums can be sent to staging")
                destination = configured_target("STAGING", settings) / source.name
                if destination.exists():
                    raise FileOperationError("destination already exists; refusing to overwrite")
                if not destination.parent.is_dir():
                    raise FileOperationError("staging directory is missing")
                with tempfile.TemporaryFile(dir=destination.parent):
                    pass
                with tempfile.TemporaryFile(dir=source):
                    pass
                if destination.parent.is_relative_to(source):
                    raise FileOperationError("destination cannot be inside the source album")
                parents=set()
                for file in album_files(source):
                    with file.open("rb") as readable:
                        readable.read(1)
                    parents.add(file.parent)
                for parent in parents:
                    with tempfile.TemporaryFile(dir=parent):
                        pass
                manifest = snapshot(source)
                row.update(ok=True, source=str(source), destination=str(destination),
                    signature=fingerprint(manifest), bytes=sum(item["size"] for item in manifest.values()))
        except (OSError, RuntimeError, ValueError) as exc:
            row["error"] = str(exc)
        rows.append(row)
    for row in rows:
        if row["ok"] and sum(other.get("destination") == row["destination"] for other in rows) > 1:
            row.update(ok=False,error="Selected albums have the same destination name")
    required = 2 * sum(row["bytes"] for row in rows if row["ok"])
    free = shutil.disk_usage(roots["staging"]).free if roots["staging"] and roots["staging"].is_dir() else 0
    payload = {"mode":mode,"rows":rows,"roots":{k:str(v) for k,v in roots.items()}}
    token = sign("preflight:" + json.dumps(payload, sort_keys=True))
    return {"rows":rows,"mode":mode,"required":required,"free":free,
        "ready":any(row["ok"] for row in rows) and free >= required,"token":token}


def enqueue_batch(release_ids, mode, token, actor):
    import hmac
    plan = preflight(release_ids, mode)
    if not hmac.compare_digest(plan["token"], token) or not plan["ready"]:
        raise FileOperationError("Preflight changed or space is insufficient; preview again")
    batch_id = uuid4().hex
    with SessionLocal() as session:
        lock_file_operations(session)
        if workflow_settings()["locked"] or not workflow_settings()["enabled"]:
            raise FileOperationError("File operations are disabled or locked")
        session.add(FileBatch(id=batch_id,actor=actor,created_at=utc_now()))
        for row in plan["rows"]:
            if not row["ok"]:
                continue
            release = session.get(Release, row["id"])
            if release is None or release.source_path != row["source"]:
                raise FileOperationError("Album paths changed; preview again")
            check_pending_transfer(session, release.id)
            check_active_jobs(session, Path(release.source_path))
            session.add(FileTask(id=uuid4().hex,batch_id=batch_id,release_id=release.id,
                source_path=row["source"],destination_path=row["destination"],source_signature=row["signature"],
                source_root=str(Path(workflow_settings()["quarantine"]).resolve()),
                manual_pass=mode == "approve",state="QUEUED",actor=actor,created_at=utc_now()))
        session.commit()
    return batch_id


def recover_tasks():
    with SessionLocal() as session:
        for task in session.scalars(select(FileTask).where(FileTask.state == "RUNNING")):
            transfer = session.get(FileTransfer, task.transfer_id or task.id)
            task.state = "NEEDS_ATTENTION" if transfer and transfer.state == "NEEDS_ATTENTION" else "QUEUED"
        session.commit()


def process_next_task():
    if workflow_settings()["locked"] or not workflow_settings()["enabled"]:
        return False
    with SessionLocal() as session:
        lock_file_operations(session)
        task = session.scalar(select(FileTask).where(FileTask.state == "QUEUED").order_by(FileTask.created_at, FileTask.id).limit(1))
        if task is None:
            return False
        task.state = "RUNNING"
        task.transfer_id = task.transfer_id or task.id
        session.commit()
    try:
        with SessionLocal() as session:
            transfer = session.get(FileTransfer, task.transfer_id)
        if transfer:
            if transfer.state == "ABANDONED":
                raise FileOperationError("Transfer was closed; preview a new operation")
            resume_transfer(transfer.id)
        else:
            move_release(task.release_id,"STAGING",manual_pass=task.manual_pass,actor=task.actor,
                expected_source_root=Path(task.source_root) if task.source_root else None,
                expected_source_path=task.source_path, expected_destination=task.destination_path, expected_signature=task.source_signature,
                current_task_id=task.id,transfer_id=task.transfer_id)
        state, error = "COMPLETE", None
    except Exception as exc:
        with SessionLocal() as session:
            transfer = session.get(FileTransfer, task.transfer_id)
        state, error = ("NEEDS_ATTENTION" if transfer else "FAILED"), str(exc)
    with SessionLocal() as session:
        saved = session.get(FileTask, task.id)
        saved.state, saved.error = state, error
        session.commit()
    return True


def enqueue_resume(transfer_id, actor):
    settings=workflow_settings()
    if settings["locked"] or not settings["enabled"]:
        raise FileOperationError("File operations are disabled or locked")
    with SessionLocal() as session:
        lock_file_operations(session)
        transfer=session.get(FileTransfer,transfer_id)
        if not transfer or transfer.state in {"COMPLETE","ABANDONED"}:
            raise FileOperationError("Transfer is not available for recovery")
        task=session.scalar(select(FileTask).where(FileTask.transfer_id==transfer_id))
        if task and task.state in {"QUEUED","RUNNING"}:
            return task.id
        if not task:
            task=FileTask(id=uuid4().hex,release_id=transfer.release_id,source_path=transfer.source_path,
                destination_path=transfer.destination_path,transfer_id=transfer.id,manual_pass=transfer.manual_pass,
                actor=actor,created_at=utc_now())
            session.add(task)
        task.state,task.error="QUEUED",None
        session.commit()
        return task.id
