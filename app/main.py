import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import Base, SessionLocal, engine
from app.models import Release, Track


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


def recalculate_release_status(release: Release) -> None:
    if any(
        track.status == "REJECTED"
        or track.human_review == "REJECTED"
        for track in release.tracks
    ):
        release.status = "REJECTED"
        return

    if any(
        track.status == "QUARANTINE"
        and track.human_review != "APPROVED"
        for track in release.tracks
    ):
        release.status = "QUARANTINE"
        return

    release.status = "PASS"


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
        raise HTTPException(status_code=400, detail="Invalid review decision")

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
            url=f"/tracks/{track.id}",
            status_code=303,
        )


@app.get("/health")
def health():
    return {"status": "ok"}
