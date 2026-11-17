import pytest
from sqlalchemy import select
from client import TestClient
from app import main
from app.models import Release,Track
from app.services import importer
from app.services.verdicts import effective_track_status,effective_track_status_sql,recalculate_release_status
from test_workflow import report


def add_tracks(session,release_id,values):
    for i,(status,review,integrity) in enumerate(values):
        session.add(Track(release_id=release_id,path=f'/album/{i}.flac',display_path=f'{i}.flac',status=status,human_review=review,integrity_ok=integrity))


def test_dashboard_reproduces_all_approved_screenshot(setup):
    with setup.db() as session:
        album=Release(title='Library',source_path='/album',status='PASS')
        session.add(album);session.flush()
        add_tracks(session,album.id,[('PASS','NONE',1)]*1787+[('QUARANTINE','APPROVED',1)]*281)
        session.commit()
    with TestClient(main.app) as client:
        response=client.get('/')
        assert response.status_code==200
        counts=response.context['track_counters']
        assert counts['total']==2068
        assert counts['accepted']==2068 and counts['accepted_pct']==100.0
        assert counts['accepted_automatic']==1787 and counts['accepted_manual']==281
        assert counts['pending_review']==counts['current_rejected']==counts['integrity_failures']==0
        assert counts['pass']==1787 and counts['quarantine']==281 and counts['pass_pct']==86.4
        html=response.text
        assert 'Accepted</' not in html or '2068' in html
        assert 'href="/releases?status=QUARANTINE"' not in html
        assert '<summary>Original automatic results</summary>' in html


def test_dashboard_rejections_are_not_all_integrity_failures(setup):
    with setup.db() as session:
        album=Release(title='Mixed',source_path='/album',status='REJECTED')
        session.add(album);session.flush()
        add_tracks(session,album.id,[('PASS','NONE',1),('QUARANTINE','APPROVED',1),('QUARANTINE','NONE',1),('QUARANTINE','REJECTED',1),('REJECTED','NONE',1),('REJECTED','APPROVED',0)])
        session.commit()
    with TestClient(main.app) as client:
        counts=client.get('/').context['track_counters']
        assert counts['accepted']==2 and counts['pending_review']==1 and counts['current_rejected']==3
        assert counts['integrity_failures']==1 and counts['rejected']==2
        assert counts['accepted']+counts['pending_review']+counts['current_rejected']==counts['total']


def test_dashboard_empty_library_has_zero_percent_and_no_division_error(setup):
    with TestClient(main.app) as client:
        response=client.get('/')
        assert response.status_code==200
        counts=response.context['track_counters']
        assert counts['total']==counts['accepted']==counts['accepted_pct']==0


def test_review_updates_current_dashboard_without_changing_automatic_evidence(setup):
    album=importer.import_report(report(setup,'QUARANTINE'))
    with setup.db() as session:
        identifier=session.scalar(select(Track.id))
    with TestClient(main.app) as client:
        before=client.get('/').context['track_counters']
        assert before['pending_review']==1 and before['accepted']==0
        assert client.post(f'/tracks/{identifier}/review/approve',follow_redirects=False).status_code==303
        after=client.get('/').context['track_counters']
        assert after['pending_review']==0 and after['accepted']==after['accepted_manual']==1
        assert after['quarantine']==before['quarantine']==1
        assert after['pass']==before['pass']==0


@pytest.mark.parametrize('status,review,integrity,expected',[
    ('PASS','NONE',1,'PASS'),('PASS','REJECTED',1,'REJECTED'),
    ('QUARANTINE','NONE',1,'QUARANTINE'),('QUARANTINE','APPROVED',1,'PASS'),
    ('QUARANTINE','REJECTED',1,'REJECTED'),('REJECTED','APPROVED',1,'REJECTED'),
    ('QUARANTINE','APPROVED',0,'REJECTED')])
def test_current_track_and_album_decisions_match_database_counts(setup,status,review,integrity,expected):
    with setup.db() as session:
        album=Release(title='Test',source_path='/album',status='QUARANTINE')
        session.add(album);session.flush()
        add_tracks(session,album.id,[(status,review,integrity)])
        session.flush()
        track=session.scalar(select(Track))
        assert effective_track_status(track)==expected
        assert session.scalar(select(effective_track_status_sql()).select_from(Track))==expected
        recalculate_release_status(album)
        assert album.status==expected
        assert track.status==status and track.human_review==review
