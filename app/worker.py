import subprocess
import time
from pathlib import Path

from sqlalchemy import select, update

from app.config import REPORT_ROOT
from app.database import Base, SessionLocal, engine
from app.models import AnalysisJob
from app.services.importer import import_report
from app.services.file_workflow import auto_route_release

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
            .where(AnalysisJob.status == "QUEUED")
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
    recovered = recover_interrupted_jobs()
    print(
        f"SonicSentry worker started; recovered {recovered} job(s)",
        flush=True,
    )

    while True:
        job = claim_job()

        if job is None:
            time.sleep(2)
            continue

        process_job(job)


if __name__ == "__main__":
    main()
