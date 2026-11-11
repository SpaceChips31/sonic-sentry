"""Explicit admin reset of analysis/history metadata; no audio or filesystem deletion."""
from contextlib import ExitStack
from sqlalchemy import select,func,delete
from app.database import SessionLocal
from app.models import (Release,Track,AnalysisJob,ReleaseOperation,FileTransfer,SourceObservation,
    AnalyzedContent,TrashEntry,ReviewEvent,FileTask,FileBatch,ReconcileScan,AnalysisSource)
from app.services.file_workflow import lock_file_operations
from app.services.locks import process_lock

TABLES=(ReviewEvent,ReleaseOperation,Track,FileTask,FileBatch,SourceObservation,AnalyzedContent,
    AnalysisJob,FileTransfer,ReconcileScan,TrashEntry,Release)


def _preview(session):
    counts={model.__tablename__:session.scalar(select(func.count()).select_from(model)) for model in TABLES}
    blockers={
        "active_analyses":session.scalar(select(func.count()).select_from(AnalysisJob).where(AnalysisJob.status.in_({"ANALYZING","ROUTING"}))),
        "unfinished_transfers":session.scalar(select(func.count()).select_from(FileTransfer).where(FileTransfer.state.not_in({"COMPLETE","ABANDONED"}))),
        "running_file_tasks":session.scalar(select(func.count()).select_from(FileTask).where(FileTask.state=="RUNNING")),
        "running_scans":session.scalar(select(func.count()).select_from(ReconcileScan).where(ReconcileScan.state=="RUNNING")),
        "recoverable_trash":session.scalar(select(func.count()).select_from(TrashEntry).where(TrashEntry.state.not_in({"RESTORED","PURGED","CANCELLED"}))),
    }
    return {"counts":counts,"blockers":{key:value for key,value in blockers.items() if value},
        "total":sum(counts.values())}


def reset_preview():
    with SessionLocal() as session:
        return _preview(session)


def reset_history():
    # Lock order matches scanner -> DB and file workflow -> DB. No worker/Web shutdown needed.
    with ExitStack() as stack:
        stack.enter_context(process_lock("scanner"))
        stack.enter_context(process_lock("file-workflow"))
        with SessionLocal() as session:
            lock_file_operations(session)
            preview=_preview(session)
            if preview["blockers"]:
                raise ValueError("Finish/cancel active work and resolve unfinished transfers or recoverable trash before resetting history")
            for source in session.scalars(select(AnalysisSource)):
                source.watch_enabled=False
                source.next_scan_at=source.last_scan_at=source.watch_error=None
            for model in TABLES:
                session.execute(delete(model))
            session.commit()
    return preview["total"]
