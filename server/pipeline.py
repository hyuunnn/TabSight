from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs,urlparse

import numpy as np

from .models import Note,Project,STANDARD
from .music import assign_fingering,build_bars,choose_settings,metadata_settings,suggest_techniques,tuning_name
from .store import DATA,get_project,project_dir,save_project

EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='tabsight')
# Preparing media ends at the player's tuning/capo check, so it must not wait behind another
# song's transcription. The model itself still runs one song at a time on EXECUTOR.
PREPARE=ThreadPoolExecutor(max_workers=1,thread_name_prefix='tabsight-prepare')
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


def submit(pid,transcribe=False):
    """Queue media preparation, or transcription once the player has confirmed tuning and capo."""
    CANCEL[pid]=threading.Event()
    (EXECUTOR if transcribe else PREPARE).submit(run,pid,transcribe)


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


def _extract_audio(folder):
    media=folder/'source.mp4'
    subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(media),'-vn','-ac','1','-ar','16000',str(folder/'audio.wav')],check=True,timeout=300,capture_output=True)
    subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-ss','10','-i',str(media),'-frames:v','1',str(folder/'poster.jpg')],timeout=60,capture_output=True)


def _prepare(pid,folder,update,event):
    """Fetch the media and read the description, then stop until the player confirms tuning and capo."""
    started=time.monotonic()
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
    if media.exists():_extract_audio(folder)
    import soundfile as sf
    duration=sf.info(str(audio)).duration
    if duration<1:raise ValueError('분석할 연주 구간이 너무 짧습니다.')
    tuning,capos,line=metadata_settings(p.metadata.get('description',''))
    if event.is_set():raise InterruptedError('분석을 취소했습니다.')
    p=get_project(pid)
    # The screen quotes what the description said next to the fields it filled in.
    p.metadata['detected_settings']={'tuning':tuning,'capos':capos,'text':line.strip()}
    if not p.metadata.get('settings_confirmed'):
        # A description that names the tuning but no capo means no capo. Guessing it from the
        # pitch range picked capos the player never used (capo 4 on a baritone song). Without a
        # named tuning the screen asks the player, so the tuning stored here is a placeholder.
        p.tuning=tuning or STANDARD.copy();p.capo=capos[0] if capos else 0;p.capo_segments=[]
    p.metadata['prepare_seconds']=round(time.monotonic()-started,2)
    p.duration=duration;p.status='awaiting_settings';p.stage='튜닝·카포를 확인해 주세요';p.progress=.15
    save_project(p)


def _transcribe(pid,folder,update,event):
    """Transcribe with the tuning and capo the player confirmed; a guess never replaces them."""
    started=time.monotonic();audio=folder/'audio.wav'
    update('기타 음표를 듣는 중 · 첫 실행은 모델을 준비합니다',.18)
    if not audio.exists() and (folder/'source.mp4').exists():_extract_audio(folder)
    if not audio.exists():raise ValueError('준비된 음성이 없습니다. 다시 분석해 주세요.')
    import soundfile as sf
    import librosa
    y,sr=sf.read(audio,dtype='float32')
    if y.ndim>1:y=y.mean(axis=1)
    if sr!=16000:y=librosa.resample(y,orig_sr=sr,target_sr=16000);sr=16000
    duration=len(y)/sr
    # Transcription takes most of the analysis time, so it gets most of the progress bar.
    notes,device=transcribe_audio(y,sr,lambda f:update('기타 음표를 듣는 중',.18+.67*f),event.is_set)
    if not notes:raise ValueError('기타 음표를 찾지 못했습니다. 연주가 잘 들리는 영상을 사용해 주세요.')
    update('박자와 마디를 정리하는 중',.86)
    env=librosa.onset.onset_strength(y=y,sr=sr,hop_length=256)
    tempo,beats=librosa.beat.beat_track(onset_envelope=env,sr=sr,hop_length=256,trim=False)
    tempo=float(np.clip(np.asarray(tempo).reshape(-1)[0],30,240))
    beat_times=librosa.frames_to_time(beats,sr=sr,hop_length=256)
    p=get_project(pid);p.notes=notes;p.duration=duration;p.tempo=tempo
    p.bars=build_bars(notes,duration,tempo,beat_times)
    p.warnings=['줄·프렛 및 특수 주법은 추정 결과입니다. 검토 표시를 확인해 주세요.', '박자·마디 시작은 자동 추정입니다. 루바토와 못갖춘마디는 보정이 필요할 수 있습니다.']
    capos=(p.metadata.get('detected_settings') or {}).get('capos') or []
    if len(capos)>1 and not p.capo_segments:p.warnings.append(f'설명에 카포 {", ".join(map(str,capos))}프렛이 있습니다. 구간별 카포 변경을 확인해 주세요.')
    update('운지와 연주 기법을 정리하는 중',.9)
    stats=assign_fingering(p)
    p.metadata.pop('suggested_tuning',None)
    if stats['unassigned']:
        # Descriptions can be wrong or describe an earlier recording, and a player may confirm one
        # without checking. Repeated out-of-reach notes suggest another tuning, but the confirmed
        # one stays: those notes keep their pitch without a string, for review.
        suggested,_=choose_settings(p.notes,p.tuning,p.capo)
        if suggested!=p.tuning:
            p.metadata['suggested_tuning']=suggested
            p.warnings.append(f'확인한 튜닝·카포로 낼 수 없는 음이 반복됩니다. 카포가 맞다면 소리로는 {tuning_name(suggested)} 튜닝이 더 맞아 보입니다. 원음과 비교해 튜닝·카포를 확인해 주세요.')
    suggest_techniques(p,y,sr)
    p.warnings.append('특수 주법 자동 표기는 실험적입니다. 원본 연주와 비교해 수정해 주세요.')
    p.warnings.append('파일의 리듬은 64분음표 단위까지 표현합니다. 같은 줄의 잔향은 다음 음이 시작할 때 끝나도록 정리합니다.')
    p.metrics={'audio_model':'GAPS paper checkpoint (2024)','device':device,**stats,
               'accuracy_verified':False,'note_count':len(p.notes)}
    if stats['unassigned']:p.warnings.append(f'{stats["unassigned"]}개 음의 운지가 미정입니다. 내보내기 전에 수정해 주세요.')
    if event.is_set():raise InterruptedError('분석을 취소했습니다.')
    # Time spent in a queue or waiting for the player's confirmation is not analysis time.
    p.analysis_seconds=round(p.metadata.get('prepare_seconds',0)+time.monotonic()-started,2)
    p.status='ready';p.stage='채보 초안 준비 완료';p.progress=1
    save_project(p)
    (folder/'analysis-report.json').write_text(json.dumps({'seconds':p.analysis_seconds,**p.metrics},ensure_ascii=False,indent=2))


def run(pid,transcribe=False):
    folder=project_dir(pid)
    event=CANCEL.setdefault(pid,threading.Event())
    def update(stage,progress):
        if event.is_set():raise InterruptedError('분석을 취소했습니다.')
        p=get_project(pid);p.status='processing';p.stage=stage;p.progress=float(progress);save_project(p)
    try:(_transcribe if transcribe else _prepare)(pid,folder,update,event)
    except InterruptedError:
        p=get_project(pid);p.status='cancelled';p.stage='분석 취소';save_project(p)
    except Exception as exc:
        traceback.print_exc()
        p=get_project(pid);p.status='error';p.error=str(exc);p.stage='분석을 완료하지 못했습니다';save_project(p)
