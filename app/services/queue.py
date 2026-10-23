from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AnalysisJob, FileTransfer
from app.services.file_workflow import lock_file_operations, utc_now


def enqueue_paths(paths: list[Path], source_type: str) -> list[int]:
    """One transaction for every producer, including overlapping parent/child jobs."""
    created = []
    with SessionLocal() as session:
        lock_file_operations(session)
        busy = [Path(path) for path in session.scalars(select(AnalysisJob.source_path).where(
            AnalysisJob.status.in_({"QUEUED", "ANALYZING", "ROUTING"})))]
        for transfer in session.scalars(select(FileTransfer).where(FileTransfer.state.not_in({"COMPLETE", "ABANDONED"}))):
            busy.extend((Path(transfer.source_path), Path(transfer.destination_path)))
        for path in paths:
            path = path.resolve()
            if not path.is_dir() or any(path.is_relative_to(other) or other.is_relative_to(path) for other in busy):
                continue
            job = AnalysisJob(source_type=source_type, source_path=str(path), display_name=path.name,
                              status="QUEUED", created_at=utc_now())
            session.add(job)
            session.flush()
            created.append(job.id)
            busy.append(path)
        session.commit()
    return created
