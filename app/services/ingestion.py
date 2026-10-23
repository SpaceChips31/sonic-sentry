"""Polling ingestion: stable albums only, durable observations and exact-file deduplication."""
import time
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AnalysisJob, AnalysisSource, AnalyzedContent, SourceObservation
from app.services.batch import discover_releases
from app.services.files import INCOMPLETE_SUFFIXES, audio_content_hash, fingerprint, snapshot
from app.services.file_workflow import utc_now
from app.services.queue import enqueue_paths
from app.services.settings import workflow_settings

MAX_ENQUEUE_PER_SCAN = 25


def scan_sources(*, now: float | None = None) -> int:
    now = time.time() if now is None else now
    settings = workflow_settings()
    excluded = tuple(Path(settings[key]).resolve() for key in ("staging", "quarantine", "rejected") if settings[key])
    with SessionLocal() as session:
        sources = list(session.scalars(select(AnalysisSource).where(
            AnalysisSource.enabled.is_(True), AnalysisSource.watch_enabled.is_(True))))
    queued = 0
    for source in sources:
        try:
            root = Path(source.path).resolve()
            if not root.is_dir():
                raise ValueError("Source directory is missing")
            releases = discover_releases(root, excluded_roots=excluded, include_root=False, limit=None)
            seen = set()
            for album in releases:
                if album == root:
                    continue  # Never automatically handle a source root as an album.
                seen.add(str(album))
                queued += observe_album(source, album, now, settings["settle_seconds"], allow_queue=queued < MAX_ENQUEUE_PER_SCAN)
            with SessionLocal() as session:
                for observation in session.scalars(select(SourceObservation).where(SourceObservation.source_id == source.id)):
                    if observation.path not in seen:
                        observation.state = "MISSING"
                saved = session.get(AnalysisSource, source.id)
                if saved:
                    saved.last_scan_at, saved.watch_error = utc_now(), None
                session.commit()
        except (OSError, ValueError) as exc:
            with SessionLocal() as session:
                saved = session.get(AnalysisSource, source.id)
                if saved:
                    saved.last_scan_at, saved.watch_error = utc_now(), str(exc)
                    session.commit()
    return queued


def observe_album(source: AnalysisSource, album: Path, now: float, settle_seconds: int, *, allow_queue: bool = True) -> int:
    manifest = snapshot(album)
    signature = fingerprint(manifest)
    incomplete = any(Path(name).suffix.lower() in INCOMPLETE_SUFFIXES or name.lower().endswith(".!qb") for name in manifest)
    newest = max((item["mtime"] / 1_000_000_000 for item in manifest.values()), default=now)
    with SessionLocal() as session:
        observation = session.scalar(select(SourceObservation).where(
            SourceObservation.source_id == source.id, SourceObservation.path == str(album)))
        if observation is None:
            session.add(SourceObservation(source_id=source.id, path=str(album), fingerprint=signature,
                                          stable_since=now, state="WAITING"))
            session.commit()
            return 0
        if observation.fingerprint != signature or observation.state == "MISSING":
            observation.fingerprint, observation.stable_since = signature, now
            observation.state, observation.job_id, observation.content_hash = "WAITING", None, None
            observation.message = None
            session.commit()
            return 0
        if observation.state in {"QUEUED", "ANALYZING", "ROUTING", "COMPLETED", "FAILED", "CANCELLED", "DUPLICATE"}:
            if observation.job_id:
                job = session.get(AnalysisJob, observation.job_id)
                if job:
                    observation.state = job.status
                    session.commit()
            return 0
        if incomplete or now - max(observation.stable_since, newest) < settle_seconds:
            observation.message = "Download is incomplete" if incomplete else "Waiting for stable files"
            session.commit()
            return 0
        if not allow_queue:
            observation.message = "Waiting for the next ingestion batch"
            session.commit()
            return 0
        digest = audio_content_hash(album)
        if fingerprint(snapshot(album)) != signature:
            observation.stable_since = now
            session.commit()
            return 0
        observation.content_hash = digest
        duplicate = session.get(AnalyzedContent, digest)
        pending = session.scalar(select(SourceObservation.id).join(AnalysisJob, SourceObservation.job_id == AnalysisJob.id).where(
            SourceObservation.content_hash == digest, SourceObservation.id != observation.id,
            AnalysisJob.status.in_({"QUEUED", "ANALYZING"})))
        if duplicate:
            observation.state, observation.message = "DUPLICATE", f"Exact FLAC duplicate of release #{duplicate.release_id}; files left in place"
            session.commit()
            return 0
        if pending:
            observation.message = "Identical content is already being analyzed"
            session.commit()
            return 0
        observation_id = observation.id
        session.commit()
    # Do not keep a read transaction open while the queue acquires its writer lock.
    ids = enqueue_paths([album], f"{source.kind}_WATCH")
    with SessionLocal() as session:
        observation = session.get(SourceObservation, observation_id)
        if ids:
            observation.state, observation.job_id, observation.message = "QUEUED", ids[0], None
        else:
            observation.message = "Album is busy; waiting for the next scan"
        session.commit()
    return len(ids)
