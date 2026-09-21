from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import Release
from app.services.file_workflow import (
    FileOperationError,
    delete_release_files,
    move_release,
    settings_snapshot,
)
from app.version import APP_VERSION


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = APP_VERSION


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"settings": settings_snapshot()},
    )


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
