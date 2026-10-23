from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.i18n import resolve_language
from app.models import AnalysisJob, AnalysisSource, FileTransfer, SourceObservation, Release, AnalyzedContent
from app.presentation import configure_templates
from app.services.file_workflow import (
    FileOperationError,
    delete_release_files,
    move_release,
    settings_snapshot,
    resume_transfer,
    close_transfer,
    check_pending_transfer,
    check_active_jobs,
    lock_file_operations,
)
from app.services.sources import SourceConfigurationError, add_source
from app.services.settings import WorkflowConfigurationError, save_workflow_settings, workflow_settings, save_ingestion_settings
from app.services.verdicts import recalculate_release_status
from pathlib import Path


router = APIRouter()
templates = configure_templates(Jinja2Templates(directory="app/templates"))


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request):
    with SessionLocal() as session:
        transfers = list(session.scalars(select(FileTransfer).order_by(FileTransfer.created_at.desc()).limit(100)))
        observations = list(session.scalars(select(SourceObservation).order_by(SourceObservation.id.desc()).limit(100)))
        sources = {source.id: source.name for source in session.scalars(select(AnalysisSource))}
        hashes = [item.content_hash for item in observations if item.content_hash]
        duplicates = dict(session.execute(select(AnalyzedContent.content_hash, AnalyzedContent.release_id).where(
            AnalyzedContent.content_hash.in_(hashes))).all())
    return templates.TemplateResponse(request=request, name="activity.html", context={
        "transfers": transfers, "observations": observations, "source_names": sources, "duplicate_releases": duplicates})


@router.post("/activity/transfers/{transfer_id}/resume")
def resume_file_transfer(transfer_id: str):
    try:
        resume_transfer(transfer_id)
    except FileOperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/activity", status_code=303)


@router.post("/activity/transfers/{transfer_id}/close")
def close_file_transfer(transfer_id: str):
    try:
        close_transfer(transfer_id)
    except FileOperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/activity", status_code=303)


@router.post("/settings/ingestion")
def update_ingestion(request: Request, scan_interval: int = Form(...), settle_seconds: int = Form(...)):
    try:
        save_ingestion_settings(scan_interval, settle_seconds)
    except WorkflowConfigurationError as exc:
        response = render_settings(request, error=str(exc))
        response.status_code = 400
        return response
    return RedirectResponse("/settings#ingestion", status_code=303)


@router.post("/settings/sources/{source_id}/watch")
def toggle_watch(source_id: int):
    with SessionLocal() as session:
        source = session.get(AnalysisSource, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        source.watch_enabled = not source.watch_enabled
        session.commit()
    return RedirectResponse("/settings#ingestion", status_code=303)


@router.post("/analyses/{job_id}/cancel")
def cancel_analysis(job_id: int):
    with SessionLocal() as session:
        lock_file_operations(session)
        job = session.get(AnalysisJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        if job.status not in {"QUEUED", "ANALYZING"}:
            raise HTTPException(status_code=409, detail="This job has already finished or is transferring files")
        job.cancel_requested = True
        if job.status == "QUEUED":
            job.status = "CANCELLED"
        session.commit()
    return RedirectResponse("/analyses", status_code=303)


@router.post("/releases/review")
def bulk_review(release_ids: list[int] = Form(...), decision: str = Form(...)):
    choices = {"approve": "APPROVED", "reject": "REJECTED", "reset": "NONE"}
    if decision not in choices or not release_ids or len(release_ids) > 50:
        raise HTTPException(status_code=400, detail="Select up to 50 albums and a valid decision")
    try:
        with SessionLocal() as session:
            lock_file_operations(session)
            releases = list(session.scalars(select(Release).where(Release.id.in_(set(release_ids))).options(selectinload(Release.tracks))))
            if len(releases) != len(set(release_ids)):
                raise HTTPException(status_code=404, detail="Release not found")
            for release in releases:
                check_pending_transfer(session, release.id)
                check_active_jobs(session, Path(release.source_path))
                if decision == "approve" and any(not track.integrity_ok for track in release.tracks):
                    raise FileOperationError("an album with integrity failures cannot be approved")
                for track in release.tracks:
                    if track.status == "QUARANTINE":
                        track.human_review = choices[decision]
                recalculate_release_status(release)
            session.commit()
    except FileOperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/releases", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return render_settings(request)


def render_settings(request: Request, *, error: str | None = None, values: dict | None = None):
    with SessionLocal() as session:
        sources = list(
            session.scalars(
                select(AnalysisSource).order_by(AnalysisSource.name, AnalysisSource.id)
            ).all()
        )
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": settings_snapshot(),
            "workflow": values if values is not None else workflow_settings(),
            "error": error,
            "sources": sources,
            "language_preference": request.cookies.get("lv_language", "auto"),
            "effective_language": resolve_language(request),
        },
    )


@router.post("/settings/workflow", response_class=HTMLResponse)
def update_workflow(request: Request, enabled: bool = Form(False), policy: str = Form("manual"),
                    movable_roots: str = Form(""), staging: str = Form(""),
                    quarantine: str = Form(""), rejected: str = Form("")):
    try:
        save_workflow_settings(enabled=enabled, policy=policy, movable_roots=movable_roots,
                               staging=staging, quarantine=quarantine, rejected=rejected)
    except WorkflowConfigurationError as exc:
        values = dict(enabled=enabled, auto_route=policy != "manual", route_nonpass=policy == "all",
                      movable_roots=movable_roots.splitlines(), staging=staging,
                      quarantine=quarantine, rejected=rejected)
        response = render_settings(request, error=str(exc), values=values)
        response.status_code = 400
        return response
    return RedirectResponse("/settings?saved=1#automation", status_code=303)


@router.post("/settings/language")
def set_language(language: str = Form(...)):
    if language not in {"auto", "it", "en"}:
        raise HTTPException(status_code=400, detail="Invalid language")
    response = RedirectResponse("/settings", status_code=303)
    if language == "auto":
        response.delete_cookie("lv_language")
    else:
        response.set_cookie(
            "lv_language",
            language,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
        )
    return response


@router.post("/settings/sources")
def create_source(
    name: str = Form(...),
    path: str = Form(...),
    kind: str = Form(...),
):
    try:
        add_source(name, path, kind)
    except SourceConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/sources/{source_id}/toggle")
def toggle_source(source_id: int):
    with SessionLocal() as session:
        source = session.get(AnalysisSource, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Sorgente non trovata")
        source.enabled = not source.enabled
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/sources/{source_id}/delete")
def delete_source(source_id: int):
    with SessionLocal() as session:
        source = session.get(AnalysisSource, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Sorgente non trovata")
        session.execute(delete(SourceObservation).where(SourceObservation.source_id == source_id))
        session.delete(source)
        session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/releases/{release_id}/files")
def release_file_action(
    release_id: int,
    action: str = Form(...),
):
    try:
        if action == "manual-pass":
            move_release(release_id, "STAGING", manual_pass=True)
        elif action == "quarantine":
            move_release(release_id, "QUARANTINE")
        elif action == "delete":
            delete_release_files(release_id)
        else:
            raise HTTPException(status_code=400, detail="Invalid action")
    except FileOperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return RedirectResponse(
        f"/releases/{release_id}",
        status_code=303,
    )
