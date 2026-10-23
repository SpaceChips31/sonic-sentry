from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.i18n import resolve_language
from app.models import AnalysisSource
from app.presentation import configure_templates
from app.services.file_workflow import (
    FileOperationError,
    delete_release_files,
    move_release,
    settings_snapshot,
)
from app.services.sources import SourceConfigurationError, add_source
from app.services.settings import WorkflowConfigurationError, save_workflow_settings, workflow_settings


router = APIRouter()
templates = configure_templates(Jinja2Templates(directory="app/templates"))


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
