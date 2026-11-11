"""Preview-first cleanup of recognized, unreferenced application artifacts."""
import hashlib
import json
import re
import time
from pathlib import Path

from sqlalchemy import select

from app.config import REPORT_ROOT, UPLOAD_ROOT
from app.database import SessionLocal, DATA_DIR
from app.models import AnalysisJob, Release, Track, FileTransfer, TrashEntry, AnalysisSource, FileTask
from app.services.files import MARKER, snapshot, album_files
from app.services.file_workflow import FileOperationError, lock_file_operations
from app.services.settings import workflow_settings
from app.services.locks import process_lock


def overlaps(path: Path, other: Path) -> bool:
    return path.is_relative_to(other) or other.is_relative_to(path)


def cleanup_preview(*, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    settings = workflow_settings()
    with SessionLocal() as session:
        sources = [Path(p).resolve() for p in session.scalars(select(AnalysisSource.path))]
        protected = [Path(p).resolve() for p in session.scalars(select(Release.source_path))]
        protected += [Path(p).resolve() for p in session.scalars(select(Track.path))]
        protected += [Path(p).resolve() for p in session.scalars(select(FileTask.source_path))]
        protected += [Path(p).resolve() for p in session.scalars(select(FileTask.destination_path))]
        jobs = list(session.scalars(select(AnalysisJob)))
        protected += [Path(job.source_path).resolve() for job in jobs]
        for transfer in session.scalars(select(FileTransfer)):
            protected.extend(Path(p).resolve() for p in (transfer.source_path, transfer.destination_path, transfer.temporary_path))
        protected += [Path(p).resolve() for p in session.scalars(select(TrashEntry.trash_path))]
        graphs = set()
        for raw in session.scalars(select(Track.forensic_json).where(Track.forensic_json.is_not(None))):
            try:
                path = json.loads(raw).get("result", {}).get("spectrogram_path")
                if path:
                    graphs.add(Path(path).resolve())
            except (ValueError, TypeError, AttributeError):
                continue
    jobs_by_id = {job.id: job for job in jobs}
    results = []
    reports, uploads = REPORT_ROOT.resolve(), UPLOAD_ROOT.resolve()
    media = sources + [Path(p).resolve() for p in settings["movable_roots"]]
    media += [Path(settings[k]).resolve() for k in ("staging", "quarantine", "rejected", "trash") if settings[k]]

    def safe_root(root):
        return root.is_dir() and root != DATA_DIR.resolve() and not DATA_DIR.resolve().is_relative_to(root) and not any(overlaps(root, p) for p in media)

    def add(path, category, files):
        stats = [(str(p.relative_to(path) if path.is_dir() else p.name), p.stat().st_size, p.stat().st_mtime_ns) for p in files]
        token = hashlib.sha256(json.dumps([str(path), stats], sort_keys=True).encode()).hexdigest()
        results.append({"token": token, "path": str(path), "category": category,
                        "bytes": sum(row[1] for row in stats), "stats": stats})

    if reports != uploads and safe_root(reports):
        for path in reports.iterdir():
            if path.is_symlink():
                continue
            if path.is_file() and path.stat().st_mtime < now - settings["report_days"] * 86400:
                match = re.fullmatch(r"job-(\d+)\.(json|progress\.json|progress\.tmp)", path.name)
                imported = re.fullmatch(r"[0-9a-f]{32}-.+\.json", path.name)
                job = jobs_by_id.get(int(match[1])) if match else None
                if imported or match and job and job.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                    add(path, "Report", [path])
            elif path.is_dir() and re.fullmatch(r"job-\d+-spectrograms", path.name):
                job_id = int(path.name.split("-")[1])
                job = jobs_by_id.get(job_id)
                if job and job.status not in {"COMPLETED", "FAILED", "CANCELLED"}:
                    continue
                for image in path.iterdir():
                    if image.is_file() and not image.is_symlink() and re.fullmatch(r"[0-9a-f]{24}\.png", image.name) and image.resolve() not in graphs and image.stat().st_mtime < now - settings["report_days"] * 86400:
                        add(image, "Unused spectrogram", [image])
    if reports != uploads and safe_root(uploads):
        for path in uploads.iterdir():
            if not path.is_dir() or path.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", path.name):
                continue
            if any(overlaps(path.resolve(), other) for other in protected):
                continue
            if any(part.name == MARKER or part.name.startswith(".sonic-sentry-") for part in path.rglob("*")):
                continue
            try:
                files = list(album_files(path))
                newest = max([path.stat().st_mtime] + [p.stat().st_mtime for p in files])
                if newest < now - settings["upload_days"] * 86400:
                    add(path, "Unreferenced upload (may contain audio)", files)
            except (OSError, ValueError):
                continue
    return results


def execute_cleanup(tokens: list[str]) -> tuple[int, int]:
    if workflow_settings()["locked"]:
        raise FileOperationError("File operations are locked by the deployment")
    if not tokens or len(tokens) > 500:
        raise FileOperationError("Select between 1 and 500 cleanup candidates")
    with process_lock("file-workflow"), SessionLocal() as session:
        lock_file_operations(session)
        current = {row["token"]: row for row in cleanup_preview()}
        if any(token not in current for token in tokens):
            raise FileOperationError("Cleanup preview changed; reload it before deleting anything")
        count, size = 0, 0
        for token in dict.fromkeys(tokens):
            row = current[token]
            path = Path(row["path"])
            if path.is_dir():
                for name, length, modified in row["stats"]:
                    file = path / name
                    stat = file.stat()
                    if stat.st_size != length or stat.st_mtime_ns != modified or file.is_symlink():
                        raise FileOperationError("Upload changed during cleanup; remaining files were preserved")
                    file.unlink()
                # rmdir refuses new files; it never recursively removes unexpected content.
                for folder in sorted((p for p in path.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                    folder.rmdir()
                path.rmdir()
            else:
                stat = path.stat()
                if path.is_symlink() or stat.st_size != row["stats"][0][1] or stat.st_mtime_ns != row["stats"][0][2]:
                    raise FileOperationError("Artifact changed during cleanup")
                path.unlink()
            count += 1
            size += row["bytes"]
        return count, size
