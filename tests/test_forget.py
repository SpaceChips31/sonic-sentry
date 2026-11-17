import json
import time
import pytest
from sqlalchemy import select
from client import TestClient
from app import main, config
from app.models import (Release, Track, AnalysisJob, AnalysisSource, SourceObservation, AnalyzedContent,
    FileBatch, FileTask, ReviewEvent, ReconcileScan)
from app.services import forget, importer, ingestion
from test_workflow import configure, report
from test_operations import trashed, add_user


def album(setup):
    configure(setup)
    return importer.import_report(report(setup))


def execute(ids, mode="forget"):
    plan = forget.forget_preview(ids)
    return forget.forget_albums(ids, plan["token"], mode)


def test_forget_scoped_metadata_and_keeps_files_and_other_album(setup):
    first = album(setup)
    with setup.db() as session:
        other = Release(title="Other", source_path=str(setup.inbox/"Other"), status="PASS")
        session.add(other)
        session.flush()
        other_id = other.id
        job = AnalysisJob(source_type="TEST", source_path=first.source_path, display_name="Old", status="COMPLETED", created_at="now", release_id=first.id, hidden=True)
        session.add(job)
        session.flush()
        session.add_all([
            AnalyzedContent(content_hash="selected", release_id=first.id),
            AnalyzedContent(content_hash="other", release_id=other_id),
            SourceObservation(source_id=1,path=first.source_path,fingerprint="a",stable_since=0,state="COMPLETED",job_id=job.id),
            SourceObservation(source_id=1,path="duplicate",fingerprint="b",stable_since=0,state="DUPLICATE",content_hash="selected"),
            SourceObservation(source_id=1,path="other",fingerprint="c",stable_since=0,state="COMPLETED",content_hash="other"),
            ReviewEvent(release_id=first.id,track_path="old",previous="NONE",decision="APPROVED",created_at="now"),
            FileBatch(id="shared", actor="local", created_at="now"),
            FileTask(id="selected",batch_id="shared",release_id=first.id,source_path=first.source_path,destination_path="dest",state="COMPLETE",actor="local",created_at="now"),
            FileTask(id="other",batch_id="shared",release_id=other_id,source_path="other",destination_path="other-dest",state="COMPLETE",actor="local",created_at="now"),
            ReconcileScan(id="scan",root=str(setup.inbox),state="COMPLETE",results_json=json.dumps([{"id":first.id},{"id":other_id}]),actor="local",created_at="now")])
        session.commit()
    audio = setup.inbox/"Album"/"track.flac"
    before = audio.stat()
    assert execute([first.id]) == {"forgotten":1,"queued":0}
    assert audio.read_bytes() == b"test audio"
    assert audio.stat().st_mode == before.st_mode
    with setup.db() as session:
        assert session.get(Release, first.id) is None
        assert session.get(Release, other_id) is not None
        assert not list(session.scalars(select(Track)))
        assert not list(session.scalars(select(AnalysisJob)))
        assert not list(session.scalars(select(ReviewEvent)))
        assert [v.path for v in session.scalars(select(SourceObservation))] == ["other"]
        assert session.get(AnalyzedContent,"other")
        assert session.get(FileBatch,"shared") and session.get(FileTask,"other")
        assert not session.get(FileTask,"selected")
        assert json.loads(session.get(ReconcileScan,"scan").results_json)==[{"id":other_id}]


def test_bulk_skips_busy_album_and_token_detects_change(setup):
    first = album(setup)
    with setup.db() as session:
        second = Release(title="Busy",source_path=str(setup.inbox/"Busy"),status="PASS")
        session.add(second); session.flush()
        second_id=second.id
        session.add(AnalysisJob(source_type="TEST",source_path=second.source_path,display_name="Busy",status="QUEUED",created_at="now",release_id=second.id))
        session.commit()
    plan=forget.forget_preview([first.id,second_id])
    assert plan["eligible"]==1
    with setup.db() as session:
        session.get(Release,first.id).status="QUARANTINE"
        session.commit()
    with pytest.raises(forget.FileOperationError,match="changed"):
        forget.forget_albums([first.id,second_id],plan["token"],"forget")
    assert execute([first.id,second_id])["forgotten"]==1
    with setup.db() as session:
        assert session.get(Release,second_id)


@pytest.mark.parametrize("scenario",["trash","transfer"])
def test_recovery_metadata_is_protected(setup,monkeypatch,scenario):
    if scenario=="trash":
        first,destination,_=trashed(setup)
    else:
        from test_recovery_ingestion import interrupted_transfer
        first,_=interrupted_transfer(setup,monkeypatch,"before_publish")
    plan=forget.forget_preview([first.id])
    assert plan["eligible"]==0
    with pytest.raises(forget.FileOperationError):
        forget.forget_albums([first.id],plan["token"],"forget")
    with setup.db() as session:
        assert session.get(Release,first.id)


def test_forget_and_rescan_is_atomic_and_keeps_source_policy(setup,monkeypatch):
    first=album(setup)
    with setup.db() as session:
        source=AnalysisSource(name="Inbox",path=str(setup.inbox),kind="FILESYSTEM",enabled=True,watch_enabled=True,routing_policy="manual")
        session.add(source); session.commit(); source_id=source.id
    plan=forget.forget_preview([first.id])
    with monkeypatch.context() as patch:
        patch.setattr(forget,"enqueue_paths_in_session",lambda *args,**kwargs:[])
        with pytest.raises(forget.FileOperationError,match="no records"):
            forget.forget_albums([first.id],plan["token"],"rescan")
    with setup.db() as session:
        assert session.get(Release,first.id)
        assert list(session.scalars(select(Track)))
    assert execute([first.id],"rescan")=={"forgotten":1,"queued":1}
    with setup.db() as session:
        job=session.scalar(select(AnalysisJob))
        assert job.status=="QUEUED" and job.source_id==source_id and job.routing_policy=="manual"
        assert session.get(AnalysisSource,source_id).watch_enabled
        assert session.get(Release,first.id) is None


def test_watched_album_can_be_rediscovered_after_forget(setup):
    first=album(setup)
    with setup.db() as session:
        source=AnalysisSource(name="Inbox",path=str(setup.inbox),kind="FILESYSTEM",enabled=True,watch_enabled=True,settle_seconds=10)
        session.add(source); session.flush()
        session.add(SourceObservation(source_id=source.id,path=first.source_path,fingerprint="old",stable_since=0,state="COMPLETED"))
        session.commit()
    execute([first.id])
    now=time.time()+100
    assert ingestion.scan_sources(now=now)==0
    assert ingestion.scan_sources(now=now+11)==1


def test_missing_folder_can_only_be_forgotten(setup):
    first=album(setup)
    (setup.inbox/"Album"/"track.flac").unlink()
    plan=forget.forget_preview([first.id])
    assert plan["eligible"]==1 and not plan["can_rescan"]
    with pytest.raises(forget.FileOperationError,match="cannot be queued"):
        forget.forget_albums([first.id],plan["token"],"rescan")
    assert execute([first.id])["forgotten"]==1


def test_single_bulk_preview_and_confirmation_routes(setup):
    first=album(setup)
    with TestClient(main.app) as client:
        assert 'bulk-forget' in client.get('/releases').text
        assert f'/releases/{first.id}/forget' in client.get(f'/releases/{first.id}').text
        assert client.get(f'/releases/{first.id}/forget').status_code==200
        assert client.post('/releases/forget/preview',data={'release_ids':[first.id]}).status_code==200
        data={'release_ids':[first.id],'mode':'forget','token':forget.forget_preview([first.id])['token'],'confirmation':'wrong'}
        assert client.post('/releases/forget',data=data).status_code==400
        data.update(confirmation='DIMENTICA',token='0'*64)
        assert client.post('/releases/forget',data=data).status_code==409
        data['token']=forget.forget_preview([first.id])['token']
        response=client.post('/releases/forget',data=data,follow_redirects=False)
        assert response.status_code==303
        assert client.get(response.headers['location']).status_code==200
    assert (setup.inbox/'Album'/'track.flac').exists()


def test_viewer_cannot_forget(setup,monkeypatch):
    first=album(setup)
    add_user(setup,'admin','admin'); add_user(setup,'viewer','viewer')
    monkeypatch.setattr(config,'AUTH_ENABLED',True)
    with TestClient(main.app) as client:
        client.post('/login',data={'username':'viewer','password':'a-test-password-123'})
        assert client.post('/releases/forget/preview',data={'release_ids':[first.id]}).status_code==403
        assert client.post('/releases/forget',data={'release_ids':[first.id],'mode':'forget','token':forget.forget_preview([first.id])['token'],'confirmation':'DIMENTICA'}).status_code==403


@pytest.mark.parametrize('ids',[[],[0],[-1],list(range(1,52))])
def test_invalid_selection(setup,ids):
    with pytest.raises(forget.FileOperationError):
        forget.forget_preview(ids)


def test_bulk_rescan_two_albums_and_overlapping_paths(setup):
    first=album(setup)
    second_path=setup.inbox/'Second'
    second_path.mkdir()
    (second_path/'track.FLAC').write_bytes(b'other audio')
    with setup.db() as session:
        second=Release(title='Second',source_path=str(second_path),status='PASS')
        session.add(second);session.commit();second_id=second.id
    assert execute([first.id,second_id],'rescan')=={'forgotten':2,'queued':2}
    with setup.db() as session:
        assert not list(session.scalars(select(Release)))
        assert {j.source_path for j in session.scalars(select(AnalysisJob))}=={first.source_path,str(second_path)}


def test_overlapping_selected_paths_cannot_be_rescanned_together(setup):
    first=album(setup)
    with setup.db() as session:
        parent=Release(title='Parent',source_path=str(setup.inbox),status='PASS')
        session.add(parent);session.commit();parent_id=parent.id
    plan=forget.forget_preview([first.id,parent_id])
    assert plan['eligible']==2 and not plan['can_rescan']
    with pytest.raises(forget.FileOperationError):
        forget.forget_albums([first.id,parent_id],plan['token'],'rescan')
