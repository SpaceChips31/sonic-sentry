import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, select
from sqlalchemy.orm import selectinload

from app.config import REPORT_ROOT, SOURCE_ROOTS, UPLOAD_ROOT
from app.database import Base, SessionLocal, engine
from app.models import AnalysisJob, Release, Track
from app.services.importer import import_report


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Lossless Validator",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

ALLOWED_UPLOAD_EXTENSIONS = {
    ".flac",
    ".cue",
    ".log",
    ".jpg",
    ".jpeg",
    ".png",
    ".txt",
    ".m3u",
    ".m3u8",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def recalculate_release_status(release: Release) -> None:
    if any(
        track.status == "REJECTED"
        or track.human_review == "REJECTED"
        for track in release.tracks
    ):
        release.status = "REJECTED"
    elif any(
        track.status == "QUARANTINE"
        and track.human_review != "APPROVED"
        for track in release.tracks
    ):
        release.status = "QUARANTINE"
    else:
        release.status = "PASS"


def resolve_source_directory(root_index: int, relative_path: str) -> Path:
    if root_index < 0 or root_index >= len(SOURCE_ROOTS):
        raise HTTPException(status_code=400, detail="Invalid source root")

    root = SOURCE_ROOTS[root_index]
    candidate = (root / relative_path).resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Path is outside the authorized source root",
        ) from exc

    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    return candidate


def safe_upload_path(filename: str) -> Path:
    normalized = filename.replace("\\", "/")
    candidate = PurePosixPath(normalized)

    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="Unsafe upload path")

    parts = tuple(
        part for part in candidate.parts
        if part not in ("", ".")
    )

    if not parts:
        raise HTTPException(status_code=400, detail="Invalid upload filename")

    return Path(*parts)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as session:
        releases = session.scalars(
            select(Release).order_by(Release.id.desc())
        ).all()

        counters = {
            "total": len(releases),
            "pass": sum(r.status == "PASS" for r in releases),
            "quarantine": sum(r.status == "QUARANTINE" for r in releases),
            "rejected": sum(r.status == "REJECTED" for r in releases),
        }

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "releases": releases,
                "counters": counters,
            },
        )


@app.get("/analyses", response_class=HTMLResponse)
def analyses(request: Request):
    with SessionLocal() as session:
        status_priority = case(
            (AnalysisJob.status == "ANALYZING", 0),
            (AnalysisJob.status == "QUEUED", 1),
            (AnalysisJob.status == "FAILED", 2),
            (AnalysisJob.status == "COMPLETED", 3),
            else_=4,
        )

        jobs = session.scalars(
            select(AnalysisJob).order_by(
                status_priority,
                AnalysisJob.id.desc(),
            )
        ).all()

        job_groups = {
            status: [
                job for job in jobs
                if job.status == status
            ]
            for status in (
                "ANALYZING",
                "QUEUED",
                "FAILED",
                "COMPLETED",
            )
        }

        has_active_jobs = bool(
            job_groups["ANALYZING"]
            or job_groups["QUEUED"]
        )

        return templates.TemplateResponse(
            request=request,
            name="analyses.html",
            context={
                "job_groups": job_groups,
                "has_active_jobs": has_active_jobs,
            },
        )


@app.post("/analyses/clear/{status}")
def clear_analysis_history(status: str):
    normalized = status.upper()

    if normalized not in {"COMPLETED", "FAILED"}:
        raise HTTPException(
            status_code=400,
            detail="Only completed or failed jobs can be cleared",
        )

    with SessionLocal() as session:
        session.execute(
            delete(AnalysisJob)
            .where(AnalysisJob.status == normalized)
        )
        session.commit()

    return RedirectResponse("/analyses", status_code=303)


@app.get("/analyses/new", response_class=HTMLResponse)
def new_analysis(
    request: Request,
    root: int = 0,
    path: str = "",
):
    current = None
    directories = []
    parent_path = None

    if SOURCE_ROOTS:
        current = resolve_source_directory(root, path)
        root_path = SOURCE_ROOTS[root]

        relative_current = current.relative_to(root_path)

        if relative_current != Path("."):
            parent = relative_current.parent
            parent_path = "" if parent == Path(".") else str(parent)

        for entry in sorted(
            (item for item in current.iterdir() if item.is_dir()),
            key=lambda item: item.name.casefold(),
        ):
            relative = entry.relative_to(root_path)
            directories.append({
                "name": entry.name,
                "relative": str(relative),
                "encoded": quote(str(relative)),
            })

    roots = [
        {
            "index": index,
            "path": str(root_path),
            "name": root_path.name or str(root_path),
        }
        for index, root_path in enumerate(SOURCE_ROOTS)
    ]

    return templates.TemplateResponse(
        request=request,
        name="new_analysis.html",
        context={
            "roots": roots,
            "root_index": root,
            "relative_path": path,
            "current": current,
            "directories": directories,
            "parent_path": parent_path,
            "parent_encoded": quote(parent_path or ""),
        },
    )


@app.post("/analyses/container")
def create_container_analysis(
    root_index: int = Form(...),
    relative_path: str = Form(...),
):
    source = resolve_source_directory(root_index, relative_path)

    if not any(
        file.is_file() and file.suffix.lower() == ".flac"
        for file in source.rglob("*")
    ):
        raise HTTPException(
            status_code=400,
            detail="The selected directory contains no FLAC files",
        )

    with SessionLocal() as session:
        job = AnalysisJob(
            source_type="CONTAINER",
            source_path=str(source),
            display_name=source.name,
            status="QUEUED",
            created_at=utc_now(),
        )
        session.add(job)
        session.commit()

    return RedirectResponse("/analyses", status_code=303)


@app.post("/analyses/upload")
async def create_upload_analysis(
    files: list[UploadFile] = File(...),
):
    accepted = []

    for upload in files:
        relative = safe_upload_path(upload.filename or "")

        if relative.suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS:
            accepted.append((upload, relative))

    if not any(
        relative.suffix.lower() == ".flac"
        for _, relative in accepted
    ):
        raise HTTPException(
            status_code=400,
            detail="At least one FLAC file is required",
        )

    upload_id = uuid4().hex
    destination_root = (UPLOAD_ROOT / upload_id).resolve()
    destination_root.mkdir(parents=True)

    for upload, relative in accepted:
        destination = (destination_root / relative).resolve()

        try:
            destination.relative_to(destination_root)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Unsafe destination path",
            ) from exc

        destination.parent.mkdir(parents=True, exist_ok=True)

        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)

        await upload.close()

    display_name = next(
        (
            relative.parts[0]
            for _, relative in accepted
            if len(relative.parts) > 1
        ),
        f"Local upload {upload_id[:8]}",
    )

    with SessionLocal() as session:
        job = AnalysisJob(
            source_type="UPLOAD",
            source_path=str(destination_root),
            display_name=display_name,
            status="QUEUED",
            created_at=utc_now(),
        )
        session.add(job)
        session.commit()

    return RedirectResponse("/analyses", status_code=303)


@app.post("/analyses/report")
async def upload_report(report: UploadFile = File(...)):
    filename = Path(report.filename or "").name

    if not filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail="The report must be a JSON file",
        )

    destination = REPORT_ROOT / f"{uuid4().hex}-{filename}"

    with destination.open("wb") as output:
        while chunk := await report.read(1024 * 1024):
            output.write(chunk)

    await report.close()

    try:
        release = import_report(destination)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid validator report: {exc}",
        ) from exc

    return RedirectResponse(
        f"/releases/{release.id}",
        status_code=303,
    )


@app.get("/releases/{release_id}", response_class=HTMLResponse)
def release_detail(request: Request, release_id: int):
    with SessionLocal() as session:
        release = session.scalar(
            select(Release)
            .where(Release.id == release_id)
            .options(selectinload(Release.tracks))
        )

        if release is None:
            raise HTTPException(status_code=404, detail="Release not found")

        return templates.TemplateResponse(
            request=request,
            name="release.html",
            context={"release": release},
        )


@app.get("/tracks/{track_id}", response_class=HTMLResponse)
def track_detail(request: Request, track_id: int):
    with SessionLocal() as session:
        track = session.scalar(
            select(Track)
            .where(Track.id == track_id)
            .options(selectinload(Track.release))
        )

        if track is None:
            raise HTTPException(status_code=404, detail="Track not found")

        forensics = json.loads(track.forensic_json or "{}")
        result = forensics.get("result", {})
        spectral = result.get("authenticity", {}).get("spectral", {})

        return templates.TemplateResponse(
            request=request,
            name="track.html",
            context={
                "track": track,
                "spectral": spectral,
            },
        )


@app.post("/tracks/{track_id}/review/{decision}")
def review_track(track_id: int, decision: str):
    decisions = {
        "approve": "APPROVED",
        "reject": "REJECTED",
        "reset": "NONE",
    }

    if decision not in decisions:
        raise HTTPException(status_code=400, detail="Invalid decision")

    with SessionLocal() as session:
        track = session.scalar(
            select(Track)
            .where(Track.id == track_id)
            .options(
                selectinload(Track.release)
                .selectinload(Release.tracks)
            )
        )

        if track is None:
            raise HTTPException(status_code=404, detail="Track not found")

        if track.status != "QUARANTINE":
            raise HTTPException(
                status_code=409,
                detail="Only quarantined tracks can be reviewed",
            )

        track.human_review = decisions[decision]
        recalculate_release_status(track.release)
        session.commit()

        return RedirectResponse(
            f"/tracks/{track.id}",
            status_code=303,
        )


@app.get("/health")
def health():
    return {"status": "ok"}
