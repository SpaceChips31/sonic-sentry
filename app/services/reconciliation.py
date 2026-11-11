"""Read-only file discovery with explicitly confirmed database-only path reconciliation."""
import json
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import SessionLocal
from app.models import Release, Track, ReconcileScan, AnalysisSource, ReleaseOperation, TrashEntry
from app.services.files import flac_frame_hash, album_files
from app.services.batch import discover_releases
from app.services.settings import workflow_settings
from app.services.file_workflow import (utc_now, check_pending_transfer, check_active_jobs,
    lock_file_operations, FileOperationError, is_within)
from app.services.locks import process_lock


def allowed_roots():
    settings = workflow_settings()
    with SessionLocal() as session:
        paths = list(session.scalars(select(AnalysisSource.path).where(AnalysisSource.enabled.is_(True))))
    paths += settings["movable_roots"] + [settings[key] for key in ("staging","quarantine","rejected") if settings[key]]
    trash = Path(settings["trash"]).resolve() if settings["trash"] else None
    return sorted({str(Path(p).resolve()) for p in paths if Path(p).is_dir() and not (trash and Path(p).resolve().is_relative_to(trash))})


def enqueue_scan(root, actor):
    path = str(Path(root).resolve())
    if path not in allowed_roots():
        raise FileOperationError("Select a configured source or destination folder")
    with SessionLocal() as session:
        lock_file_operations(session)
        existing = session.scalar(select(ReconcileScan.id).where(ReconcileScan.root == path,ReconcileScan.state.in_({"QUEUED","RUNNING"})))
        if existing:
            return existing
        identifier = uuid4().hex
        session.add(ReconcileScan(id=identifier,root=path,state="QUEUED",actor=actor,created_at=utc_now()))
        session.commit()
    return identifier


def process_next_scan():
    with SessionLocal() as session:
        lock_file_operations(session)
        scan = session.scalar(select(ReconcileScan).where(ReconcileScan.state == "QUEUED").order_by(ReconcileScan.created_at).limit(1))
        if scan is None:
            return False
        scan.state = "RUNNING"
        session.commit()
    try:
        if scan.root not in allowed_roots():
            raise FileOperationError("Scan folder is no longer configured")
        with process_lock("file-workflow"):
            results = scan_paths(scan.id, Path(scan.root))
        state, error = "COMPLETE", None
    except (OSError, RuntimeError, ValueError) as exc:
        state, error, results = "FAILED", str(exc), []
    with SessionLocal() as session:
        saved = session.get(ReconcileScan, scan.id)
        saved.state, saved.error, saved.results_json = state,error,json.dumps(results)
        session.commit()
    return True


def scan_paths(scan_id, root):
    with SessionLocal() as session:
        releases = list(session.scalars(select(Release).options(selectinload(Release.tracks))))
        trashed = set(session.scalars(select(TrashEntry.release_id).where(TrashEntry.state.in_({"PENDING","TRASHED","PURGING","PURGE_FAILED","PURGED"}))))
    # Register identities only while original files are still available; never guess missing references.
    for release in releases:
        if release.id in trashed:
            continue
        for track in release.tracks:
            if not track.content_hash:
                identity = flac_frame_hash(Path(track.path))
                if identity:
                    with SessionLocal() as session:
                        saved = session.get(Track, track.id)
                        if saved and saved.path == track.path and not saved.content_hash:
                            saved.content_hash = track.content_hash = identity
                            session.commit()
    index, processed, album_counts = {}, 0, {}
    settings = workflow_settings()
    excluded = (Path(settings["trash"]).resolve(),) if settings["trash"] else ()
    for album in discover_releases(root, excluded_roots=excluded, limit=None):
        if (album / ".sonic-sentry-transfer").exists():
            continue
        for path in album_files(album):
            if path.suffix.lower() != ".flac":
                continue
            identity = flac_frame_hash(path)
            album_counts[str(album)] = album_counts.get(str(album),0) + 1
            processed += 1
            if processed > 100000:
                raise FileOperationError("More than 100000 FLAC files; select a smaller scan folder")
            if identity:
                index.setdefault(identity, []).append((str(album),str(path)))
            if processed % 25 == 0:
                with SessionLocal() as session:
                    session.get(ReconcileScan,scan_id).processed = processed
                    session.commit()
    results = []
    occupied = {release.source_path:release.id for release in releases}
    for release in releases:
        if release.id in trashed or not release.tracks or all(Path(t.path).is_file() for t in release.tracks):
            continue
        row = {"id":release.id,"title":release.title,"old":release.source_path,"state":"NO_REFERENCE","mapping":[]}
        if all(track.content_hash for track in release.tracks):
            matches = [index.get(track.content_hash, []) for track in release.tracks]
            row["state"] = "NOT_FOUND"
            if all(matches):
                row["state"] = "AMBIGUOUS"
                if all(len(items)==1 for items in matches):
                    albums = {items[0][0] for items in matches}
                    paths = {items[0][1] for items in matches}
                    if len(albums)==1 and len(paths)==len(release.tracks):
                        destination = next(iter(albums))
                        if occupied.get(destination, release.id) == release.id and album_counts.get(destination)==len(release.tracks):
                            row.update(state="FOUND",destination=destination,mapping=[
                                {"id":track.id,"old":track.path,"new":items[0][1],"hash":track.content_hash}
                                for track,items in zip(release.tracks,matches)])
        results.append(row)
    with SessionLocal() as session:
        session.get(ReconcileScan,scan_id).processed = processed
        session.commit()
    return results


def apply_proposal(scan_id, release_id, actor):
    with process_lock("file-workflow"):
        with SessionLocal() as session:
            scan = session.get(ReconcileScan, scan_id)
            if not scan or scan.state != "COMPLETE" or scan.root not in allowed_roots():
                raise FileOperationError("Scan is incomplete or its root is no longer configured")
            proposal = next((row for row in json.loads(scan.results_json) if row["id"] == release_id and row["state"] == "FOUND"),None)
            if not proposal:
                raise FileOperationError("No unique proposal is available for this album")
            root = Path(scan.root).resolve()
            destination = Path(proposal["destination"]).resolve()
            if not (destination == root or is_within(destination,(root,))):
                raise FileOperationError("Proposed path is outside the scan root")
        # Slow verification before acquiring the writer lock, while all app file actions are excluded.
        verified_stats={}
        for item in proposal["mapping"]:
            new = Path(item["new"])
            if not is_within(new.resolve(),(destination,)) or (Path(item["old"]).exists() and Path(item["old"]).resolve() != new.resolve()) or flac_frame_hash(new) != item["hash"]:
                raise FileOperationError("Files changed or originals are still present; scan again")
            stat=new.stat()
            verified_stats[str(new)]=(stat.st_size,stat.st_mtime_ns)
        with SessionLocal() as session:
            lock_file_operations(session)
            for item in proposal["mapping"]:
                new=Path(item["new"])
                stat=new.stat()
                if (stat.st_size,stat.st_mtime_ns)!=verified_stats[str(new)] or (Path(item["old"]).exists() and Path(item["old"]).resolve()!=new.resolve()):
                    raise FileOperationError("Files changed during verification; scan again")
            release = session.scalar(select(Release).where(Release.id==release_id).options(selectinload(Release.tracks)))
            if not release or release.source_path != proposal["old"] or {t.id for t in release.tracks} != {v["id"] for v in proposal["mapping"]}:
                raise FileOperationError("Release changed; scan again")
            check_pending_transfer(session,release_id)
            check_active_jobs(session,Path(release.source_path))
            check_active_jobs(session,destination)
            if session.scalar(select(Release.id).where(Release.source_path==str(destination),Release.id!=release_id)):
                raise FileOperationError("Destination is already registered")
            tracks = {track.id:track for track in release.tracks}
            for item in proposal["mapping"]:
                track=tracks[item["id"]]
                if track.path != item["old"] or track.content_hash != item["hash"]:
                    raise FileOperationError("Track references changed; scan again")
                track.path=item["new"]
                track.display_path=str(Path(item["new"]).relative_to(destination))
            old=release.source_path
            release.source_path=str(destination)
            session.add(ReleaseOperation(release_id=release_id,action="RECONCILE_PATH",source_path=old,destination_path=str(destination),created_at=utc_now(),actor=actor))
            session.commit()
