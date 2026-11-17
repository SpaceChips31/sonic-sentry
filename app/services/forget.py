"""Scoped analysis reset. No physical files are moved, edited or deleted."""
import hmac
import json
from contextlib import ExitStack
from pathlib import Path
from sqlalchemy import select,delete,or_,and_
from sqlalchemy.orm import selectinload
from app import config
from app.database import SessionLocal
from app.models import (Release,Track,AnalysisJob,ReleaseOperation,FileTransfer,SourceObservation,
    AnalyzedContent,TrashEntry,ReviewEvent,FileTask,FileBatch,ReconcileScan,AnalysisSource)
from app.security import sign
from app.services.file_workflow import FileOperationError,check_pending_transfer,check_active_jobs,lock_file_operations
from app.services.files import contains_flac
from app.services.locks import process_lock
from app.services.queue import enqueue_paths_in_session
from app.services.settings import workflow_settings


def _selection(ids):
    if not ids or len(ids)>50 or any(value<=0 for value in ids):
        raise FileOperationError("Select between 1 and 50 valid albums")
    return sorted(set(ids))


def _jobs(session,release):
    return list(session.scalars(select(AnalysisJob).where(or_(AnalysisJob.release_id==release.id,
        and_(AnalysisJob.release_id.is_(None),AnalysisJob.source_path==release.source_path))).order_by(AnalysisJob.id)))


def _rescan_source(session,path):
    settings=workflow_settings()
    sources=list(session.scalars(select(AnalysisSource).where(AnalysisSource.enabled.is_(True))))
    matching=[source for source in sources if path.is_relative_to(Path(source.path).resolve())]
    matching.sort(key=lambda source:len(Path(source.path).parts),reverse=True)
    roots=[Path(value).resolve() for value in settings["movable_roots"]]
    roots += [Path(settings[key]).resolve() for key in ("staging","quarantine","rejected") if settings[key]]
    roots.append(config.UPLOAD_ROOT.resolve())
    if not matching and not any(path.is_relative_to(root) for root in roots):
        raise FileOperationError("Current album path is not an authorized analysis source")
    if not contains_flac(path):
        raise FileOperationError("Current folder contains no accessible FLAC files")
    return matching[0].id if matching else None


def _preview(session,ids):
    rows=[]
    for identifier in _selection(ids):
        release=session.scalar(select(Release).where(Release.id==identifier).options(selectinload(Release.tracks)))
        row={"id":identifier,"title":str(identifier),"path":"","eligible":False,"error":None,"can_rescan":False,"rescan_error":None}
        if not release:
            row["error"]="Release not found"
            rows.append(row)
            continue
        jobs=_jobs(session,release)
        row.update(title=release.title,path=release.source_path,tracks=len(release.tracks),jobs=len(jobs),
            version={"analysed_at":release.analysed_at,"status":release.status,
                "tracks":[[t.id,t.path,t.status,t.human_review,t.forensic_json,t.content_hash] for t in release.tracks],
                "jobs":[[j.id,j.status,j.hidden] for j in jobs]})
        try:
            check_pending_transfer(session,release.id)
            job_ids=[job.id for job in jobs]
            if session.scalar(select(FileTransfer.id).where(FileTransfer.job_id.in_(job_ids),
                    FileTransfer.state.not_in({"COMPLETE","ABANDONED"}))):
                raise FileOperationError("album has an unfinished transfer; inspect Activity")
            check_active_jobs(session,Path(release.source_path))
            if any(job.status in {"QUEUED","ANALYZING","ROUTING"} for job in jobs):
                raise FileOperationError("Album has queued or running analyses")
            if session.scalar(select(TrashEntry.id).where(TrashEntry.release_id==release.id,
                    TrashEntry.state.not_in({"RESTORED","PURGED","CANCELLED"}))):
                raise FileOperationError("Restore or explicitly purge recoverable trash before forgetting its analysis")
            row["eligible"]=True
            try:
                row["source_id"]=_rescan_source(session,Path(release.source_path).resolve())
                row["can_rescan"]=True
            except (OSError, ValueError, FileOperationError) as exc:
                row["rescan_error"]=str(exc)
        except FileOperationError as exc:
            row["error"]=str(exc)
        rows.append(row)
    eligible=[row for row in rows if row["eligible"]]
    for row in eligible:
        path=Path(row["path"]).resolve()
        if any(other["id"]!=row["id"] and (path.is_relative_to(Path(other["path"]).resolve()) or Path(other["path"]).resolve().is_relative_to(path)) for other in eligible):
            row["can_rescan"]=False
            row["rescan_error"]="Selected album paths overlap; analyze them separately"
    token=sign("forget:"+json.dumps(rows,sort_keys=True))
    return {"rows":rows,"eligible":len(eligible),"selected":len(rows),"token":token,
        "can_rescan":bool(eligible) and all(row["can_rescan"] for row in eligible)}


def forget_preview(ids):
    with SessionLocal() as session:
        return _preview(session,ids)


def forget_albums(ids,token,mode):
    if mode not in {"forget","rescan"} or len(token)!=64 or not token.isascii():
        raise FileOperationError("Invalid forget action or confirmation token")
    with ExitStack() as stack:
        stack.enter_context(process_lock("scanner"))
        stack.enter_context(process_lock("file-workflow"))
        with SessionLocal() as session:
            lock_file_operations(session)
            plan=_preview(session,ids)
            if not hmac.compare_digest(token,plan["token"]) or not plan["eligible"]:
                raise FileOperationError("Selection changed or albums are blocked; preview again")
            if mode=="rescan" and not plan["can_rescan"]:
                raise FileOperationError("Some albums cannot be queued from their current paths; forget only or correct the source first")
            chosen=[row for row in plan["rows"] if row["eligible"]]
            selected=[row["id"] for row in chosen]
            paths=[row["path"] for row in chosen]
            jobs=list(session.scalars(select(AnalysisJob.id).where(or_(AnalysisJob.release_id.in_(selected),
                and_(AnalysisJob.release_id.is_(None),AnalysisJob.source_path.in_(paths))))))
            hashes=list(session.scalars(select(AnalyzedContent.content_hash).where(AnalyzedContent.release_id.in_(selected))))
            # Clear only observations owned by this selection or unlinked duplicate observations.
            session.execute(delete(SourceObservation).where(or_(SourceObservation.job_id.in_(jobs),
                and_(SourceObservation.job_id.is_(None),or_(SourceObservation.path.in_(paths),SourceObservation.content_hash.in_(hashes))))))
            batches=list(session.scalars(select(FileTask.batch_id).where(FileTask.release_id.in_(selected),FileTask.batch_id.is_not(None))))
            session.execute(delete(FileTransfer).where(FileTransfer.job_id.in_(jobs)))
            for model in (ReviewEvent,ReleaseOperation,FileTask,FileTransfer,TrashEntry,AnalyzedContent,Track):
                session.execute(delete(model).where(model.release_id.in_(selected)))
            session.execute(delete(AnalysisJob).where(AnalysisJob.id.in_(jobs)))
            session.execute(delete(Release).where(Release.id.in_(selected)))
            for batch_id in set(batches):
                if not session.scalar(select(FileTask.id).where(FileTask.batch_id==batch_id)):
                    session.execute(delete(FileBatch).where(FileBatch.id==batch_id))
            # Remove obsolete proposals so reused SQLite IDs cannot reconnect forgotten results.
            for scan in session.scalars(select(ReconcileScan)):
                try:
                    results=json.loads(scan.results_json)
                except (ValueError,TypeError):
                    continue
                if isinstance(results,list):
                    filtered=[row for row in results if not isinstance(row,dict) or row.get("id") not in selected]
                    if filtered!=results:
                        scan.results_json=json.dumps(filtered)
            queued=[]
            if mode=="rescan":
                for row in chosen:
                    created=enqueue_paths_in_session(session,[Path(row["path"])],"FORGET_RESCAN",source_id=row.get("source_id"))
                    if len(created)!=1:
                        raise FileOperationError("Album became unavailable for analysis; no records were forgotten")
                    queued+=created
            session.commit()
            return {"forgotten":len(chosen),"queued":len(queued)}
