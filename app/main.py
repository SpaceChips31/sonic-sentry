import json
import subprocess
from statistics import median
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import selectinload

from app.config import REPORT_ROOT, UPLOAD_ROOT
from app.database import Base, SessionLocal, engine, migrate_schema
from app.models import AnalysisBatch, AnalysisJob, AnalysisSource, Release, ReviewDecision, Track
from app.services.batch import BatchDiscoveryError, discover_releases
from app.services.importer import import_report
from app.presentation import configure_templates
from app.services.sources import seed_analysis_sources
from app.version import APP_NAME, APP_VERSION
from app.auth import AuthenticationMiddleware, router as auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    seed_analysis_sources()
    yield


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    lifespan=lifespan,
)
app.add_middleware(AuthenticationMiddleware)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = configure_templates(Jinja2Templates(directory="app/templates"))

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


def actor_name(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user.username if user else "local"


def record_review(
    session,
    track: Track,
    decision: str,
    *,
    actor: str,
    note: str = "",
) -> None:
    previous = track.human_review
    track.human_review = decision
    session.add(
        ReviewDecision(
            track_id=track.id,
            release_id=track.release_id,
            decision=decision,
            previous_review=previous,
            actor=actor,
            note=note.strip(),
            created_at=utc_now(),
        )
    )


def spectral_comparison(track: Track, peers: list[Track]) -> dict:
    def spectral(item: Track) -> dict:
        payload = json.loads(item.forensic_json or "{}")
        return payload.get("result", {}).get("authenticity", {}).get("spectral", {})

    current = spectral(track)
    samples = [spectral(peer) for peer in peers]
    cutoffs = [float(item["cutoff_hz"]) for item in samples if item.get("cutoff_hz")]
    confidences = [float(item["net_confidence_pct"]) for item in samples if item.get("net_confidence_pct") is not None]
    cutoff_median = median(cutoffs) if cutoffs else None
    confidence_median = median(confidences) if confidences else None
    current_cutoff = current.get("cutoff_hz")
    return {
        "cutoff_median": cutoff_median,
        "confidence_median": confidence_median,
        "cutoff_delta": (float(current_cutoff) - cutoff_median) if current_cutoff and cutoff_median else None,
        "peer_count": len(peers),
        "album_pattern": sum(peer.status == track.status for peer in peers) >= max(2, len(peers) // 2),
    }


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
            .where(AnalysisJob.status.in_({"ANALYZING", "QUEUED"}))
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
            select(AnalysisJob).order_by(
                status_priority,
                AnalysisJob.id.desc(),
            )
        ).all()
        batches = session.scalars(
            select(AnalysisBatch)
            .options(selectinload(AnalysisBatch.jobs))
            .order_by(AnalysisBatch.id.desc())
            .limit(20)
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
                "CANCELLED",
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
                "batches": batches,
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

    with SessionLocal() as session:
        job = AnalysisJob(
            source_type=configured_source.kind,
            source_path=str(source),
            display_name=source.name,
            status="QUEUED",
            created_at=utc_now(),
        )
        session.add(job)
        session.commit()

    return RedirectResponse("/analyses", status_code=303)

@app.post("/analyses/batch")
def create_batch_analysis(
    source_id: int = Form(...),
    relative_path: str = Form(...),
    selected: list[str] = Form(default=[]),
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

    if selected:
        allowed = {str(item) for item in releases}
        releases = [
            Path(item).resolve()
            for item in selected
            if str(Path(item).resolve()) in allowed
        ]
        if not releases:
            raise HTTPException(status_code=400, detail="No valid releases were selected")

    created = 0
    with SessionLocal() as session:
        batch = AnalysisBatch(
            name=source.name,
            source_path=str(source),
            status="ACTIVE",
            created_at=utc_now(),
        )
        session.add(batch)
        session.flush()
        active_paths = set(
            session.scalars(
                select(AnalysisJob.source_path).where(
                    AnalysisJob.status.in_({"QUEUED", "ANALYZING"})
                )
            ).all()
        )

        for release_path in releases:
            path_text = str(release_path)
            if path_text in active_paths:
                continue
            session.add(
                AnalysisJob(
                    source_type=f"{configured_source.kind}_BATCH",
                    source_path=path_text,
                    display_name=release_path.name,
                    status="QUEUED",
                    created_at=utc_now(),
                    batch_id=batch.id,
                )
            )
            created += 1

        session.commit()

    if not created:
        raise HTTPException(
            status_code=409,
            detail="All discovered releases are already queued or being analyzed",
        )

    return RedirectResponse("/analyses", status_code=303)


@app.post("/analyses/batch/preview", response_class=HTMLResponse)
def preview_batch(
    request: Request,
    source_id: int = Form(...),
    relative_path: str = Form(...),
):
    configured_source, source = resolve_source_directory(source_id, relative_path)
    try:
        releases = discover_releases(source)
    except BatchDiscoveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request=request,
        name="batch_preview.html",
        context={
            "source": configured_source,
            "relative_path": relative_path,
            "root": source,
            "releases": releases,
        },
    )


@app.post("/analyses/batches/{batch_id}/{action}")
def control_batch(batch_id: int, action: str):
    statuses = {"pause": "PAUSED", "resume": "ACTIVE", "cancel": "CANCELLED"}
    if action not in statuses:
        raise HTTPException(status_code=400, detail="Invalid batch action")
    with SessionLocal() as session:
        batch = session.get(AnalysisBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found")
        batch.status = statuses[action]
        if action == "cancel":
            session.execute(
                update(AnalysisJob)
                .where(AnalysisJob.batch_id == batch.id, AnalysisJob.status == "QUEUED")
                .values(status="CANCELLED", error="Cancelled with batch")
            )
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
        peers = list(
            session.scalars(
                select(Track)
                .where(Track.release_id == track.release_id)
                .order_by(Track.path)
            ).all()
        )
        decisions = list(
            session.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.track_id == track.id)
                .order_by(ReviewDecision.id.desc())
            ).all()
        )

        return templates.TemplateResponse(
            request=request,
            name="track.html",
            context={
                "track": track,
                "spectral": spectral,
                "comparison": spectral_comparison(track, peers),
                "decisions": decisions,
            },
        )


@app.post("/tracks/{track_id}/review/{decision}")
def review_track(request: Request, track_id: int, decision: str, note: str = Form("")):
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

        record_review(
            session,
            track,
            decisions[decision],
            actor=actor_name(request),
            note=note,
        )
        recalculate_release_status(track.release)
        session.commit()

        return RedirectResponse(
            f"/tracks/{track.id}",
            status_code=303,
        )


@app.get("/reviews", response_class=HTMLResponse)
def review_queue(request: Request):
    with SessionLocal() as session:
        tracks = list(
            session.scalars(
                select(Track)
                .where(Track.status == "QUARANTINE", Track.human_review == "NONE")
                .options(selectinload(Track.release))
                .order_by(Track.release_id, Track.path)
            ).all()
        )
    return templates.TemplateResponse(
        request=request,
        name="reviews.html",
        context={"tracks": tracks},
    )


@app.post("/reviews/bulk")
def bulk_review(
    request: Request,
    track_ids: list[int] = Form(default=[]),
    decision: str = Form(...),
    note: str = Form(""),
):
    mapped = {"approve": "APPROVED", "reject": "REJECTED"}
    if decision not in mapped or not track_ids:
        raise HTTPException(status_code=400, detail="Select tracks and a valid decision")
    with SessionLocal() as session:
        tracks = list(
            session.scalars(
                select(Track)
                .where(Track.id.in_(track_ids), Track.status == "QUARANTINE")
                .options(selectinload(Track.release).selectinload(Release.tracks))
            ).all()
        )
        releases = {}
        for track in tracks:
            record_review(session, track, mapped[decision], actor=actor_name(request), note=note)
            releases[track.release.id] = track.release
        for release in releases.values():
            recalculate_release_status(release)
        session.commit()
    return RedirectResponse("/reviews", status_code=303)


@app.post("/releases/{release_id}/review/{decision}")
def review_release(request: Request, release_id: int, decision: str, note: str = Form("")):
    mapped = {"approve": "APPROVED", "reject": "REJECTED", "reset": "NONE"}
    if decision not in mapped:
        raise HTTPException(status_code=400, detail="Invalid decision")
    with SessionLocal() as session:
        release = session.scalar(
            select(Release).where(Release.id == release_id).options(selectinload(Release.tracks))
        )
        if release is None:
            raise HTTPException(status_code=404, detail="Release not found")
        for track in release.tracks:
            if track.status == "QUARANTINE":
                record_review(session, track, mapped[decision], actor=actor_name(request), note=note)
        recalculate_release_status(release)
        session.commit()
    return RedirectResponse(f"/releases/{release_id}", status_code=303)


@app.post("/reviews/{review_id}/undo")
def undo_review(review_id: int):
    with SessionLocal() as session:
        review = session.get(ReviewDecision, review_id)
        if review is None or review.undone:
            raise HTTPException(status_code=404, detail="Review decision not found")
        newer = session.scalar(
            select(ReviewDecision.id)
            .where(
                ReviewDecision.track_id == review.track_id,
                ReviewDecision.id > review.id,
                ReviewDecision.undone.is_(False),
            )
            .limit(1)
        )
        if newer is not None:
            raise HTTPException(status_code=409, detail="Only the latest decision can be undone")
        track = session.scalar(
            select(Track)
            .where(Track.id == review.track_id)
            .options(selectinload(Track.release).selectinload(Release.tracks))
        )
        if track is None:
            raise HTTPException(status_code=404, detail="Track not found")
        track.human_review = review.previous_review
        review.undone = True
        recalculate_release_status(track.release)
        session.commit()
        track_id = track.id
    return RedirectResponse(f"/tracks/{track_id}", status_code=303)


def spectrogram_command(source: Path, destination: Path) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-lavfi", "showspectrumpic=s=1400x500:legend=1:color=intensity:scale=log",
        str(destination),
    ]


@app.get("/tracks/{track_id}/spectrogram.png")
def track_spectrogram(track_id: int):
    with SessionLocal() as session:
        track = session.get(Track, track_id)
        if track is None:
            raise HTTPException(status_code=404, detail="Track not found")
        source = Path(track.path)
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
    cache = REPORT_ROOT / "spectrograms"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"track-{track_id}.png"
    if not destination.exists() or destination.stat().st_mtime < source.stat().st_mtime:
        try:
            subprocess.run(spectrogram_command(source, destination), check=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Unable to create spectrogram: {exc}") from exc
    return FileResponse(destination, media_type="image/png")


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


from app.workflow import router as workflow_router

app.include_router(workflow_router)
app.include_router(auth_router)
