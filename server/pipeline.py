from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs,urlparse

import numpy as np

from .models import Note,Project
from .music import assign_fingering,build_bars,choose_settings,metadata_settings,suggest_techniques
from .store import DATA,ROOT,get_project,project_dir,save_project

EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='tabsight')
CANCEL:dict[str,threading.Event]={}
_MODEL=None


def video_id(url):
    parts=urlparse(url.strip())
    host=(parts.hostname or '').lower()
    if host in ['youtu.be','www.youtu.be']:vid=parts.path.strip('/').split('/')[0]
    elif host in ['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com']:
        vid=parse_qs(parts.query).get('v',[''])[0]
        if not vid and parts.path.startswith(('/shorts/','/embed/')):vid=parts.path.split('/')[2]
    else:raise ValueError('YouTube 영상 링크를 입력해 주세요.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',vid):raise ValueError('유효한 YouTube 영상 링크가 아닙니다.')
    return vid


def submit(pid):
    CANCEL[pid]=threading.Event()
    EXECUTOR.submit(run,pid)


def cancel(pid):
    event=CANCEL.get(pid)
    if event:event.set()


def transcriptor():
    global _MODEL
    if _MODEL is not None:return _MODEL
    import torch
    from huggingface_hub import hf_hub_download
    from piano_transcription_inference import PianoTranscription
    from piano_transcription_inference.models import Regress_onset_offset_frame_velocity_CRNN
    # Load the actual guitar checkpoint, never the placeholder HF wrapper state.
    path=hf_hub_download('xavriley/midi-transcription-models','guitar-gaps-paper-version-12200_iterations.pth',local_dir=str(DATA/'models'/'gaps'))
    torch.set_num_threads(4)
    model=Regress_onset_offset_frame_velocity_CRNN(frames_per_second=100,classes_num=88)
    # The published checkpoint includes numpy metadata as well as tensor weights.
    # Allow only the known numpy constructors; keep arbitrary pickle code disabled.
    with torch.serialization.safe_globals([np.core.multiarray._reconstruct,np.dtype,np.ndarray,type(np.dtype('float32')),type(np.dtype('float64')),type(np.dtype('int64'))]):
        checkpoint=torch.load(path,map_location='cpu',weights_only=True)
    model.load_state_dict(checkpoint['model'],strict=True)
    device='mps' if torch.backends.mps.is_available() else 'cpu'
    model=model.to(device).eval()
    tr=PianoTranscription.__new__(PianoTranscription)
    tr.model=model;tr.segment_samples=160000;tr.frames_per_second=100;tr.classes_num=88
    tr.onset_threshold=.35;tr.offset_threshod=.3;tr.frame_threshold=.15;tr.pedal_offset_threshold=.2;tr.batch_size=1
    _MODEL=(tr,device)
    return _MODEL


def transcribe_audio(y,sr,progress,cancelled):
    import torch
    tr,device=transcriptor()
    device=next(tr.model.parameters()).device.type
    events=[]; chunk=20; overlap=2; duration=len(y)/sr
    for start in np.arange(0,duration,chunk):
        if cancelled():raise InterruptedError('분석을 취소했습니다.')
        a=max(0,float(start)-overlap);b=min(duration,float(start)+chunk+overlap)
        audio=y[round(a*sr):round(b*sr)]
        try:
            with torch.inference_mode():result=tr.transcribe(audio,None)
        except (RuntimeError,NotImplementedError) as exc:
            if device!='mps':raise
            # Unsupported Metal kernels fall back to CPU without changing the model.
            tr.model=tr.model.to('cpu');device='cpu'
            torch.mps.empty_cache()
            with torch.inference_mode():result=tr.transcribe(audio,None)
        for ev in result['est_note_events']:
            onset=float(ev['onset_time'])+a; end=min(duration,float(ev['offset_time'])+a)
            pitch=int(ev['midi_note'])
            if not (start <= onset < min(duration,start+chunk)):continue
            if not (35<=pitch<=96) or end-onset<.06:continue
            frame=min(int(float(ev['onset_time'])*100),len(result['output_dict']['frame_output'])-1)
            confidence=float(result['output_dict']['frame_output'][frame,pitch-21])
            events.append(Note(id=uuid.uuid4().hex[:12],midi=pitch,start=round(onset,4),end=round(end,4),velocity=max(1,min(127,int(ev['velocity']))),confidence=float(np.clip(confidence,.2,.95)),evidence=['audio']))
        progress(min(1,(start+chunk)/duration))
    return events,device


def video_analysis(folder,progress,event):
    progress_file=folder/'vision-progress';progress_file.unlink(missing_ok=True)
    output=folder/'vision-result.json';output.unlink(missing_ok=True)
    with (folder/'vision.log').open('w') as log:
        proc=subprocess.Popen([sys.executable,'-m','server.vision_worker',str(folder)],cwd=ROOT,stdout=log,stderr=log)
        started=time.monotonic();last=-1
        try:
            while proc.poll() is None:
                if event.is_set():raise InterruptedError('분석을 취소했습니다.')
                if time.monotonic()-started>1800:raise TimeoutError('영상 분석 제한 시간을 초과했습니다.')
                try:value=float(progress_file.read_text())
                except (ValueError,FileNotFoundError):value=0
                if value!=last:progress(value);last=value
                time.sleep(.25)
            if proc.returncode:raise RuntimeError(f'손 인식 프로세스 종료 ({proc.returncode}). vision.log를 확인해 주세요.')
        finally:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=10)
    return json.loads(output.read_text())


def run(pid):
    started=time.monotonic();folder=project_dir(pid)
    event=CANCEL.setdefault(pid,threading.Event())
    def update(stage,progress):
        if event.is_set():raise InterruptedError('분석을 취소했습니다.')
        p=get_project(pid);p.status='processing';p.stage=stage;p.progress=float(progress);save_project(p)
    try:
        p=get_project(pid)
        update('영상을 가져오는 중',.03)
        media=folder/'source.mp4';audio=folder/'audio.wav'
        if not media.exists() and not audio.exists():
            import yt_dlp
            def hook(d):
                if event.is_set():raise InterruptedError('분석을 취소했습니다.')
            opts={'format':'bv[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/best',
                  'outtmpl':str(folder/'source.%(ext)s'),'merge_output_format':'mp4','noplaylist':True,
                  'quiet':True,'no_warnings':True,'socket_timeout':20,'retries':2,'progress_hooks':[hook],
                  'writeinfojson':True}
            with yt_dlp.YoutubeDL(opts) as ydl:info=ydl.extract_info(p.url,download=True)
            p=get_project(pid);p.title=info.get('title',p.title);p.duration=info.get('duration',0)
            p.metadata={'description':info.get('description',''),'channel':info.get('channel',''),'thumbnail':info.get('thumbnail','')}
            if not media.exists():
                source=next((f for f in folder.iterdir() if f.suffix in ['.mp4','.mkv','.webm']),None)
                if source:source.rename(media)
            save_project(p)
        update('소리를 준비하는 중',.12)
        if media.exists():
            subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(media),'-vn','-ac','1','-ar','16000',str(audio)],check=True,timeout=300,capture_output=True)
            subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-ss','10','-i',str(media),'-frames:v','1',str(folder/'poster.jpg')],timeout=60,capture_output=True)
        import soundfile as sf
        import librosa
        y,sr=sf.read(audio,dtype='float32')
        if y.ndim>1:y=y.mean(axis=1)
        if sr!=16000:y=librosa.resample(y,orig_sr=sr,target_sr=16000);sr=16000
        duration=len(y)/sr
        if duration<1:raise ValueError('분석할 연주 구간이 너무 짧습니다.')
        update('기타 음표를 듣는 중 · 첫 실행은 모델을 준비합니다',.18)
        notes,device=transcribe_audio(y,sr,lambda f:update('기타 음표를 듣는 중',.18+.38*f),event.is_set)
        if not notes:raise ValueError('기타 음표를 찾지 못했습니다. 연주가 잘 들리는 영상을 사용해 주세요.')
        update('박자와 마디를 정리하는 중',.58)
        env=librosa.onset.onset_strength(y=y,sr=sr,hop_length=256)
        tempo,beats=librosa.beat.beat_track(onset_envelope=env,sr=sr,hop_length=256,trim=False)
        tempo=float(np.clip(np.asarray(tempo).reshape(-1)[0],30,240))
        beat_times=librosa.frames_to_time(beats,sr=sr,hop_length=256)
        p=get_project(pid);p.notes=notes;p.duration=duration;p.tempo=tempo
        p.bars=build_bars(notes,duration,tempo,beat_times)
        tuning,capos,line=metadata_settings(p.metadata.get('description',''))
        p.tuning,p.capo=choose_settings(notes,{},tuning,capos[0] if capos else None)
        p.metadata['settings_source']='description' if line else 'audio-inference'
        p.metadata['tuning_source']='description' if tuning else 'audio-inference'
        p.metadata['capo_source']='description' if capos else 'audio-inference'
        settings_conflict=tuning is not None and p.tuning!=tuning
        if settings_conflict:
            p.metadata['settings_source']='description-conflict'
            p.metadata['tuning_source']='audio-inference'
        p.metadata['settings_text']=line
        p.warnings=['줄·프렛 및 특수 주법은 추정 결과입니다. 검토 표시를 확인해 주세요.', '박자·마디 시작은 자동 추정입니다. 루바토와 못갖춘마디는 보정이 필요할 수 있습니다.']
        if not line:p.warnings.append('카포·튜닝을 확정할 설명 정보가 없어 음역으로 추정했습니다. 설정을 확인해 주세요.')
        elif not capos:p.warnings.append('튜닝은 영상 설명을 참고했고, 카포는 음역과 가능한 운지로 추정했습니다.')
        if settings_conflict:p.warnings.append('영상 설명의 튜닝으로 연주할 수 없는 저음이 반복되어 다른 튜닝을 제안했습니다. 원음과 비교해 설정을 확인해 주세요.')
        if len(capos)>1:p.warnings.append(f'설명에 카포 {", ".join(map(str,capos))}프렛이 있습니다. 구간별 카포 변경을 확인해 주세요.')
        assign_fingering(p,use_vision=False)
        audio_only=[n.model_dump() for n in p.notes]
        (folder/'audio-only.json').write_text(json.dumps(audio_only,ensure_ascii=False))
        save_project(p)
        if media.exists():
            update('손 움직임과 지판을 살펴보는 중',.63)
            try:
                p.vision=video_analysis(folder,lambda f:update('손 움직임과 지판을 살펴보는 중',.63+.24*f),event)
            except InterruptedError:raise
            except Exception as exc:
                p.warnings.append(f'영상 분석을 완료하지 못해 음성 초안을 표시합니다: {type(exc).__name__}')
                p.vision={'error':str(exc),'frames':[]}
        update('운지와 연주 기법을 정리하는 중',.9)
        stats=assign_fingering(p,use_vision=True)
        changed=sum((a['string'],a['fret'])!=(b.string,b.fret) for a,b in zip(audio_only,p.notes))
        suggest_techniques(p,y,sr)
        p.warnings.append('특수 주법 자동 표기는 실험적입니다. 원본 연주와 비교해 수정해 주세요.')
        p.warnings.append('파일의 리듬은 64분음표 단위까지 표현합니다. 같은 줄의 잔향은 다음 음이 시작할 때 끝나도록 정리합니다.')
        p.metrics={'audio_model':'GAPS paper checkpoint (2024)','device':device,**stats,'changed_by_vision':changed,
                   'hand_detection_rate':p.vision.get('hand_detection_rate',0),'board_detection_rate':p.vision.get('board_detection_rate',0),
                   'accuracy_verified':False,'note_count':len(p.notes)}
        if not stats['vision_assisted_notes']:p.warnings.append('줄·프렛에 사용할 지판 위치를 충분히 읽지 못했습니다. 지판 보정 후 다시 운지를 계산할 수 있습니다.')
        if stats['unassigned']:p.warnings.append(f'{stats["unassigned"]}개 음의 운지가 미정입니다. 내보내기 전에 수정해 주세요.')
        if event.is_set():raise InterruptedError('분석을 취소했습니다.')
        p.analysis_seconds=round(time.monotonic()-started,2);p.status='ready';p.stage='채보 초안 준비 완료';p.progress=1
        save_project(p)
        (folder/'analysis-report.json').write_text(json.dumps({'seconds':p.analysis_seconds,**p.metrics},ensure_ascii=False,indent=2))
    except InterruptedError:
        p=get_project(pid);p.status='cancelled';p.stage='분석 취소';save_project(p)
    except Exception as exc:
        traceback.print_exc()
        p=get_project(pid);p.status='error';p.error=str(exc);p.stage='분석을 완료하지 못했습니다';save_project(p)
