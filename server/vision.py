from __future__ import annotations

import math
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from .store import DATA

HAND_URL = 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task'


def model_path():
    path = DATA / 'models' / 'hand_landmarker.task'
    if not path.exists():
        temp = path.with_suffix('.part')
        urllib.request.urlretrieve(HAND_URL, temp)
        temp.replace(path)
    return path


def automatic_board(frame, hands):
    """Find a perspective fret grid near the fretting hand using line geometry.

    Conservative fallback: no grid is better than fabricated string/fret evidence.
    Coordinates use nut-center and fret-12-center; no per-finger certainty implied.
    """
    h,w=frame.shape[:2]
    if not hands:
        return None
    tips=np.array([[p[0]*w,p[1]*h] for hand in hands for p in [hand[8],hand[12],hand[16],hand[20]]])
    edges=cv2.Canny(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),70,160)
    lines=cv2.HoughLinesP(edges,1,np.pi/360,45,minLineLength=int(w*.12),maxLineGap=12)
    if lines is None:
        return None
    ranked=[]
    for raw in lines[:,0]:
        a,b=raw[:2].astype(float),raw[2:].astype(float)
        if a[0]>b[0]: a,b=b,a
        axis=b-a; length=np.linalg.norm(axis)
        if abs(axis[1])>abs(axis[0])*.7: continue
        axis/=length
        norm=np.array([-axis[1],axis[0]])
        nearby=((tips-a)@axis > -30)&((tips-a)@axis < length+30)
        distance=np.abs((tips-a)@norm)
        count=np.sum(nearby&(distance<35))
        if count>=2: ranked.append((length+count*20,a,b,axis,norm))
    if not ranked: return None
    _,a,b,axis,norm=max(ranked,key=lambda v:v[0])
    # Perpendicular fret wires must be visible as a sequence with increasing spacing.
    short=cv2.HoughLinesP(edges,1,np.pi/360,14,minLineLength=max(8,int(w*.013)),maxLineGap=3)
    if short is None: return None
    frets=[]; widths=[]
    for raw in short[:,0]:
        c,d=raw[:2].astype(float),raw[2:].astype(float)
        vec=d-c; length=np.linalg.norm(vec)
        if length<1 or abs((vec/length)@axis)>.3:continue
        mid=(c+d)/2
        x=(mid-a)@axis; y=(mid-a)@norm
        if -10<x<np.linalg.norm(b-a)+15 and abs(y)<28 and length<65:
            frets.append(float(x));widths.append(length)
    if len(frets)<5:return None
    coords=[]
    for x in sorted(frets):
        if not coords or x-coords[-1]>4:coords.append(x)
    if len(coords)<5:return None
    gaps=np.diff(coords)
    # Reject grids with missing wires or unrelated background line patterns.
    if max(gaps)>min(gaps)*2.6 or min(gaps)<6:return None
    reverse=gaps[0]>gaps[-1]
    nut_x=coords[0]-gaps[0] if reverse else coords[-1]+gaps[-1]
    first_gap=gaps[0] if reverse else gaps[-1]
    scale=first_gap/(1-2**(-1/12))
    toward=1 if reverse else -1
    width=float(np.clip(np.median(widths),14,45))
    center_offset=float(np.median((tips-a)@norm))
    center_offset=float(np.clip(center_offset,-width/2,width/2))
    nut=a+nut_x*axis+center_offset*norm
    fret12=nut+toward*scale*.5*axis
    return {'nut':[float(nut[0]/w),float(nut[1]/h)],'fret12':[float(fret12[0]/w),float(fret12[1]/h)],'width':width/w,'source':'automatic','confidence':.35}


def finger_positions(hands, board, aspect):
    if not board:return []
    nut=np.array(board['nut'],float)*[aspect,1]
    f12=np.array(board['fret12'],float)*[aspect,1]
    axis=f12-nut; half=np.linalg.norm(axis)
    if half<.02:return []
    axis/=half;normal=np.array([-axis[1],axis[0]])
    width=board.get('width',.035)*aspect
    positions=[]
    for hand in hands:
        for finger in [4,8,12,16,20]:
            point=np.array(hand[finger][:2])*[aspect,1]
            along=(point-nut)@axis
            across=(point-nut)@normal
            if along < 0 or along > half*1.5 or abs(across)>width*.9:continue
            fret=-12*math.log2(max(.1,1-along/(half*2)))
            # A finger presses just behind a fret. Keep the observation continuous.
            string=float(np.clip(3.5+across/width*5,1,6))
            positions.append({'fret':float(np.clip(math.ceil(fret),0,24)),'string':string,'finger':finger,'confidence':board.get('confidence',.7)})
    return positions


def analyse_video(path: Path, progress, cancelled, calibration=None):
    import mediapipe as mp
    opts=mp.tasks.vision.HandLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path())),
        running_mode=mp.tasks.vision.RunningMode.VIDEO, num_hands=2,
        min_hand_detection_confidence=.35,min_hand_presence_confidence=.35,min_tracking_confidence=.4)
    cap=cv2.VideoCapture(str(path))
    fps=cap.get(cv2.CAP_PROP_FPS) or 30
    count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));step=max(1,round(fps/2))
    aspect=(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 16)/(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 9)
    frames=[]; boards=[];detected=0
    try:
        with mp.tasks.vision.HandLandmarker.create_from_options(opts) as detector:
            idx=0
            while True:
                if cancelled():raise InterruptedError('분석을 취소했습니다.')
                ok,frame=cap.read()
                if not ok:break
                if idx%step:
                    idx+=1;continue
                if frame.shape[1]>960:
                    frame=cv2.resize(frame,(960,round(frame.shape[0]*960/frame.shape[1])))
                rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
                result=detector.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,data=rgb),round(idx/fps*1000))
                hands=[[[float(lm.x),float(lm.y),float(lm.z)] for lm in hand] for hand in result.hand_landmarks]
                if hands:detected+=1
                board=calibration or automatic_board(frame,hands)
                if board:boards.append(board)
                positions=finger_positions(hands,board,frame.shape[1]/frame.shape[0])
                frames.append({'time':round(idx/fps,3),'hands':hands,'positions':positions,'board':board})
                if len(frames)%12==0:progress(idx/max(1,count))
                idx+=1
    finally:
        cap.release()
    return {'frames':frames,'aspect':aspect,'hand_detection_rate':detected/max(1,len(frames)),
            'board_detection_rate':len(boards)/max(1,len(frames)),
            'model':'MediaPipe Hand Landmarker','calibration':calibration,
            'note':'지판 자동 검출은 실험적이며 줄·프렛의 정답을 보장하지 않습니다.'}
