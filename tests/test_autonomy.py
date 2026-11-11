import json
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from client import TestClient
from app import main, config, worker
from app.models import (FileTask,FileBatch,FileTransfer,AnalysisSource,AnalysisJob,Track,Release,
    ReleaseOperation,ReconcileScan)
from app.services import (file_tasks,file_workflow,queue,importer,reconciliation,sources,ingestion)
from app.services.files import flac_frame_hash
from test_workflow import configure,report
from test_staging_filters import quarantine


def encoded(frames=b"audio frames",tag=b"tags"):
    info=b"x"*10+b"properties"[:8]+b"x"*16
    return b"fLaC"+b"\x00"+(34).to_bytes(3,"big")+info+b"\x84"+len(tag).to_bytes(3,"big")+tag+frames


def referenced_release(env):
    path=report(env,"QUARANTINE")
    (env.inbox/"Album"/"track.flac").write_bytes(encoded())
    return importer.import_report(path)


def test_preflight_does_not_approve_or_move_and_signed_confirmation_queues_only(setup):
    configure(setup)
    album=quarantine(setup,"One")
    plan=file_tasks.preflight([album.id],"approve")
    assert plan["ready"] and plan["required"]==2*len(b"test audio")
    with setup.db() as session:
        assert session.get(Release,album.id).tracks[0].human_review=="NONE"
    with TestClient(main.app) as client:
        assert client.post("/file-batches",data={"release_ids":[album.id],"mode":"approve","token":"forged"}).status_code==409
        response=client.post("/file-batches",data={"release_ids":[album.id],"mode":"approve","token":plan["token"]},follow_redirects=False)
        assert response.status_code==303
        assert client.get(response.headers["location"]).status_code==200
    assert (setup.root/"quarantine"/"One"/"track.flac").exists()
    assert not (setup.root/"staging"/"One").exists()
    assert queue.enqueue_paths([setup.root/"quarantine"/"One"],"TEST")==[]
    assert file_tasks.process_next_task()
    with setup.db() as session:
        task=session.scalar(select(FileTask))
        transfer=session.get(FileTransfer,task.transfer_id)
        assert task.state=="COMPLETE" and transfer.progress_bytes==transfer.total_bytes>0
        assert session.get(Release,album.id).tracks[0].human_review=="APPROVED"


def test_preflight_detects_collision_and_insufficient_space(setup,monkeypatch):
    configure(setup)
    album=quarantine(setup,"One")
    monkeypatch.setattr(file_tasks.shutil,"disk_usage",lambda _:SimpleNamespace(free=0))
    assert not file_tasks.preflight([album.id],"approve")["ready"]
    (setup.root/"staging"/"One").mkdir()
    plan=file_tasks.preflight([album.id],"approve")
    assert not plan["rows"][0]["ok"]
    assert "exists" in plan["rows"][0]["error"]


def test_worker_rejects_album_changed_after_confirmation(setup):
    configure(setup)
    album=quarantine(setup,"One")
    plan=file_tasks.preflight([album.id],"approve")
    file_tasks.enqueue_batch([album.id],"approve",plan["token"],"tester")
    audio=setup.root/"quarantine"/"One"/"track.flac"
    audio.write_bytes(b"new audio")
    assert file_tasks.process_next_task()
    with setup.db() as session:
        assert session.scalar(select(FileTask)).state=="FAILED"
        assert session.get(Release,album.id).tracks[0].human_review=="NONE"
    assert audio.read_bytes()==b"new audio"


def test_running_task_recovers_exactly_once_after_journal_completion(setup,monkeypatch):
    configure(setup)
    album=quarantine(setup,"One")
    plan=file_tasks.preflight([album.id],"approve")
    file_tasks.enqueue_batch([album.id],"approve",plan["token"],"tester")
    original=file_tasks.move_release
    def interrupted(*args,**kwargs):
        original(*args,**kwargs)
        raise SystemExit("power loss before task status update")
    monkeypatch.setattr(file_tasks,"move_release",interrupted)
    with pytest.raises(SystemExit):
        file_tasks.process_next_task()
    monkeypatch.setattr(file_tasks,"move_release",original)
    file_tasks.recover_tasks()
    assert file_tasks.process_next_task()
    with setup.db() as session:
        assert session.scalar(select(FileTask)).state=="COMPLETE"
        assert len(list(session.scalars(select(ReleaseOperation).where(ReleaseOperation.action=="MANUAL_PASS_TO_STAGING"))))==1


def test_queued_task_can_be_cancelled_without_files_or_approvals(setup):
    configure(setup)
    album=quarantine(setup,"One")
    plan=file_tasks.preflight([album.id],"approve")
    identifier=file_tasks.enqueue_batch([album.id],"approve",plan["token"],"tester")
    with setup.db() as session:
        task=session.scalar(select(FileTask))
    with TestClient(main.app) as client:
        assert client.post(f"/file-tasks/{task.id}/cancel",follow_redirects=False).status_code==303
    assert not file_tasks.process_next_task()
    assert (setup.root/"quarantine"/"One"/"track.flac").exists()


def test_metadata_independent_identity_detects_changed_audio(setup):
    audio=setup.inbox/"test.flac"
    audio.write_bytes(encoded(tag=b"original"))
    identity=flac_frame_hash(audio)
    audio.write_bytes(encoded(tag=b"new tags and cover"))
    assert flac_frame_hash(audio)==identity
    audio.write_bytes(encoded(frames=b"different audio",tag=b"original"))
    assert flac_frame_hash(audio)!=identity
    audio.write_bytes(b"fLaC\x80\x00\x00\x22truncated")
    assert flac_frame_hash(audio) is None


def test_reconciliation_confirms_only_database_paths_and_preserves_review(setup):
    configure(setup)
    album=referenced_release(setup)
    with setup.db() as session:
        session.get(Release,album.id).tracks[0].human_review="APPROVED"
        session.commit()
    old=setup.inbox/"Album"
    new=setup.root/"staging"/"Tagged"
    old.rename(new)
    (new/"track.flac").rename(new/"01 renamed.FLAC")
    (new/"01 renamed.FLAC").write_bytes(encoded(tag=b"Picard tags"))
    identifier=reconciliation.enqueue_scan(str(setup.root/"staging"),"tester")
    assert reconciliation.process_next_scan()
    with setup.db() as session:
        scan=session.get(ReconcileScan,identifier)
        assert json.loads(scan.results_json)[0]["state"]=="FOUND"
    reconciliation.apply_proposal(identifier,album.id,"operator")
    with setup.db() as session:
        saved=session.get(Release,album.id)
        assert saved.source_path==str(new) and saved.tracks[0].display_path=="01 renamed.FLAC"
        assert saved.tracks[0].human_review=="APPROVED"
        assert saved.operations[0].actor=="operator"
    assert (new/"01 renamed.FLAC").read_bytes()==encoded(tag=b"Picard tags")


def test_reconciliation_refuses_ambiguous_copies_and_changes_after_scan(setup):
    configure(setup)
    album=referenced_release(setup)
    audio=(setup.inbox/"Album"/"track.flac").read_bytes()
    (setup.inbox/"Album"/"track.flac").unlink()
    for name in ("One","Two"):
        target=setup.root/"staging"/name
        target.mkdir()
        (target/"track.flac").write_bytes(audio)
    identifier=reconciliation.enqueue_scan(str(setup.root/"staging"),"tester")
    reconciliation.process_next_scan()
    with setup.db() as session:
        assert json.loads(session.get(ReconcileScan,identifier).results_json)[0]["state"]=="AMBIGUOUS"
    with pytest.raises(file_workflow.FileOperationError):
        reconciliation.apply_proposal(identifier,album.id,"tester")
    (setup.root/"staging"/"Two"/"track.flac").unlink()
    identifier=reconciliation.enqueue_scan(str(setup.root/"staging"),"tester")
    reconciliation.process_next_scan()
    (setup.root/"staging"/"One"/"track.flac").write_bytes(encoded(frames=b"changed"))
    with pytest.raises(file_workflow.FileOperationError,match="changed"):
        reconciliation.apply_proposal(identifier,album.id,"tester")


def test_source_policy_is_snapshotted_and_schedule_respects_override(setup):
    configure(setup)
    report(setup)
    with setup.db() as session:
        source=AnalysisSource(name="Inbox",path=str(setup.inbox),kind="FILESYSTEM",enabled=True,watch_enabled=True)
        session.add(source)
        session.commit()
    sources.save_source_policy(source.id,10,30,"pass")
    now=time.time()+1
    assert ingestion.scan_sources(now=now,respect_schedule=True)==0
    assert ingestion.scan_sources(now=now+5,respect_schedule=True)==0
    assert ingestion.scan_sources(now=now+31,respect_schedule=True)==1
    sources.save_source_policy(source.id,None,None,"manual")
    with setup.db() as session:
        job=session.scalar(select(AnalysisJob))
        assert job.source_id==source.id and job.routing_policy=="pass"
    with pytest.raises(ValueError):
        sources.save_source_policy(source.id,1,30,"all")


def test_review_reason_filter_summary_and_compare_are_scoped(setup):
    path=report(setup,"QUARANTINE")
    payload=json.loads(path.read_text())
    payload["files"][0]["forensics"]={"result":{"authenticity":{"spectral":{"cutoff_hz":16000,"verdict_label":"SUSPICIOUS"}}}}
    path.write_text(json.dumps(payload))
    album=importer.import_report(path)
    with setup.db() as session:
        assert session.get(Release,album.id).tracks[0].review_reason=="cutoff"
    with TestClient(main.app) as client:
        assert "Album" in client.get("/releases?reason=cutoff").text
        assert f'/releases/{album.id}\"' not in client.get("/releases?reason=integrity").text
        assert client.get(f"/releases/{album.id}/compare").status_code==200
        assert client.get(f"/releases/{album.id}/compare?left=99999").status_code==400
        assert client.get("/releases?reason=invalid").status_code==400
        assert client.get("/reconciliation").status_code==200
