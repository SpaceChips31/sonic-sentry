import json
import os
import signal
import subprocess
import sys
import time
import threading
import errno
from pathlib import Path

from sqlalchemy import select, update, or_

from app.config import PROJECT_ROOT, REPORT_ROOT
from app.database import SessionLocal, engine
from app.migrations import initialize_database
from app.health import Heartbeat
from app.models import AnalysisJob, AnalyzedContent, FileTransfer, ReconcileScan
from app.services.importer import import_report
from app.services.file_workflow import FileOperationError, auto_route_release, recover_transfers, utc_now, lock_file_operations
from app.services.files import manifest_audio_hash, same_content, snapshot
from app.services.ingestion import scan_sources
from app.services.locks import process_lock
from app.services.settings import workflow_settings

VALIDATOR = PROJECT_ROOT / "validator" / "validate_release.py"
EXPECTED_EXIT_CODES = {0, 10, 20}
STOP = threading.Event()
HEARTBEAT = None


def phase(name, **values):
    if HEARTBEAT:
        HEARTBEAT.update(phase=name, **values)


class WorkerStopping(Exception):
    pass


class AnalysisTimeout(TimeoutError):
    pass


class AnalysisCancelled(Exception):
    pass


def recover_interrupted_jobs() -> int:
    with SessionLocal() as session:
        jobs = list(session.scalars(select(AnalysisJob).where(AnalysisJob.status == "ANALYZING")))
        for job in jobs:
            job.status = "CANCELLED" if job.cancel_requested else "QUEUED"
            job.error = "Recovered after worker restart"
            job.progress = 0
        routing = list(session.scalars(select(AnalysisJob).where(AnalysisJob.status == "ROUTING")))
        session.commit()
    for job in routing:
        error = None
        try:
            with SessionLocal() as session:
                transfer = session.scalar(select(FileTransfer).where(FileTransfer.job_id == job.id).order_by(FileTransfer.created_at.desc()))
            if transfer is None:
                auto_route_release(job.release_id, current_job_id=job.id, policy=job.routing_policy)
            elif transfer.state != "COMPLETE":
                error = f"Analysis completed; file transfer needs attention: {transfer.error or transfer.state}"
        except (FileOperationError, OSError) as exc:
            error = f"Analysis completed; automatic routing needs attention: {exc}"
        finish_job(job.id, "COMPLETED", error)
    return len(jobs) + len(routing)


def claim_job():
    with SessionLocal() as session:
        job = session.scalar(select(AnalysisJob).where(AnalysisJob.status == "QUEUED", or_(AnalysisJob.next_run_at.is_(None), AnalysisJob.next_run_at <= time.time())).order_by(AnalysisJob.id).limit(1))
        if job is None:
            return None
        result = session.execute(update(AnalysisJob).where(AnalysisJob.id == job.id, AnalysisJob.status == "QUEUED").values(
            status="ANALYZING", error=None, progress=0, progress_total=0, started_at=utc_now(), finished_at=None,
            attempts=AnalysisJob.attempts + 1, next_run_at=None))
        session.commit()
        if result.rowcount != 1:
            return None
        return {"id": job.id, "source_path": job.source_path}


def finish_job(job_id: int, status: str, error: str | None = None):
    with SessionLocal() as session:
        job = session.get(AnalysisJob, job_id)
        if job is not None:
            job.status, job.error, job.finished_at = status, error, utc_now()
            session.commit()


def cancellation_requested(job_id: int) -> bool:
    with SessionLocal() as session:
        job = session.get(AnalysisJob, job_id)
        return job is None or job.cancel_requested


def stop_validator(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.communicate()
            return
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.communicate()


def run_validator(job_id: int, source: Path, report: Path):
    progress_file = report.with_suffix(".progress.json")
    progress_file.unlink(missing_ok=True)
    options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    proc = subprocess.Popen([sys.executable, str(VALIDATOR), "--report", str(report),
                             "--progress", str(progress_file), str(source)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **options)
    last_progress = None
    limits = workflow_settings()
    started = progressed = time.monotonic()
    phase("VALIDATING", job_id=job_id, last_progress=time.time(), stall_timeout=limits["stall_timeout"], deadline=time.time() + limits["analysis_timeout"])
    try:
        while True:
            if STOP.is_set():
                raise WorkerStopping()
            if time.monotonic() - started > limits["analysis_timeout"]:
                raise AnalysisTimeout("Analysis exceeded its total time limit")
            if time.monotonic() - progressed > limits["stall_timeout"]:
                raise AnalysisTimeout("Analysis stopped reporting track progress")
            if cancellation_requested(job_id):
                raise AnalysisCancelled()
            try:
                values = json.loads(progress_file.read_text(encoding="utf-8"))
                progress = (int(values["completed"]), int(values["total"]))
                if progress != last_progress:
                    with SessionLocal() as session:
                        session.execute(update(AnalysisJob).where(AnalysisJob.id == job_id).values(
                            progress=progress[0], progress_total=progress[1]))
                        session.commit()
                    last_progress = progress
                    progressed = time.monotonic()
                    if HEARTBEAT:
                        HEARTBEAT.update(last_progress=time.time())
            except (OSError, ValueError, KeyError):
                pass
            try:
                output, _ = proc.communicate(timeout=1)
                return subprocess.CompletedProcess(proc.args, proc.returncode, stdout=output)
            except subprocess.TimeoutExpired:
                continue
    finally:
        stop_validator(proc)
        progress_file.unlink(missing_ok=True)


def process_job(job):
    job_id, source = job["id"], Path(job["source_path"])
    report = REPORT_ROOT / f"job-{job_id}.json"
    try:
        if cancellation_requested(job_id):
            raise AnalysisCancelled()
        phase("PREPARING", job_id=job_id)
        before = snapshot(source, hashes=True)
        report.unlink(missing_ok=True)
        proc = run_validator(job_id, source, report)
        print(proc.stdout, flush=True)
        if proc.returncode not in EXPECTED_EXIT_CODES:
            raise RuntimeError(f"validator exited with code {proc.returncode}")
        if not report.is_file():
            raise RuntimeError("validator did not create a report")
        phase("VERIFYING", job_id=job_id)
        after = snapshot(source, hashes=True)
        if not same_content(after, before):
            changed = sorted(name for name in before.keys() | after.keys()
                             if name not in before or name not in after or before[name]["sha256"] != after[name]["sha256"])
            raise RuntimeError("Album changed during analysis; files were not routed. Retry once the download is complete. "
                               + ", ".join(changed[:10]))
        digest = manifest_audio_hash(after)
        if cancellation_requested(job_id):
            raise AnalysisCancelled()
        release = import_report(report)
        with SessionLocal() as session:
            lock_file_operations(session)
            current = session.get(AnalysisJob, job_id)
            current.release_id = release.id
            if current.cancel_requested:
                session.commit()
                raise AnalysisCancelled()
            current.status = "ROUTING"
            current.progress = current.progress_total = release.total_tracks
            if session.get(AnalyzedContent, digest) is None:
                session.add(AnalyzedContent(content_hash=digest, release_id=release.id))
            session.commit()
        phase("TRANSFERRING", job_id=job_id)
        routing_error = None
        try:
            auto_route_release(release.id, current_job_id=job_id, policy=current.routing_policy)
        except (FileOperationError, OSError) as exc:
            routing_error = f"Analysis completed; automatic routing needs attention: {exc}"
        finish_job(job_id, "COMPLETED", routing_error)
    except WorkerStopping:
        raise
    except AnalysisCancelled:
        finish_job(job_id, "CANCELLED")
    except Exception as exc:
        print(f"Job {job_id} failed: {exc}", flush=True)
        transient = isinstance(exc, AnalysisTimeout) or isinstance(exc, OSError) and exc.errno in {errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT}
        with SessionLocal() as session:
            current = session.get(AnalysisJob, job_id)
            if transient and current and max(current.attempts, 1) <= workflow_settings()["retry_limit"]:
                current.status, current.next_run_at = "QUEUED", time.time() + 60
                current.error = f"Temporary failure; retry scheduled in 60 seconds: {exc}"
                session.commit()
                return
        finish_job(job_id, "FAILED", str(exc))


def scan_loop():
    while not STOP.is_set():
        try:
            with process_lock("scanner"):
                scan_sources(respect_schedule=True)
            if HEARTBEAT:
                HEARTBEAT.update(scanner_error=None, last_scan=time.time())
        except Exception as exc:
            print(f"Source scan failed: {exc}", flush=True)
            if HEARTBEAT:
                HEARTBEAT.update(scanner_error=str(exc))
        from app.services.sources import next_scan_interval
        STOP.wait(next_scan_interval())


def main():
    global HEARTBEAT
    STOP.clear()
    def stopping(*args):
        STOP.set()
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    with process_lock("worker"), Heartbeat() as heartbeat:
        initialize_database(engine)
        HEARTBEAT = heartbeat
        phase("RECOVERING")
        recover_transfers()
        from app.services.trash import recover_purges
        recover_purges()
        recover_interrupted_jobs()
        from app.services.file_tasks import recover_tasks, process_next_task
        from app.services.reconciliation import process_next_scan
        recover_tasks()
        with SessionLocal() as session:
            for scan in session.scalars(select(ReconcileScan).where(ReconcileScan.state == "RUNNING")):
                scan.state = "QUEUED"
            session.commit()
        scanner = threading.Thread(target=scan_loop, daemon=True)
        scanner.start()
        try:
            while not STOP.is_set():
                phase("IDLE", job_id=None)
                phase("FILE_TASKS",job_id=None)
                handled = process_next_task()
                if not handled:
                    phase("RECONCILING",job_id=None)
                    handled = process_next_scan()
                phase("IDLE",job_id=None)
                job = claim_job()
                if job is None:
                    STOP.wait(0.1 if handled else 2)
                else:
                    process_job(job)
        except WorkerStopping:
            pass
        finally:
            STOP.set()
            scanner.join(timeout=5)
            HEARTBEAT = None


if __name__ == "__main__":
    main()
