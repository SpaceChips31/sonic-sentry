from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import Base, SessionLocal, engine
from app.models import Release


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


@app.get("/health")
def health():
    return {"status": "ok"}
