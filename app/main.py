import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import selectinload

from app.config import REPORT_ROOT, UPLOAD_ROOT
from app.database import Base, SessionLocal, engine
from app.migrations import initialize_database
from app.services.queue import enqueue_paths
from app.services.file_workflow import check_pending_transfer, check_active_jobs, lock_file_operations, FileOperationError
from app.models import AnalysisJob, AnalysisSource, Release, Track, ReleaseOperation, FileTransfer
from app.services.verdicts import recalculate_release_status
from app.services.settings import workflow_settings
from app.services.batch import BatchDiscoveryError, discover_releases
from app.services.importer import import_report
from app.presentation import configure_templates
from app.services.sources import seed_analysis_sources
from app.version import APP_NAME, APP_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database(engine)
    seed_analysis_sources()
    yield


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = configure_templates(Jinja2Templates(directory="app/templates"))

@app.exception_handler(StarletteHTTPException)
async def explain_http_error(request: Request, exc: StarletteHTTPException):
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(request=request, name="error.html", status_code=exc.status_code,
                                          context={"error": exc.detail, "status_code": exc.status_code})
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


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


def resolve_source_directory(source_id: int, relative_path: str) -> tuple[AnalysisSource, Path]:
    with SessionLocal() as session:
        source = session.get(AnalysisSource, source_id)
        if source is None or not source.enabled:
            raise HTTPException(status_code=404, detail="Analysis source not found")
        session.expunge(source)

    root = Path(source.path).resolve()
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

    return source, candidate

def safe_upload_path(filename: str) -> Path:
    normalized = filename.replace("\\", "/")
    candidate = PurePosixPath(normalized)

    if candidate.is_absolute() or PureWindowsPath(normalized).drive or ".." in candidate.parts or ":" in normalized or "\x00" in normalized:
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

        release_counters = {
            "total": len(releases),
            "pass": sum(r.status == "PASS" for r in releases),
            "quarantine": sum(r.status == "QUARANTINE" for r in releases),
            "rejected": sum(r.status == "REJECTED" for r in releases),
        }

        track_row = session.execute(
            select(
                func.count(Track.id),
                func.sum(case((Track.status == "PASS", 1), else_=0)),
                func.sum(case((Track.status == "QUARANTINE", 1), else_=0)),
                func.sum(case((Track.status == "REJECTED", 1), else_=0)),
                func.sum(
                    case(
                        (
                            (Track.status == "QUARANTINE")
                            & (Track.human_review == "NONE"),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case((Track.human_review == "APPROVED", 1), else_=0)
                ),
                func.sum(
                    case((Track.human_review == "REJECTED", 1), else_=0)
                ),
            )
        ).one()

        track_counters = {
            "total": int(track_row[0] or 0),
            "pass": int(track_row[1] or 0),
            "quarantine": int(track_row[2] or 0),
            "rejected": int(track_row[3] or 0),
            "pending_review": int(track_row[4] or 0),
            "approved": int(track_row[5] or 0),
            "human_rejected": int(track_row[6] or 0),
        }

        job_counters = {
            status: count
            for status, count in session.execute(
                select(AnalysisJob.status, func.count(AnalysisJob.id))
                .where(AnalysisJob.hidden.is_(False))
                .group_by(AnalysisJob.status)
            )
        }

        track_counters["pass_pct"] = (
            round(
                track_counters["pass"]
                / track_counters["total"]
                * 100,
                1,
            )
            if track_counters["total"]
            else 0
        )

        attention_releases = [
            release for release in releases
            if release.status in {"QUARANTINE", "REJECTED"}
        ][:6]

        active_priority = case(
            (AnalysisJob.status == "ANALYZING", 0),
            else_=1,
        )
        active_jobs = session.scalars(
            select(AnalysisJob)
            .where(AnalysisJob.status.in_({"ANALYZING", "QUEUED", "ROUTING"}))
            .order_by(active_priority, AnalysisJob.id)
            .limit(10)
        ).all()

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "releases": releases,
                "release_counters": release_counters,
                "track_counters": track_counters,
                "job_counters": job_counters,
                "attention_releases": attention_releases,
                "active_jobs": active_jobs,
                "has_active_jobs": bool(
                    job_counters.get("ANALYZING", 0)
                    or job_counters.get("QUEUED", 0)
                    or job_counters.get("ROUTING", 0)
                ),
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
            select(AnalysisJob).where(AnalysisJob.hidden.is_(False)).order_by(
                status_priority,
                AnalysisJob.id.desc(),
            )
        ).all()

        release_links = dict(session.execute(select(Release.source_path, Release.id)).all())
        for path, release_id in session.execute(select(ReleaseOperation.source_path, ReleaseOperation.release_id).order_by(ReleaseOperation.id)):
            release_links.setdefault(path, release_id)

        job_groups = {
            status: [
                job for job in jobs
                if job.status == status
            ]
            for status in (
                "ANALYZING",
                "QUEUED",
                "ROUTING",
                "CANCELLED",
                "FAILED",
                "COMPLETED",
            )
        }

        has_active_jobs = bool(
            job_groups["ANALYZING"]
            or job_groups["QUEUED"]
            or job_groups["ROUTING"]
        )

        return templates.TemplateResponse(
            request=request,
            name="analyses.html",
            context={
                "job_groups": job_groups,
                "release_links": release_links,
                "has_active_jobs": has_active_jobs,
            },
        )


@app.get("/releases", response_class=HTMLResponse)
def releases_page(request: Request, q: str = "", status: str = "", page: int = 1):
    if status not in {"", "PASS", "QUARANTINE", "REJECTED"}:
        raise HTTPException(status_code=400, detail="Invalid release status")
    statement = select(Release)
    if q.strip():
        statement = statement.where(Release.title.icontains(q.strip(), autoescape=True))
    if status:
        statement = statement.where(Release.status == status)
    with SessionLocal() as session:
        total = session.scalar(select(func.count()).select_from(statement.subquery()))
        pages = max(1, (total + 49) // 50)
        page = min(max(1, page), pages)
        releases = session.scalars(statement.order_by(Release.id.desc()).offset((page - 1) * 50).limit(50)).all()
    return templates.TemplateResponse(request=request, name="releases.html", context={
        "releases": releases, "query": q, "status": status, "page": page, "pages": pages, "total": total,
    })


@app.post("/analyses/{job_id}/retry")
def retry_analysis(job_id: int):
    with SessionLocal() as session:
        lock_file_operations(session)
        job = session.get(AnalysisJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        if job.status not in {"FAILED", "CANCELLED"}:
            raise HTTPException(status_code=409, detail="Only failed or cancelled analyses can be retried")
        if not Path(job.source_path).is_dir():
            raise HTTPException(status_code=409, detail="Source directory no longer exists")
        try:
            check_active_jobs(session, Path(job.source_path))
            if job.release_id:
                check_pending_transfer(session, job.release_id)
        except FileOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        job.status = "QUEUED"
        job.hidden = False
        job.cancel_requested = False
        job.progress = job.progress_total = 0
        job.started_at = job.finished_at = None
        job.error = None
        session.commit()
    return RedirectResponse("/analyses", status_code=303)


@app.post("/analyses/clear/{status}")
def clear_analysis_history(status: str):
    normalized = status.upper()

    if normalized not in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise HTTPException(
            status_code=400,
            detail="Only completed or failed jobs can be cleared",
        )

    with SessionLocal() as session:
        session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.status == normalized).values(hidden=True)
        )
        session.commit()

    return RedirectResponse("/analyses", status_code=303)


@app.get("/analyses/new", response_class=HTMLResponse)
def new_analysis(
    request: Request,
    source: int | None = None,
    path: str = "",
    q: str = "",
):
    with SessionLocal() as session:
        sources = list(
            session.scalars(
                select(AnalysisSource)
                .where(AnalysisSource.enabled.is_(True))
                .order_by(AnalysisSource.name, AnalysisSource.id)
            ).all()
        )

    current_source = None
    current = None
    directories = []
    parent_path = None
    total_directories = 0
    query = q.strip()

    if sources:
        source_id = source if any(item.id == source for item in sources) else sources[0].id
        current_source, current = resolve_source_directory(source_id, path)
        root_path = Path(current_source.path).resolve()
        relative_current = current.relative_to(root_path)

        if relative_current != Path("."):
            parent = relative_current.parent
            parent_path = "" if parent == Path(".") else str(parent)

        entries = sorted(
            (item for item in current.iterdir() if item.is_dir()),
            key=lambda item: item.name.casefold(),
        )
        if query:
            entries = [item for item in entries if query.casefold() in item.name.casefold()]
        total_directories = len(entries)

        for entry in entries[:200]:
            relative = entry.relative_to(root_path)
            directories.append({
                "name": entry.name,
                "relative": str(relative),
                "encoded": quote(str(relative)),
            })
    else:
        source_id = None

    return templates.TemplateResponse(
        request=request,
        name="new_analysis.html",
        context={
            "sources": sources,
            "source_id": source_id,
            "current_source": current_source,
            "relative_path": path,
            "query": query,
            "current": current,
            "directories": directories,
            "total_directories": total_directories,
            "results_limited": total_directories > len(directories),
            "parent_path": parent_path,
            "parent_encoded": quote(parent_path or ""),
        },
    )


@app.get("/api/sources/{source_id}/directories")
def source_directories(
    source_id: int,
    path: str = "",
    q: str = "",
):
    configured_source, current = resolve_source_directory(source_id, path)
    root = Path(configured_source.path).resolve()
    query = q.strip().casefold()

    entries = sorted(
        (item for item in current.iterdir() if item.is_dir()),
        key=lambda item: item.name.casefold(),
    )
    if query:
        entries = [item for item in entries if query in item.name.casefold()]

    total = len(entries)
    return {
        "directories": [
            {
                "name": item.name,
                "relative": str(item.relative_to(root)),
            }
            for item in entries[:200]
        ],
        "total": total,
        "limited": total > 200,
    }


@app.post("/analyses/container")
def create_container_analysis(
    source_id: int = Form(...),
    relative_path: str = Form(...),
):
    configured_source, source = resolve_source_directory(source_id, relative_path)

    if not any(
        file.is_file() and file.suffix.lower() == ".flac"
        for file in source.rglob("*")
    ):
        raise HTTPException(
            status_code=400,
            detail="The selected directory contains no FLAC files",
        )

    if not enqueue_paths([source], configured_source.kind):
        raise HTTPException(status_code=409, detail="This album overlaps an active analysis or transfer")

    return RedirectResponse("/analyses", status_code=303)

@app.post("/analyses/batch")
def create_batch_analysis(
    source_id: int = Form(...),
    relative_path: str = Form(...),
):
    configured_source, source = resolve_source_directory(source_id, relative_path)

    try:
        releases = discover_releases(source)
    except BatchDiscoveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not releases:
        raise HTTPException(
            status_code=400,
            detail="No FLAC releases were found below the selected directory",
        )

    if not enqueue_paths(releases, f"{configured_source.kind}_BATCH"):
        raise HTTPException(status_code=409, detail="All discovered albums overlap active analyses or transfers")

    return RedirectResponse("/analyses", status_code=303)

@app.post("/analyses/upload")
async def create_upload_analysis(
    files: list[UploadFile] = File(...),
):
    accepted = []
    seen_paths = set()

    for upload in files:
        if not upload.filename:
            continue
        relative = safe_upload_path(upload.filename)

        if relative.suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS:
            if relative in seen_paths:
                raise HTTPException(status_code=400, detail="Duplicate upload filename")
            seen_paths.add(relative)
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
    filename = Path((report.filename or "").replace("\\", "/")).name

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
            .options(
                selectinload(Release.tracks),
                selectinload(Release.operations),
            )
        )

        if release is None:
            raise HTTPException(status_code=404, detail="Release not found")

        return templates.TemplateResponse(
            request=request,
            name="release.html",
            context={"release": release, "file_operations_enabled": workflow_settings()["enabled"],
                     "pending_transfer": session.scalar(select(FileTransfer.id).where(FileTransfer.release_id == release.id,
                                                                                   FileTransfer.state.not_in({"COMPLETE", "ABANDONED"})))},
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
        lock_file_operations(session)
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

        try:
            check_pending_transfer(session, track.release_id)
            check_active_jobs(session, Path(track.release.source_path))
        except FileOperationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        track.human_review = decisions[decision]
        recalculate_release_status(track.release)
        session.commit()

        return RedirectResponse(
            f"/tracks/{track.id}",
            status_code=303,
        )


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


from app.workflow import router as workflow_router

app.include_router(workflow_router)
