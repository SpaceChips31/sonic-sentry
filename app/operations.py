from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.templating import Jinja2Templates

from app.presentation import configure_templates
from app.services.inventory import KINDS, destination_inventory
from app.services.settings import workflow_settings, save_operational_settings, WorkflowConfigurationError
from app.services.file_workflow import FileOperationError, is_within, move_release
from app.database import SessionLocal
from app.models import Release, FileTask, FileBatch, FileTransfer, ReconcileScan, Track, AnalysisSource
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.services.file_tasks import preflight, enqueue_batch
from app.services.reconciliation import allowed_roots, enqueue_scan, apply_proposal
import json
from app.services.batch import discover_releases, BatchDiscoveryError
from app.services.queue import enqueue_paths
from app.services.trash import restore_trash, purge_trash
from app.services.retention import cleanup_preview, execute_cleanup


router = APIRouter()
templates = configure_templates(Jinja2Templates(directory="app/templates"))


@router.get("/maintenance/backup")
def download_backup():
    import tempfile
    import sqlite3
    from app.maintenance import export_backup
    from datetime import datetime, timezone
    folder = Path(tempfile.mkdtemp(prefix="sonic-export-"))
    path = folder / "backup.zip"
    try:
        export_backup(path)
    except (OSError, ValueError, sqlite3.Error) as exc:
        path.unlink(missing_ok=True)
        folder.rmdir()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    def cleanup():
        path.unlink(missing_ok=True)
        folder.rmdir()
    filename = f"sonic-sentry-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    return FileResponse(path, filename=filename, media_type="application/zip", background=BackgroundTask(cleanup))


@router.get("/destinations/{kind}", response_class=HTMLResponse)
def destinations(request: Request, kind: str, q: str = "", page: int = 1, show_all: bool = False):
    try:
        data = destination_inventory(kind, q, show_all=show_all)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    total = len(data["rows"])
    pages = max(1, (total + 49) // 50)
    page = min(max(1, page), pages)
    data.update(rows=data["rows"][(page-1)*50:page*50], page=page, pages=pages, total=total,
                kinds=KINDS, settings=workflow_settings())
    return templates.TemplateResponse(request=request, name="destinations.html", context=data)


@router.post("/destinations/quarantine/staging", response_class=HTMLResponse)
def bulk_staging(request: Request, release_ids: list[int] = Form(...), mode: str = Form(...)):
    try:
        plan=preflight(release_ids,mode)
    except (OSError,RuntimeError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    return templates.TemplateResponse(request=request,name="file_preflight.html",context={"plan":plan,"ids":release_ids})


@router.post("/file-batches")
def create_file_batch(request: Request, release_ids: list[int] = Form(...), mode: str = Form(...), token: str = Form(...)):
    try:
        identifier=enqueue_batch(release_ids,mode,token,request.state.actor)
    except (OSError,RuntimeError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    return RedirectResponse("/file-batches/"+identifier,status_code=303)


@router.get("/file-batches/{batch_id}",response_class=HTMLResponse)
def file_batch(request: Request,batch_id: str):
    with SessionLocal() as session:
        batch=session.get(FileBatch,batch_id)
        if not batch:
            raise HTTPException(status_code=404,detail="Batch not found")
        tasks=list(session.scalars(select(FileTask).where(FileTask.batch_id==batch_id).order_by(FileTask.created_at,FileTask.id)))
        transfers={t.id:t for t in session.scalars(select(FileTransfer).where(FileTransfer.id.in_([t.transfer_id for t in tasks if t.transfer_id])))}
    return templates.TemplateResponse(request=request,name="file_batch.html",context={"batch":batch,"tasks":tasks,"transfers":transfers,
        "active":any(t.state in {"QUEUED","RUNNING"} for t in tasks),"settings":workflow_settings()})


@router.post("/file-tasks/{task_id}/cancel")
def cancel_file_task(task_id: str):
    from app.services.file_workflow import lock_file_operations
    with SessionLocal() as session:
        lock_file_operations(session)
        task=session.get(FileTask,task_id)
        if not task or task.state != "QUEUED":
            raise HTTPException(status_code=409,detail="Only queued operations can be cancelled")
        task.state="CANCELLED"
        session.commit()
    return RedirectResponse("/file-batches/"+task.batch_id if task.batch_id else "/activity",status_code=303)


@router.get("/reconciliation",response_class=HTMLResponse)
def reconciliation(request: Request):
    with SessionLocal() as session:
        scans=list(session.scalars(select(ReconcileScan).order_by(ReconcileScan.created_at.desc()).limit(50)))
    return templates.TemplateResponse(request=request,name="reconciliation.html",context={"roots":allowed_roots(),"scans":scans})


@router.post("/reconciliation")
def start_reconciliation(request: Request,root: str=Form(...)):
    try:
        identifier=enqueue_scan(root,request.state.actor)
    except (OSError,RuntimeError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    return RedirectResponse("/reconciliation/"+identifier,status_code=303)


@router.get("/reconciliation/{scan_id}",response_class=HTMLResponse)
def reconciliation_results(request: Request,scan_id: str):
    with SessionLocal() as session:
        scan=session.get(ReconcileScan,scan_id)
    if not scan:
        raise HTTPException(status_code=404,detail="Scan not found")
    return templates.TemplateResponse(request=request,name="reconciliation_results.html",context={"scan":scan,"rows":json.loads(scan.results_json)})


@router.post("/reconciliation/{scan_id}/apply")
def reconcile_paths(request: Request,scan_id: str,release_id: int=Form(...)):
    try:
        apply_proposal(scan_id,release_id,request.state.actor)
    except (OSError,RuntimeError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    return RedirectResponse(f"/releases/{release_id}",status_code=303)


@router.post("/settings/sources/{source_id}/policy")
def source_policy(source_id: int,scan_interval: str=Form(""),settle_seconds: str=Form(""),routing_policy: str=Form("inherit")):
    from app.services.sources import save_source_policy
    try:
        save_source_policy(source_id,int(scan_interval) if scan_interval.strip() else None,int(settle_seconds) if settle_seconds.strip() else None,routing_policy)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    return RedirectResponse("/settings#ingestion",status_code=303)


@router.get("/releases/{release_id}/compare",response_class=HTMLResponse)
def compare_tracks(request: Request,release_id: int,left: int|None=None,right: int|None=None):
    from app.main import managed_spectrogram
    with SessionLocal() as session:
        release=session.scalar(select(Release).where(Release.id==release_id).options(selectinload(Release.tracks)))
    if not release:
        raise HTTPException(status_code=404,detail="Release not found")
    chosen=[]
    ids={track.id:track for track in release.tracks}
    for identifier in (left if left is not None else next(iter(ids),None),right if right is not None else next(iter(list(ids)[1:]),next(iter(ids),None))):
        if identifier not in ids:
            raise HTTPException(status_code=400,detail="Select tracks from this release")
        track=ids[identifier]
        try:
            data=json.loads(track.forensic_json or "{}")
            spectral=data.get("result",{}).get("authenticity",{}).get("spectral",{})
        except (ValueError,AttributeError):
            data,spectral={},{}
        chosen.append({"track":track,"spectral":spectral,"graph":managed_spectrogram(data) is not None})
    return templates.TemplateResponse(request=request,name="compare.html",context={"release":release,"chosen":chosen})


@router.post("/destinations/{kind}/analyze")
def analyze_destination(kind: str, path: str = Form(...)):
    if kind not in KINDS or kind == "trash":
        raise HTTPException(status_code=400, detail="Restore trashed files before analyzing them")
    root_value = workflow_settings()[kind]
    candidate = Path(path).resolve()
    if not root_value or not is_within(candidate, (Path(root_value).resolve(),)) or not candidate.is_dir():
        raise HTTPException(status_code=400, detail="Invalid destination folder")
    try:
        releases = discover_releases(candidate)
    except (OSError, BatchDiscoveryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not releases or not enqueue_paths(releases, "DESTINATION_RESCAN"):
        raise HTTPException(status_code=409, detail="No available FLAC albums to analyze")
    return RedirectResponse("/analyses", status_code=303)


@router.post("/trash/{entry_id}/restore")
def restore_album(request: Request, entry_id: str):
    try:
        restore_trash(entry_id, actor=getattr(request.state, "actor", "local"))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/destinations/trash", status_code=303)


@router.post("/trash/{entry_id}/purge")
def purge_album(request: Request, entry_id: str, confirmation: str = Form(...)):
    if confirmation != "ELIMINA":
        raise HTTPException(status_code=400, detail="Type ELIMINA to permanently delete this trashed album")
    try:
        purge_trash(entry_id, actor=getattr(request.state, "actor", "local"))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/destinations/trash", status_code=303)


@router.post("/settings/operations")
def operational_settings(analysis_timeout: int = Form(...), stall_timeout: int = Form(...),
                         retry_limit: int = Form(...), report_days: int = Form(...), upload_days: int = Form(...)):
    try:
        save_operational_settings(analysis_timeout, stall_timeout, retry_limit, report_days, upload_days)
    except WorkflowConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/settings#operations", status_code=303)


@router.get("/maintenance", response_class=HTMLResponse)
def maintenance(request: Request, removed: int = 0, bytes: int = 0):
    try:
        candidates = cleanup_preview()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return templates.TemplateResponse(request=request, name="maintenance.html", context={
        "candidates": candidates, "removed": removed, "removed_bytes": bytes,
        "total_bytes": sum(row["bytes"] for row in candidates), "settings": workflow_settings()})


@router.post("/maintenance/cleanup")
def clean_artifacts(tokens: list[str] = Form(...), confirmation: str = Form(...)):
    if confirmation != "PULISCI":
        raise HTTPException(status_code=400, detail="Type PULISCI to delete selected unreferenced artifacts")
    try:
        count, size = execute_cleanup(tokens)
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/maintenance?removed={count}&bytes={size}", status_code=303)
