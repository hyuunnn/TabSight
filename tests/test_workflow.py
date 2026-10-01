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

def written(data,string):
    """(tied, hammer, slide, bend point values) of each note the GP5 writes on a string, in order."""
    song=guitarpro.parse(io.BytesIO(data),encoding='utf-8');out=[]
    for measure in song.tracks[0].measures:
        for beat in measure.voices[0].beats:
            for n in beat.notes:
                if n.string==string:
                    e=n.effect;out.append((n.type==guitarpro.NoteType.tie,e.hammer,bool(e.slides),[v.value for v in e.bend.points] if e.bend else None))
    return out

@pytest.mark.parametrize('technique,frets',[('hammer',(3,5)),('pull',(5,3)),('slide',(3,5))])
def test_legato_leaves_from_the_last_tied_segment_and_imports_back(tmp_path,technique,frets):
    # The bass onset at .5 splits 'a' into two tied segments. The legato has to reach 'b', not a's own tie,
    # and reading the file back keeps it on 'a'.
    a,b=frets
    p=fixture_project(notes=[Note(id='bass',midi=45,start=.5,end=1,string=5,fret=0),
        Note(id='a',midi=64+a,start=0,end=1,string=1,fret=a,technique=technique),Note(id='b',midi=64+b,start=1,end=1.5,string=1,fret=b)])
    data=gp5_bytes(p);flag=2 if technique=='slide' else 1
    assert [(x[0],x[flag]) for x in written(data,1)]==[(False,False),(True,True),(False,False)]
    path=tmp_path/'legato.gp5';path.write_bytes(data)
    assert {n['fret']:n['technique'] for n in inspect(path)['notes'] if n['string']==1}=={a:technique,b:'normal'}

def test_bend_is_a_semitone_and_held_across_ties():
    # Bend points count quarter tones. A tied segment holds the bent pitch instead of bending again.
    p=fixture_project(notes=[Note(id='bass',midi=45,start=.5,end=1,string=5,fret=0),Note(id='a',midi=64,start=0,end=1,string=2,fret=5,technique='bend')])
    assert [x[3] for x in written(gp5_bytes(p),2)]==[[0,2],[2,2]]

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
    # The reason names the string and the note, so the player knows what to change.
    assert '6번 줄 C♯3, 카포 9 포함' in r.json()['detail'] and '가장 낮은 음 E2' in r.json()['detail']
    assert get_project(p.id).capo==0

def test_api_revoice_can_keep_unplayable_notes_for_review():
    p=fixture_project();save_project(p)
    r=client.post(f'/api/projects/{p.id}/revoice',json={'tuning':p.tuning,'capo':9,'allow_unplayable':True})
    assert r.status_code==200 and r.json()['capo']==9 and r.json()['revision']==1
    notes={n['id']:n for n in r.json()['notes']}
    assert notes['bass']['string']==0 and notes['bass']['midi']==40  # kept at its pitch, left for review
    q=get_project(p.id)
    assert all(sounding_pitch(n,q)==n.midi for n in q.notes if n.string)

def test_retuning_turns_a_lost_harmonic_into_a_fretted_note():
    # E6 is the 7th-fret harmonic of standard E4; half step down has no natural harmonic at that pitch.
    p=fixture_project(notes=[Note(id='harm',midi=83,start=0,end=.5,string=1,fret=7,technique='harmonic')],tuning=[39,44,49,54,58,63])
    assert assign_fingering(p,strict=True)['unassigned']==0
    n=p.notes[0]
    assert n.technique=='normal' and 'harmonic-candidate' in n.evidence and sounding_pitch(n,p)==83

def test_unplaced_harmonic_does_not_block_save_or_preview():
    # B6 is the 3rd-fret harmonic of E4. Half step down has neither that harmonic nor a fret for it.
    p=fixture_project(notes=[Note(id='harm',midi=95,start=0,end=.5,string=1,fret=3,technique='harmonic')]);save_project(p)
    r=client.post(f'/api/projects/{p.id}/revoice',json={'tuning':[39,44,49,54,58,63],'capo':0,'allow_unplayable':True})
    n=r.json()['notes'][0]
    assert n['string']==0 and n['midi']==95 and n['technique']=='normal' and 'harmonic-candidate' in n['evidence']
    assert client.put(f'/api/projects/{p.id}',json=r.json()).status_code==200
    assert client.get(f'/api/projects/{p.id}/score/gp5?preview=true').status_code==200
    assert '미정' in client.get(f'/api/projects/{p.id}/score/gp5').json()['detail']
    # Older code saved such a note as a harmonic without a string; that project must still save and preview.
    q=fixture_project(notes=[Note(id='h',midi=95,start=0,end=.5,technique='harmonic')]);save_project(q)
    assert client.put(f'/api/projects/{q.id}',json=q.model_dump()).status_code==200
    assert client.get(f'/api/projects/{q.id}/score/gp5?preview=true').status_code==200

def test_import_keeps_unsupported_harmonics_as_fretted_notes_to_review():
    p=fixture_project(notes=[Note(id=i,midi=76,start=s,end=s+.5,string=1,fret=12,technique='harmonic') for i,s in [('a',0),('b',.5)]])
    song=guitarpro.parse(io.BytesIO(gp5_bytes(p)),encoding='utf-8')
    song.tracks[0].measures[0].voices[0].beats[1].notes[0].value=16  # a 16th-fret natural harmonic
    out=io.BytesIO();guitarpro.write(song,out,version=(5,1,0),encoding='utf-8')
    r=client.post('/api/import',files={'file':('harmonics.gp5',out.getvalue(),'application/octet-stream')})
    by_fret={n['fret']:n for n in r.json()['notes']}
    assert by_fret[12]['technique']=='harmonic' and by_fret[12]['midi']==76
    other=by_fret[16]
    assert other['technique']=='normal' and other['midi']==80 and not other['reviewed'] and 'harmonic-candidate' in other['evidence']
    assert any('다른 하모닉스 1개' in w for w in r.json()['warnings'])
    assert client.put(f"/api/projects/{r.json()['id']}",json=r.json()).status_code==200

def test_revoice_puts_moved_and_unplaced_notes_back_on_the_review_list():
    # Under capo 2 each note has one outcome whatever the fingering costs: E2 cannot be played, open E4
    # has to leave the 1st string, and the 22nd-fret D6 stays at the same place as fret 20.
    p=fixture_project(notes=[Note(id='low',midi=40,start=0,end=.5,string=6,fret=0,reviewed=True),
        Note(id='open',midi=64,start=1,end=1.5,string=1,fret=0,reviewed=True),Note(id='high',midi=86,start=2,end=2.5,string=1,fret=22,reviewed=True)])
    save_project(p)
    r=client.post(f'/api/projects/{p.id}/revoice',json={'tuning':p.tuning,'capo':2,'allow_unplayable':True})
    notes={n['id']:n for n in r.json()['notes']}
    assert notes['low']['string']==0 and not notes['low']['reviewed']
    assert notes['open']['string']!=1 and not notes['open']['reviewed']
    assert (notes['high']['string'],notes['high']['fret'])==(1,20) and notes['high']['reviewed']

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

def test_metadata_reads_note_names_nashville_and_capo_line():
    # Sungha Jung's baritone video: strings 4 and 3 sit an octave above plain ADGCEA.
    assert metadata_settings('Tuning : ADGCEA (Baritone Nashville Tuning)')[:2]==([33,38,55,60,52,57],[])
    assert metadata_settings('Tuning : ADGCEA')[0]==[33,38,43,48,52,57]
    assert metadata_settings('Tuning: D A D G A D')[0]==[38,45,50,55,57,62]
    assert metadata_settings('Tuning: Eb Ab Db Gb Bb Eb')[0]==[39,44,49,54,58,63]
    assert metadata_settings('Tuning: Nashville')[0]==[52,57,62,67,59,64]
    assert metadata_settings('Tuning: Standard\nCapo on 3rd fret')[:2]==([40,45,50,55,59,64],[3])

@pytest.mark.parametrize('text,tuning,capos',[
    # A name only counts whole, so a variant the presets lack stays empty instead of becoming the preset it starts with.
    ('Tuning: Drop Db',None,[]),('Tuning: Open Dm',None,[]),('Tuning: Open D minor',None,[]),('Tuning: Open C6',None,[]),
    ('Tuning: Eb standard',[39,44,49,54,58,63],[]),('Tuning: D Standard',[38,43,48,53,57,62],[]),
    ('Tuning: a standard tuning',[40,45,50,55,59,64],[]),
    # Read right before; a wider parser once broke these.
    ('Tuning: E Standard (half step down)',[39,44,49,54,58,63],[]),('Tuning: Drop D (6th string down a whole step)',[38,45,50,55,59,64],[]),
    ('Tuning: Drop D (from E standard)',[38,45,50,55,59,64],[]),('Tuning: Drop D Standard',[38,45,50,55,59,64],[]),
    # A number glued to letters is a capo model, and one next to a colon is a time.
    ('Tuning: Standard, G7th capo 3',[40,45,50,55,59,64],[3]),('Tuning: Standard, Shubb C1 capo 2',[40,45,50,55,59,64],[2]),
    ('Tuning: Standard, capo 123',[40,45,50,55,59,64],[]),('Tuning: standard tuning, 0:05 capo 2',[40,45,50,55,59,64],[2]),
    ('Tuning: Standard\n0:00 capo 2, 1:30 capo 4',[40,45,50,55,59,64],[2,4]),
])
def test_metadata_reads_whole_names_and_only_fret_numbers(text,tuning,capos):
    assert metadata_settings(text)[:2]==(tuning,capos)

def test_described_tuning_without_capo_is_played_without_capo(monkeypatch):
    import numpy as np,soundfile as sf
    p=fixture_project(status='queued',source='file',metadata={'description':'Tuning : ADGCEA (Baritone Nashville Tuning)'});save_project(p)
    t=np.arange(16000*4)/16000;sf.write(project_dir(p.id)/'audio.wav',(.1*np.sin(2*np.pi*110*t)).astype('float32'),16000)
    seen={}
    def transcribe(y,sr,progress,cancelled):
        seen['called']=True
        return [Note(id=str(i),midi=m,start=i*.5,end=i*.5+.4) for i,m in enumerate([38,45,57,62,69])],'cpu'
    monkeypatch.setattr(pipeline,'transcribe_audio',transcribe)
    # Nothing is transcribed until the player confirms the settings the description filled in.
    pipeline.run(p.id);q=get_project(p.id)
    assert 'called' not in seen and q.status=='awaiting_settings' and q.tuning==[33,38,55,60,52,57] and q.capo==0
    assert q.metadata['detected_settings']=={'tuning':[33,38,55,60,52,57],'capos':[],'text':'Tuning : ADGCEA (Baritone Nashville Tuning)'}
    monkeypatch.setattr(pipeline,'submit',lambda pid,transcribe=False:pipeline.run(pid,transcribe))
    assert client.post(f'/api/projects/{p.id}/start',json={'tuning':q.tuning,'capo':q.capo}).status_code==200
    q=get_project(p.id)
    assert seen['called'] and q.status=='ready' and q.tuning==[33,38,55,60,52,57] and q.capo==0
    assert q.metadata['tuning_source']==q.metadata['capo_source']=='description'
    assert all(n.string for n in q.notes if n.technique!='percussion')

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

def test_custom_described_tuning_stays_in_the_comparison():
    # Baritone Nashville is not a preset. A few notes below its reach start the comparison with the
    # presets, but the rest fit it far better than any of them, so no preset may replace it.
    nashville=[33,38,55,60,52,57]
    notes=[Note(id=str(i),midi=m,start=i,end=i+.5) for i,m in enumerate(nashville*10+[31]*6)]
    assert choose_settings(notes,nashville,0)==(nashville,0)

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
