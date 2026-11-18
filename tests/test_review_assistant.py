import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from client import TestClient
from app import main
from app.models import Release, Track, ReviewEvent
from app.services import importer
from app.services.review_assistant import evidence_for, album_comparison, next_pending, review_progress
from test_workflow import report


def sample(identifier,cutoff=16000,rate="44100",precision="16-bit",**spectral):
    return SimpleNamespace(id=identifier,integrity_ok=1,status='QUARANTINE',human_review='NONE',
        forensic_json=json.dumps({'result':{'technical':{'sample_rate':rate,'channels':'2','precision':precision},
        'authenticity':{'spectral':{'cutoff_hz':cutoff,**spectral}}}}))


def review_album(setup):
    release=importer.import_report(report(setup,'QUARANTINE'))
    with setup.db() as session:
        first=session.scalar(select(Track));first.path=release.source_path+'/01.flac'
        second=Track(release_id=release.id,path=release.source_path+'/02.flac',display_path='02.flac',status='QUARANTINE',integrity_ok=1)
        session.add(second);session.commit()
        return release.id,first.id,second.id


def test_guided_review_resumes_saves_and_finishes_without_moving_files(setup):
    release_id,first,second=review_album(setup)
    with TestClient(main.app) as client:
        assert client.get(f'/releases/{release_id}/review',follow_redirects=False).headers['location']==f'/tracks/{first}?guided=1'
        html=client.get(f'/tracks/{first}?guided=1').text
        assert 'name="continue_review"' in html
        assert client.post(f'/tracks/{first}/review/approve',data={'continue_review':'true','note':'Source checked'},follow_redirects=False).headers['location']==f'/releases/{release_id}/review?after={first}'
        assert client.get(f'/releases/{release_id}/review',follow_redirects=False).headers['location']==f'/tracks/{second}?guided=1'
        html=client.get(f'/tracks/{second}?guided=1').text
        assert '1 / 2' in html
        response=client.post(f'/tracks/{second}/review/reject',data={'continue_review':'true'},follow_redirects=True)
        assert response.status_code==200 and 'Album review summary' in response.text
        assert response.context['progress']['pending']==0
        assert response.context['guidance']['tone']=='rejected'
        assert client.post(f'/tracks/{second}/review/reset',data={'continue_review':'true'},follow_redirects=False).headers['location']==f'/tracks/{second}?guided=1'
    with setup.db() as session:
        events=list(session.scalars(select(ReviewEvent)))
        assert [e.decision for e in events]==['APPROVED','REJECTED','NONE']
        assert events[0].note=='Source checked'
    assert (setup.inbox/'Album'/'track.flac').read_bytes()==b'test audio'


def test_skip_does_not_review_and_last_skipped_track_stays_pending(setup):
    release_id,first,second=review_album(setup)
    with TestClient(main.app) as client:
        assert client.get(f'/releases/{release_id}/review?after={first}',follow_redirects=False).headers['location']==f'/tracks/{second}?guided=1'
        client.post(f'/tracks/{second}/review/approve')
        response=client.get(f'/releases/{release_id}/review?after={first}')
        assert response.status_code==200 and response.context['progress']['pending']==1
        assert client.get(f'/releases/{release_id}/review',follow_redirects=False).headers['location']==f'/tracks/{first}?guided=1'
    with setup.db() as session:
        assert session.get(Track,first).human_review=='NONE'
        assert len(list(session.scalars(select(ReviewEvent))))==1


def test_guided_summary_keeps_automatic_blockers_visible(setup):
    release=importer.import_report(report(setup,'REJECTED',False))
    with TestClient(main.app) as client:
        response=client.get(f'/releases/{release.id}/review')
        assert response.status_code==200
        assert response.context['progress']['blocked']==1
        assert response.context['progress']['total']==0
        assert response.context['guidance']['can_stage'] is False
        assert client.get(f'/releases/{release.id}/review?after=99999').status_code==400


def test_specific_evidence_preserves_actual_values_and_does_not_invent_weights():
    track=sample(1,16000,segment_total=12,segment_walled=9,codec_fingerprint='Codec sample',evidence=['Codec Wall Fingerprint: measured value'],natural_evidence=['Organic Frequency Rolloff: measured detail'])
    evidence=evidence_for(track)
    assert [f['value'] for f in evidence['facts']]==['16,000 Hz','9 / 12','Codec sample']
    assert evidence['suspicious']==['Codec Wall Fingerprint: measured value']
    assert evidence['natural']==['Organic Frequency Rolloff: measured detail']


@pytest.mark.parametrize('raw',['bad json','null','[]','{"result":null}','{"result":{"authenticity":[]}}'])
def test_old_or_malformed_report_is_not_evidence_and_does_not_crash_pages(setup,raw):
    release=importer.import_report(report(setup,'QUARANTINE'))
    with setup.db() as session:
        track=session.scalar(select(Track));track.forensic_json=raw;identifier=track.id
        session.commit()
    assert not evidence_for(SimpleNamespace(forensic_json=raw))['available']
    with TestClient(main.app) as client:
        assert client.get(f'/tracks/{identifier}?guided=1').status_code==200
        assert client.get(f'/tracks/{identifier}/spectrogram').status_code==404


def test_album_context_distinguishes_common_and_outlier_cutoffs_without_changing_verdicts():
    tracks=[sample(1,20000),sample(2,20100),sample(3,20200),sample(4,15000)]
    result=album_comparison(tracks)
    assert result[4]['state']=='lower' and result[4]['baseline']==20050
    assert result[1]['state']=='shared'
    assert all(t.status=='QUARANTINE' and t.human_review=='NONE' for t in tracks)


def test_context_does_not_compare_different_resolutions_or_missing_and_invalid_data():
    tracks=[sample(1,20000),sample(2,20000,rate='48000'),sample(3,15000,precision='24-bit'),sample(4,float('nan')),sample(5,999999)]
    assert all(row['state']=='unavailable' for row in album_comparison(tracks).values())
    assert not evidence_for(sample(1,False,segment_total=2,segment_walled=5))['available']


def test_context_does_not_invent_common_pattern_for_mixed_album():
    tracks=[sample(i,value) for i,value in enumerate([8000,12000,16000,20000],1)]
    assert {row['state'] for row in album_comparison(tracks).values()}=={'mixed'}


def test_foreign_track_cannot_be_used_to_navigate_album():
    release=SimpleNamespace(tracks=[sample(1),sample(2)])
    with pytest.raises(ValueError):
        next_pending(release,99)
    assert next_pending(release,1)==2
    assert review_progress(release)['pending']==2


def test_guided_approval_cannot_accept_a_damaged_track_even_with_forged_form(setup):
    album=importer.import_report(report(setup,'QUARANTINE',False))
    with setup.db() as session:
        identifier=session.scalar(select(Track.id))
    with TestClient(main.app) as client:
        assert client.post(f'/tracks/{identifier}/review/approve',data={'continue_review':'true'}).status_code==409
    with setup.db() as session:
        assert session.get(Track,identifier).human_review=='NONE'
        assert not list(session.scalars(select(ReviewEvent)))
