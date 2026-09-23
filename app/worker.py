import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import or_, select, update

from app.config import REPORT_ROOT
from app.database import Base, SessionLocal, engine, migrate_schema
from app.models import AnalysisBatch, AnalysisJob
from app.services.importer import import_report
from app.services.file_workflow import auto_route_release
from app.services.runtime_settings import int_value

VALIDATOR = Path("/app/validator/validate_release.py")
EXPECTED_EXIT_CODES = {0, 10, 20}


def recover_interrupted_jobs() -> int:
    """Return jobs interrupted by a previous worker shutdown to the queue."""
    with SessionLocal() as session:
        result = session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.status == "ANALYZING")
            .values(
                status="QUEUED",
                error="Recovered after worker restart",
            )
        )
        session.commit()
        return int(result.rowcount or 0)


def claim_job():
    with SessionLocal() as session:
        job = session.scalar(
            select(AnalysisJob)
            .outerjoin(AnalysisBatch, AnalysisJob.batch_id == AnalysisBatch.id)
            .where(AnalysisJob.status == "QUEUED")
            .where(or_(AnalysisJob.batch_id.is_(None), AnalysisBatch.status == "ACTIVE"))
            .order_by(AnalysisJob.id)
            .limit(1)
        )

        if job is None:
            return None

        job.status = "ANALYZING"
        job.error = None
        session.commit()

        return {
            "id": job.id,
            "source_path": job.source_path,
        }


def finish_job(job_id: int, status: str, error: str | None = None):
    with SessionLocal() as session:
        job = session.get(AnalysisJob, job_id)

        if job is not None:
            job.status = status
            job.error = error
            batch_id = job.batch_id
            session.commit()
            if batch_id is not None:
                remaining = session.scalar(
                    select(AnalysisJob.id)
                    .where(
                        AnalysisJob.batch_id == batch_id,
                        AnalysisJob.status.in_({"QUEUED", "ANALYZING"}),
                    )
                    .limit(1)
                )
                batch = session.get(AnalysisBatch, batch_id)
                if batch is not None and remaining is None and batch.status != "CANCELLED":
                    batch.status = "COMPLETED"
                    session.commit()


def process_job(job):
    job_id = job["id"]
    source = Path(job["source_path"])
    report = REPORT_ROOT / f"job-{job_id}.json"

    try:
        proc = subprocess.run(
            [
                "python",
                str(VALIDATOR),
                "--report",
                str(report),
                str(source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        print(proc.stdout, flush=True)

        if proc.returncode not in EXPECTED_EXIT_CODES:
            raise RuntimeError(
                f"validator exited with code {proc.returncode}"
            )

        if not report.is_file():
            raise RuntimeError("validator did not create a report")

        release = import_report(report)

        destination = auto_route_release(release.id)

        print(
            f"Job {job_id} completed: "
            f"release #{release.id} [{release.status}]"
            + (
                f" -> {destination}"
                if destination is not None
                else ""
            ),
            flush=True,
        )

        finish_job(job_id, "COMPLETED")

    except Exception as exc:
        print(f"Job {job_id} failed: {exc}", flush=True)
        finish_job(job_id, "FAILED", str(exc))


def main():
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    recovered = recover_interrupted_jobs()
    print(
        f"SonicSentry worker started; recovered {recovered} job(s)",
        flush=True,
    )

    concurrency = int_value("worker_concurrency")
    print(f"Worker concurrency: {concurrency}", flush=True)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = set()
        while True:
            finished = {future for future in futures if future.done()}
            for future in finished:
                future.result()
            futures -= finished

            while len(futures) < concurrency:
                job = claim_job()
                if job is None:
                    break
                futures.add(executor.submit(process_job, job))

            time.sleep(0.5 if futures else 2)


if __name__ == "__main__":
    main()
