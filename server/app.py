from __future__ import annotations

import json
import shutil
import subprocess
import uuid
import tempfile
from pathlib import Path

from fastapi import FastAPI,File,HTTPException,UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field

from . import pipeline
from .export import gp5_bytes
from .models import CapoSegment,Project
from .music import assign_fingering,build_bars,TUNINGS,sounding_pitch,HARMONICS
from .store import DATA,ROOT,delete_project,get_project,list_projects,project_dir,save_project

app=FastAPI(title='TabSight',docs_url='/api/docs')
app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:5173','http://localhost:5173','http://127.0.0.1:8787','http://localhost:8787'],allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Content-Type'])


@app.get('/api/health')
def health():
    return {'status':'ok','local':True,'tunings':TUNINGS,'models_cached':(DATA/'models'/'gaps'/'guitar-gaps-paper-version-12200_iterations.pth').exists()}


@app.get('/api/projects')
def projects():return list_projects()


def project(pid):
    try:return get_project(pid)
    except KeyError:raise HTTPException(404,'프로젝트를 찾을 수 없습니다.')


class NewProject(BaseModel):
    url:str


@app.post('/api/projects')
def create(body:NewProject):
    try:vid=pipeline.video_id(body.url)
    except ValueError as e:raise HTTPException(422,str(e))
    p=Project(id=uuid.uuid4().hex,url=f'https://www.youtube.com/watch?v={vid}',video_id=vid)
    save_project(p);pipeline.submit(p.id)
    return p


@app.get('/api/projects/{pid}')
def read(pid:str):return project(pid)


@app.put('/api/projects/{pid}')
def update(pid:str,body:Project):
    current=project(pid)
    if current.status in ['processing','queued']:raise HTTPException(409,'분석이 끝난 뒤 수정해 주세요.')
    if current.sync:raise HTTPException(409,'올린 악보로 맞춘 곡은 음표를 고칠 수 없습니다. 원본 악보 파일에서 고쳐 주세요.')
    if body.id!=pid:raise HTTPException(422,'프로젝트 ID가 다릅니다.')
    # Preserve server-owned source/job fields.
    for field in ['url','video_id','source','status','created_at','analysis_seconds','metadata','metrics','sync']:
        setattr(body,field,getattr(current,field))
    for n in body.notes:
        # An unplaced harmonic has no touch fret yet; it is checked once it gets a string.
        if n.string and n.technique=='harmonic' and n.fret not in HARMONICS:raise HTTPException(422,'자연 하모닉스의 터치 프렛은 3, 4, 5, 7, 9, 12를 지원합니다.')
        if n.string and n.technique!='percussion' and sounding_pitch(n,body)!=n.midi:raise HTTPException(422,'음높이와 운지가 일치하지 않습니다. 줄·프렛 또는 카포를 확인해 주세요.')
    if any(b.start<a.end-.02 for a,b in zip(body.bars,body.bars[1:])):raise HTTPException(422,'마디 구간이 겹칩니다.')
    try:return save_project(body,edit=True,expected_revision=body.revision)
    except ValueError as e:raise HTTPException(409,str(e))


@app.delete('/api/projects/{pid}')
def delete(pid:str):
    p=project(pid)
    # A running job keeps writing to the project, so it must stop before its files go away.
    if p.status in ['processing','queued']:raise HTTPException(409,'분석 중인 채보는 삭제할 수 없습니다. 분석을 취소한 뒤 삭제해 주세요.')
    delete_project(pid);pipeline.CANCEL.pop(pid,None)
    return {'status':'deleted'}


@app.post('/api/projects/{pid}/cancel')
def cancel(pid:str):
    p=project(pid);pipeline.cancel(pid)
    return {'status':'cancelling'}


@app.post('/api/projects/{pid}/retry')
def retry(pid:str):
    p=project(pid)
    if p.status in ['processing','queued']:raise HTTPException(409,'이미 분석 중입니다.')
    # Also the way back from a synced score: preparing again returns to the tuning/capo check.
    p.sync=[];p.metadata.pop('score_file',None);pipeline.remove_score(project_dir(pid))
    p.status='queued';p.stage='대기 중';p.progress=0;p.error='';save_project(p);pipeline.submit(pid);return p


class Settings(BaseModel):
    tuning:list[int]=Field(min_length=6,max_length=6)
    capo:int=Field(ge=0,le=12)
    capo_segments:list[CapoSegment]=Field(default_factory=list)


@app.post('/api/projects/{pid}/start')
def start(pid:str,body:Settings):
    """Transcribe with the tuning and capo the player checked against the video."""
    p=project(pid)
    if p.status!='awaiting_settings':raise HTTPException(409,'튜닝·카포를 확인하는 단계가 아닙니다.')
    p.tuning=body.tuning;p.capo=body.capo;p.capo_segments=body.capo_segments
    try:Project.model_validate(p.model_dump())
    except ValueError as e:raise HTTPException(422,str(e))
    detected=p.metadata.get('detected_settings') or {}
    capos=detected.get('capos') or []
    described_capo=capos[0] if capos else 0 if detected.get('tuning') else None
    p.metadata.update(settings_confirmed=True,
        tuning_source='description' if body.tuning==detected.get('tuning') else 'manual',
        capo_source='description' if body.capo==described_capo and not body.capo_segments else 'manual')
    p.sync=[];p.metadata.pop('score_file',None);pipeline.remove_score(project_dir(pid))
    p.status='queued';p.stage='대기 중';p.progress=.15;p.error=''
    save_project(p);pipeline.submit(pid,transcribe=True)
    return p


SCORE_TYPES=['.gp','.gpx','.gp5','.gp4','.gp3']


@app.post('/api/projects/{pid}/score-file')
async def score_file(pid:str,file:UploadFile=File(...)):
    """Time the player's own Guitar Pro file to the media instead of transcribing.

    The file is read here so a bad one is refused at once; the timing runs as a job.
    """
    p=project(pid)
    if p.status!='awaiting_settings':raise HTTPException(409,'튜닝·카포를 확인하는 단계에서 악보를 올려 주세요.')
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in SCORE_TYPES:raise HTTPException(422,'Guitar Pro 악보 파일(.gp, .gpx, .gp5, .gp4, .gp3)을 선택해 주세요.')
    data=await file.read(20*1024*1024+1)
    if len(data)>20*1024*1024:raise HTTPException(413,'악보 파일은 20MB 이하로 선택해 주세요.')
    folder=project_dir(pid)
    with tempfile.TemporaryDirectory(prefix='upload-',dir=folder) as work:
        path=Path(work)/('score'+suffix);path.write_bytes(data)
        result=subprocess.run(['node',str(ROOT/'scripts'/'score-bridge.mjs'),'timeline',str(path)],capture_output=True,text=True,encoding='utf-8',timeout=120,cwd=ROOT)
        if result.returncode==3:raise HTTPException(422,'기타 트랙이 있는 악보를 선택해 주세요.')
        if result.returncode:raise HTTPException(422,'악보를 읽을 수 없습니다. 암호화되지 않은 Guitar Pro 파일을 선택해 주세요.')
        timeline=json.loads(result.stdout)
        if not any(n[1]>0 for n in timeline['notes']):raise HTTPException(422,'악보에 연주할 음표가 없습니다.')
        pipeline.remove_score(folder)
        path.rename(folder/('score'+suffix))
    (folder/'score.timeline.json').write_text(result.stdout,encoding='utf-8')
    tuning=timeline['tuning'];capo=int(timeline['capo'] or 0)
    p.metadata['score_file']={'name':Path(file.filename).name,'file':'score'+suffix,'title':timeline['title'],'artist':timeline['artist'],
        'track':timeline['track'],'track_name':timeline['track_name'],'track_count':timeline['track_count'],
        'tuning':tuning,'capo':capo,'bars':timeline['bar_count'],'tempo':timeline['tempo']}
    # The file names the tuning and capo, so there is nothing left for the player to confirm.
    # A 7-string or bass track keeps the placeholder; its own tuning stays in score_file.
    if len(tuning)==6 and all(24<=n<=84 for n in tuning):p.tuning=tuning
    if 0<=capo<=12:p.capo=capo
    p.capo_segments=[]
    p.metadata.update(settings_confirmed=True,tuning_source='score',capo_source='score')
    p.status='queued';p.stage='대기 중';p.progress=.15;p.error=''
    save_project(p);pipeline.submit(pid,sync=True)
    return p


class Revoice(Settings):
    # The screen shows which notes a setting cannot place before the user applies it. Those
    # notes are kept at their pitch without a string, as they are after a tricky analysis.
    allow_unplayable:bool=False


@app.post('/api/projects/{pid}/revoice')
def revoice(pid:str,body:Revoice):
    p=project(pid)
    if p.status!='ready':raise HTTPException(409,'분석이 끝난 뒤 수정해 주세요.')
    if p.sync:raise HTTPException(409,'올린 악보로 맞춘 곡은 튜닝·카포를 바꿀 수 없습니다.')
    rev=p.revision;p.tuning=body.tuning;p.capo=body.capo;p.capo_segments=body.capo_segments
    try:
        Project.model_validate(p.model_dump());assign_fingering(p,strict=not body.allow_unplayable)
        return save_project(p,edit=True,expected_revision=rev)
    except ValueError as e:raise HTTPException(422,str(e))


@app.get('/api/projects/{pid}/media/{kind}')
def media(pid:str,kind:str):
    project(pid);folder=project_dir(pid)
    name={'video':'source.mp4','audio':'audio.wav','poster':'poster.jpg'}.get(kind)
    if not name or not (folder/name).exists():raise HTTPException(404,'미디어가 없습니다.')
    return FileResponse(folder/name,media_type={'video':'video/mp4','audio':'audio/wav','poster':'image/jpeg'}[kind])


@app.get('/api/projects/{pid}/score/{fmt}')
def export(pid:str,fmt:str,preview:bool=False):
    p=project(pid)
    if fmt=='original':
        # The uploaded file itself: the browser displays it and it is what a synced song exports.
        score=p.metadata.get('score_file')
        path=project_dir(pid)/score['file'] if score else None
        if not path or not path.exists():raise HTTPException(404,'올린 악보가 없습니다.')
        return FileResponse(path,media_type='application/octet-stream',filename=score['name'],headers={'Cache-Control':'no-store'})
    if fmt not in ['gp5','gp','json']:raise HTTPException(404)
    if fmt=='json':return Response(p.model_dump_json(indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="tabsight.json"'})
    try:data=gp5_bytes(p,preview=preview)
    except ValueError as e:raise HTTPException(422,str(e))
    if fmt=='gp':
        with tempfile.TemporaryDirectory(prefix='export-',dir=project_dir(pid)) as work:
            gp5=Path(work)/'export.gp5';gp=Path(work)/'export.gp';gp5.write_bytes(data)
            result=subprocess.run(['node',str(ROOT/'scripts'/'score-bridge.mjs'),'convert',str(gp5),str(gp)],capture_output=True,text=True,timeout=60,cwd=ROOT)
            if result.returncode:raise HTTPException(500,'GP 변환에 실패했습니다: '+result.stderr[-500:])
            data=gp.read_bytes()
    return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="tabsight.{fmt}"','Cache-Control':'no-store'})


@app.post('/api/import')
async def import_file(file:UploadFile=File(...)):
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in ['.gp','.gp5','.gp4','.gp3','.mp4','.mov','.mkv','.webm','.wav','.mp3','.m4a']:
        raise HTTPException(422,'Guitar Pro 악보 또는 영상·음성 파일을 선택해 주세요.')
    pid=uuid.uuid4().hex;folder=project_dir(pid);path=folder/('import'+suffix)
    total=0
    with path.open('wb') as out:
        while chunk:=await file.read(1024*1024):
            total+=len(chunk)
            if total>1024*1024*1024:
                out.close();path.unlink(missing_ok=True);raise HTTPException(413,'파일은 1GB 이하로 선택해 주세요.')
            out.write(chunk)
    if suffix.startswith('.gp'):
        result=subprocess.run(['node',str(ROOT/'scripts'/'score-bridge.mjs'),'inspect',str(path)],capture_output=True,text=True,timeout=60,cwd=ROOT)
        if result.returncode:raise HTTPException(422,'악보를 읽을 수 없습니다. 암호화되지 않은 GP/GP5 파일을 선택해 주세요.')
        p=Project(id=pid,source='score',status='ready',stage='가져온 악보',progress=1,**json.loads(result.stdout))
        p.warnings=['가져온 기존 악보입니다. AI 채보 결과가 아닙니다.']
        if p.metadata.get('track_count',1)>1:p.warnings.append('여러 트랙 중 첫 기타 트랙을 편집용으로 가져왔습니다. 원본 파일은 별도로 보관됩니다.')
        # The editor knows natural harmonics at 3, 4, 5, 7, 9 and 12 only. Any other harmonic (artificial,
        # tapped, another touch fret) would fail every save, so it keeps its fret as a plain note to review.
        other=[n for n in p.notes if n.string and n.technique=='harmonic' and (n.fret not in HARMONICS or sounding_pitch(n,p)!=n.midi)]
        for n in other:
            n.technique='normal';n.midi=sounding_pitch(n,p);n.evidence=[*n.evidence,'harmonic-candidate'];n.reviewed=False;n.confidence=min(n.confidence,.3)
        if other:p.warnings.append(f'이 앱은 3, 4, 5, 7, 9, 12프렛의 자연 하모닉스만 표현합니다. 다른 하모닉스 {len(other)}개는 누르는 프렛의 일반 음으로 가져와 검토할 음에 올렸습니다.')
        save_project(p);return p
    p=Project(id=pid,title=Path(file.filename or '연주').stem,source='file',metadata={'audio_only':suffix in ['.wav','.mp3','.m4a']});save_project(p)
    if suffix in ['.wav','.mp3','.m4a']:
        result=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(path),'-ac','1','-ar','16000',str(folder/'audio.wav')],capture_output=True,timeout=300)
        if result.returncode:
            p.status='error';p.error='음성 파일을 읽을 수 없습니다.';save_project(p);raise HTTPException(422,p.error)
    elif suffix=='.mp4':path.rename(folder/'source.mp4')
    else:
        result=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(path),'-c:v','libx264','-preset','fast','-crf','23','-c:a','aac','-movflags','+faststart',str(folder/'source.mp4')],capture_output=True,timeout=600)
        if result.returncode:
            p.status='error';p.error='영상 파일을 변환할 수 없습니다.';save_project(p);raise HTTPException(422,p.error)
    pipeline.submit(pid);return p


# Recover interrupted jobs explicitly instead of showing an endless spinner after restart.
for summary in list_projects():
    if summary['status'] in ['processing','queued']:
        p=get_project(summary['id']);p.status='error';p.error='이전 실행 중 앱이 종료되었습니다. 다시 분석할 수 있습니다.';save_project(p)

if (ROOT/'dist').exists():
    app.mount('/',StaticFiles(directory=ROOT/'dist',html=True),name='frontend')
