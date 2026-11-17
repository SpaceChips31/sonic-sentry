from types import SimpleNamespace
import pytest
from sqlalchemy import select
from client import TestClient
from app import main
from app.models import Release,Track,ReviewEvent
from app.services import importer
from app.services.result_guidance import album_guidance,track_guidance
from test_workflow import configure,report


def track(identifier, status="PASS", integrity=True, review="NONE", verdict=None):
    return SimpleNamespace(id=identifier,path=str(identifier),status=status,integrity_ok=integrity,
        human_review=review,forensic_verdict=verdict,forensic_json=None)


def test_guidance_prioritizes_integrity_and_pending_tracks():
    tracks=[track(1),track(2,"QUARANTINE"),track(3,"REJECTED",False),track(4,"QUARANTINE",review="APPROVED")]
    guide=album_guidance(SimpleNamespace(tracks=tracks,status="REJECTED"))
    assert guide['broken']==1 and guide['pending']==1 and guide['approved']==1
    assert [t.id for t in guide['tracks']]==[3,2,1,4]
    assert not guide['can_approve'] and not guide['can_stage']
    assert track_guidance(tracks[2])['title']=='Damaged or unreadable file'


@pytest.mark.parametrize('status,review,title',[
    ('PASS','NONE','Automatic checks passed'),
    ('QUARANTINE','NONE','Needs your review'),
    ('QUARANTINE','APPROVED','Approved by your review'),
    ('QUARANTINE','REJECTED','Rejected by your review'),
    ('REJECTED','APPROVED','Rejected by automatic checks')])
def test_track_explanation_keeps_automatic_and_human_decisions_separate(status,review,title):
    value=track(1,status,review=review,verdict='SUSPICIOUS')
    assert track_guidance(value)['title']==title
    assert value.forensic_verdict=='SUSPICIOUS'


def test_approved_uncertain_track_still_explains_cutoff():
    value=track(1,'QUARANTINE',review='APPROVED')
    value.forensic_json='{"result":{"authenticity":{"spectral":{"cutoff_hz":16000}}}}'
    assert 'cutoff alone does not prove' in track_guidance(value)['explanation']


def test_results_offer_review_before_technical_details_and_preserve_files(setup):
    configure(setup)
    release=importer.import_report(report(setup,'QUARANTINE'))
    with setup.db() as session:
        first=session.scalar(select(Track))
        first_id=first.id
        second=Track(release_id=release.id,path=release.source_path+'/second.flac',display_path='second.flac',status='QUARANTINE',integrity_ok=1)
        session.add(second);session.commit();second_id=second.id
    with TestClient(main.app) as client:
        response=client.get(f'/tracks/{first_id}')
        assert response.status_code==200
        html=response.text
        assert html.index('result-guide')<html.index('result-details')
        assert f'/tracks/{second_id}' in html
        assert 'formaction="/tracks/'+str(first_id)+'/review/reject"' in html
        assert '<summary>' in html
        assert client.get(f'/releases/{release.id}').status_code==200
        response=client.post(f'/releases/{release.id}/review',data={'decision':'approve','note':'Checked source'},follow_redirects=False)
        assert response.status_code==303 and response.headers['location']==f'/releases/{release.id}'
        html=client.get(response.headers['location']).text
        assert 'result-review' not in html
        assert f'/releases/{release.id}/files' in html
    with setup.db() as session:
        assert session.get(Release,release.id).status=='PASS'
        assert {t.human_review for t in session.scalars(select(Track))}=={'APPROVED'}
        assert len(list(session.scalars(select(ReviewEvent))))==2
    assert (setup.inbox/'Album'/'track.flac').read_bytes()==b'test audio'
    assert not list((setup.root/'staging').iterdir())


def test_album_review_cannot_override_integrity_failure(setup):
    release=importer.import_report(report(setup,'QUARANTINE',False))
    with TestClient(main.app) as client:
        html=client.get(f'/releases/{release.id}').text
        assert 'name="decision" value="approve"' not in html
        assert client.post(f'/releases/{release.id}/review',data={'decision':'approve'}).status_code==409
    with setup.db() as session:
        assert session.scalar(select(Track)).human_review=='NONE'


def test_approved_track_shows_review_decision_in_header(setup):
    release=importer.import_report(report(setup,'QUARANTINE'))
    with setup.db() as session:
        value=session.scalar(select(Track));value.human_review='APPROVED';identifier=value.id
        session.commit()
    with TestClient(main.app) as client:
        html=client.get(f'/tracks/{identifier}').text
        assert '<span class="badge large pass">Approved</span>' in html
        assert 'SUSPICIOUS' not in html or 'Automatic result' in html


def test_viewer_cannot_use_album_review(setup,monkeypatch):
    from app import config
    from test_operations import add_user
    release=importer.import_report(report(setup,'QUARANTINE'))
    add_user(setup,'admin','admin');add_user(setup,'viewer','viewer')
    monkeypatch.setattr(config,'AUTH_ENABLED',True)
    with TestClient(main.app) as client:
        client.post('/login',data={'username':'viewer','password':'a-test-password-123'})
        assert client.post(f'/releases/{release.id}/review',data={'decision':'approve'}).status_code==403
