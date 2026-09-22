from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.i18n import resolve_language
from app.models import AnalysisSource, Release
from app.presentation import configure_templates
from app.services.file_workflow import (
    FileOperationError,
    delete_release_files,
    move_release,
    settings_snapshot,
)
from app.services.sources import SourceConfigurationError, add_source


router = APIRouter()
templates = configure_templates(Jinja2Templates(directory="app/templates"))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
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
            "sources": sources,
            "language_preference": request.cookies.get("lv_language", "auto"),
            "effective_language": resolve_language(request),
        },
    )


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
