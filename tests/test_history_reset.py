import pytest
from sqlalchemy import select
from client import TestClient
from app import main,config
from app.models import (Release,Track,AnalysisJob,AnalysisSource,WorkflowSettings,AuthUser,
    FileTask,ReconcileScan)
from app.services import history_reset,importer,file_workflow
from test_workflow import configure,report
from test_operations import trashed,add_user


def test_reset_clears_metadata_but_keeps_config_accounts_and_all_media(setup):
    configure(setup)
    album=importer.import_report(report(setup))
    destination=file_workflow.move_release(album.id,"STAGING")
    with setup.db() as session:
        original_config=session.get(WorkflowSettings,1).payload
        session.add(AuthUser(username="admin",password_hash="preserve-hash",role="admin"))
        session.add(AnalysisSource(name="Inbox",path=str(setup.inbox),kind="FILESYSTEM",enabled=True,watch_enabled=True))
        session.add(AnalysisJob(source_type="TEST",source_path=str(setup.inbox),display_name="Pending",status="QUEUED",created_at="now"))
        session.add(FileTask(id="queued",release_id=album.id,source_path=str(destination),destination_path=str(setup.root/"other"),state="QUEUED",actor="local",created_at="now"))
        session.add(ReconcileScan(id="scan",root=str(setup.inbox),state="QUEUED",actor="local",created_at="now"))
        session.commit()
    report_path=setup.root/"retained-report.json"
    report_path.write_bytes(b"report")
    assert not history_reset.reset_preview()["blockers"]
    assert history_reset.reset_history()>0
    assert history_reset.reset_preview()["total"]==0
    assert (destination/"track.flac").read_bytes()==b"test audio"
    assert report_path.read_bytes()==b"report"
    with setup.db() as session:
        assert session.get(WorkflowSettings,1).payload==original_config
        assert session.get(AuthUser,"admin").password_hash=="preserve-hash"
        source=session.scalar(select(AnalysisSource))
        assert source.enabled and not source.watch_enabled
        assert source.path==str(setup.inbox)


@pytest.mark.parametrize("state",["ANALYZING","ROUTING"])
def test_reset_refuses_active_analysis(setup,state):
    album=importer.import_report(report(setup))
    with setup.db() as session:
        session.add(AnalysisJob(source_type="TEST",source_path=album.source_path,display_name="Active",status=state,created_at="now"))
        session.commit()
    with pytest.raises(ValueError,match="active work"):
        history_reset.reset_history()
    with setup.db() as session:
        assert session.get(Release,album.id) is not None


def test_reset_preserves_recoverable_trash_manifests(setup):
    album,destination,_=trashed(setup)
    with pytest.raises(ValueError,match="recoverable trash"):
        history_reset.reset_history()
    assert (destination/"track.flac").exists()
    with setup.db() as session:
        assert session.get(Release,album.id) is not None


def test_reset_refuses_unfinished_transfer(setup,monkeypatch):
    from test_recovery_ingestion import interrupted_transfer
    album,_=interrupted_transfer(setup,monkeypatch,"before_publish")
    with pytest.raises(ValueError,match="unfinished transfers"):
        history_reset.reset_history()
    assert (setup.inbox/"Album"/"track.flac").exists()


def test_reset_requires_exact_confirmation_and_keeps_files(setup):
    album=importer.import_report(report(setup))
    with TestClient(main.app) as client:
        assert client.get("/maintenance/reset").status_code==200
        assert client.post("/maintenance/reset",data={"confirmation":"wrong"}).status_code==400
        with setup.db() as session:
            assert session.get(Release,album.id) is not None
        response=client.post("/maintenance/reset",data={"confirmation":"AZZERA"},follow_redirects=False)
        assert response.status_code==303
        assert client.get(response.headers["location"]).status_code==200
    assert history_reset.reset_preview()["total"]==0
    assert (setup.inbox/"Album"/"track.flac").exists()


@pytest.mark.parametrize("role",["viewer","operator"])
def test_reset_is_admin_only(setup,monkeypatch,role):
    add_user(setup,"admin","admin")
    add_user(setup,role,role)
    monkeypatch.setattr(config,"AUTH_ENABLED",True)
    with TestClient(main.app) as client:
        assert client.post("/login",data={"username":role,"password":"a-test-password-123"},follow_redirects=False).status_code==303
        assert client.get("/maintenance/reset").status_code==403
        assert client.post("/maintenance/reset",data={"confirmation":"AZZERA"}).status_code==403
