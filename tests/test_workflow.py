import io
import json
import subprocess
import uuid

import guitarpro
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server import pipeline
from server.models import Project,Note,Bar,CapoSegment
from server.music import assign_fingering,sounding_pitch,metadata_settings,choose_settings,candidates
from server.export import gp5_bytes
from server.store import ROOT,connection,get_project,project_dir,save_project

client=TestClient(app)

def fixture_project(**changes):
    p=Project(id=uuid.uuid4().hex,title='검증용 기타',status='ready',tempo=120,duration=4,
      bars=[Bar(start=0,end=2,tempo=120),Bar(start=2,end=4,tempo=120)],
      notes=[Note(id='bass',midi=40,start=0,end=2.5,string=6,fret=0),
             Note(id='melody',midi=64,start=0,end=.5,string=1,fret=0),
             Note(id='next',midi=67,start=.5,end=1,string=1,fret=3)])
    for k,v in changes.items():setattr(p,k,v)
    return p

def inspect(path):
    r=subprocess.run(['node',str(ROOT/'scripts/score-bridge.mjs'),'inspect',str(path)],cwd=ROOT,capture_output=True,text=True,check=True)
    return json.loads(r.stdout)

def test_pitch_preserving_revoice_and_conflicts():
    p=fixture_project(tuning=[38,43,48,53,57,62],capo=2)
    original=[n.midi for n in p.notes];assign_fingering(p,strict=True)
    assert [sounding_pitch(n,p) for n in p.notes]==original
    p.capo=8
    with pytest.raises(ValueError):assign_fingering(p,strict=True)

def test_same_chord_cannot_share_string():
    p=fixture_project(notes=[Note(id=str(i),midi=n,start=0,end=1) for i,n in enumerate([40,47,52,55,59,64])])
    assign_fingering(p,strict=True)
    assert len({n.string for n in p.notes})==6
    assert all(sounding_pitch(n,p)==n.midi for n in p.notes)

def test_gp5_gp_roundtrip_pitch_rhythm_and_tie(tmp_path):
    p=fixture_project();gp5=tmp_path/'song.gp5';gp=tmp_path/'song.gp';gp5.write_bytes(gp5_bytes(p))
    subprocess.run(['node',str(ROOT/'scripts/score-bridge.mjs'),'convert',str(gp5),str(gp)],cwd=ROOT,check=True)
    for path in [gp5,gp]:
        result=inspect(path)
        assert result['title']==p.title
        assert result['duration']==pytest.approx(4)
        assert len(result['notes'])==3
        bass=next(n for n in result['notes'] if n['midi']==40)
        assert bass['end']==pytest.approx(2.5)
        assert {(n['midi'],n['string'],n['fret']) for n in result['notes']}=={(40,6,0),(64,1,0),(67,1,3)}

def test_harmonic_keeps_sounding_pitch(tmp_path):
    p=fixture_project(notes=[Note(id='harm',midi=83,start=0,end=.5,string=1,fret=7,technique='harmonic')])
    path=tmp_path/'harm.gp5';path.write_bytes(gp5_bytes(p));result=inspect(path)
    assert result['notes'][0]['midi']==83
    assert result['notes'][0]['technique']=='harmonic'

def test_capo_segments_and_percussion_tracks():
    p=fixture_project(notes=[Note(id='a',midi=64,start=0,end=1,string=1,fret=0),Note(id='b',midi=66,start=2,end=3,string=1,fret=0),Note(id='hit',midi=37,start=1,end=1.125,technique='percussion')],capo_segments=[CapoSegment(start=2,capo=2)])
    song=guitarpro.parse(io.BytesIO(gp5_bytes(p)),encoding='utf-8')
    assert len(song.tracks)==3
    assert [t.offset for t in song.tracks[:2]]==[0,2]
    assert song.tracks[2].isPercussionTrack

def test_unassigned_export_is_explicit():
    p=fixture_project(notes=[Note(id='x',midi=30,start=0,end=1)])
    with pytest.raises(ValueError,match='미정'):gp5_bytes(p)
    assert len(gp5_bytes(p,preview=True))>100

def test_api_persistence_optimistic_save_and_invalid_note():
    p=fixture_project();save_project(p)
    body=p.model_dump();body['title']='저장된 편집'
    saved=client.put(f'/api/projects/{p.id}',json=body)
    assert saved.status_code==200 and saved.json()['revision']==1
    assert client.get(f'/api/projects/{p.id}').json()['title']=='저장된 편집'
    assert client.put(f'/api/projects/{p.id}',json=body).status_code==409
    next_body=saved.json();next_body['notes'][0]['fret']=9
    assert client.put(f'/api/projects/{p.id}',json=next_body).status_code==422
    assert get_project(p.id).notes[0].fret==0

def test_api_revoice_rejects_without_mutating():
    p=fixture_project();save_project(p)
    r=client.post(f'/api/projects/{p.id}/revoice',json={'tuning':p.tuning,'capo':9})
    assert r.status_code==422
    assert get_project(p.id).capo==0

def test_youtube_validation_and_job_creation(monkeypatch):
    seen=[];monkeypatch.setattr(pipeline,'submit',lambda pid:seen.append(pid))
    assert client.post('/api/projects',json={'url':'https://example.com/watch?v=AbCdEfGhIjK'}).status_code==422
    r=client.post('/api/projects',json={'url':'https://youtu.be/AbCdEfGhIjK?t=2'})
    assert r.status_code==200 and r.json()['video_id']=='AbCdEfGhIjK'
    assert seen==[r.json()['id']]
    assert pipeline.video_id('https://www.youtube.com/shorts/_abcDEF-123')=='_abcDEF-123'

def test_reference_import_is_labeled_not_ai():
    p=fixture_project()
    r=client.post('/api/import',files={'file':('검증.gp5',gp5_bytes(p),'application/octet-stream')})
    assert r.status_code==200
    assert r.json()['source']=='score'
    assert 'AI 채보 결과가 아닙니다' in r.json()['warnings'][0]

def test_metadata_tuning_and_two_capos():
    tuning,capos,line=metadata_settings('Tuning : Drop D 1 Capo - 4 Capo')
    assert tuning==[38,45,50,55,59,64] and capos==[1,4]

def test_rearticulation_never_resurrects_old_note(tmp_path):
    p=fixture_project(notes=[Note(id='a',midi=64,start=0,end=2,string=1,fret=0),Note(id='b',midi=67,start=.5,end=1,string=1,fret=3)])
    path=tmp_path/'rearticulation.gp5';path.write_bytes(gp5_bytes(p));notes=inspect(path)['notes']
    assert len(notes)==2
    assert notes[0]['end']==pytest.approx(.5)

def test_export_refuses_to_silently_drop_colliding_notes():
    p=fixture_project(notes=[Note(id='a',midi=64,start=0,end=.1,string=1,fret=0),Note(id='b',midi=67,start=.001,end=.15,string=1,fret=3)])
    with pytest.raises(ValueError,match='겹친'):gp5_bytes(p)

def test_cancel_before_start_does_not_download():
    import threading
    p=fixture_project(status='queued');save_project(p)
    pipeline.CANCEL[p.id]=threading.Event();pipeline.CANCEL[p.id].set()
    pipeline.run(p.id)
    assert get_project(p.id).status=='cancelled'

def test_repeated_out_of_range_notes_challenge_description():
    notes=[Note(id=str(i),midi=pitch,start=i,end=i+.5) for i,pitch in enumerate([44,49,54,59,63,68]*12)]
    tuning,capo=choose_settings(notes,[40,45,50,55,59,64],5)
    assert capo==5 and tuning!=[40,45,50,55,59,64]
    assert all(candidates(n.midi,tuning,capo) for n in notes)

def history_rows(pid):
    with connection() as c:return c.execute('SELECT count(*) FROM history WHERE project_id=?',(pid,)).fetchone()[0]

def test_delete_removes_project_history_and_files():
    p=fixture_project();save_project(p);save_project(p,edit=True)
    folder=project_dir(p.id);(folder/'audio.wav').write_bytes(b'x')
    assert history_rows(p.id)==1
    r=client.delete(f'/api/projects/{p.id}')
    assert r.status_code==200 and r.json()=={'status':'deleted'}
    assert client.get(f'/api/projects/{p.id}').status_code==404
    assert p.id not in [x['id'] for x in client.get('/api/projects').json()]
    assert history_rows(p.id)==0 and not folder.exists()
    assert client.delete(f'/api/projects/{p.id}').status_code==404

@pytest.mark.parametrize('status',['queued','processing'])
def test_delete_refuses_running_analysis(status):
    # The analysis job keeps writing to the project, so its files must stay until it stops.
    p=fixture_project(status=status);save_project(p);media=project_dir(p.id)/'audio.wav';media.write_bytes(b'x')
    r=client.delete(f'/api/projects/{p.id}')
    assert r.status_code==409 and '분석을 취소한 뒤' in r.json()['detail']
    assert get_project(p.id).status==status and media.exists()
