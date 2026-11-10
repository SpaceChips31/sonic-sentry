from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.templating import Jinja2Templates

from app.presentation import configure_templates
from app.services.inventory import KINDS, destination_inventory
from app.services.settings import workflow_settings, save_operational_settings, WorkflowConfigurationError
from app.services.file_workflow import FileOperationError, is_within
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
def destinations(request: Request, kind: str, q: str = "", page: int = 1):
    try:
        data = destination_inventory(kind, q)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    total = len(data["rows"])
    pages = max(1, (total + 49) // 50)
    page = min(max(1, page), pages)
    data.update(rows=data["rows"][(page-1)*50:page*50], page=page, pages=pages, total=total,
                kinds=KINDS, settings=workflow_settings())
    return templates.TemplateResponse(request=request, name="destinations.html", context=data)


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
