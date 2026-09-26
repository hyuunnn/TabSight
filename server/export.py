from __future__ import annotations

import io
import math
from collections import defaultdict

import guitarpro
from guitarpro import models as g

from .models import Project
from .music import sounding_pitch,HARMONICS


def durations(ticks):
    choices=[]
    for value in [1,2,4,8,16,32,64]:
        base=3840//value
        choices.extend([(base,value,False),(base*3//2,value,True)])
    choices.sort(reverse=True)
    while ticks>=60:
        amount,value,dot=next(v for v in choices if v[0]<=ticks)
        yield amount,g.Duration(value=value,isDotted=dot)
        ticks-=amount


def gp5_bytes(p: Project, preview=False):
    for n in p.notes:
        if n.string and n.technique!='percussion' and sounding_pitch(n,p)!=n.midi:
            raise ValueError('음높이와 운지가 일치하지 않는 음표가 있습니다. 줄·프렛을 확인해 주세요.')
        if n.technique=='harmonic' and n.fret not in HARMONICS:
            raise ValueError('자연 하모닉스는 3, 4, 5, 7, 9, 12프렛을 지원합니다.')
    unresolved=[n for n in p.notes if not n.string and n.technique!='percussion']
    if unresolved and not preview:
        raise ValueError(f'운지가 미정인 음이 {len(unresolved)}개 있습니다. 검토 목록에서 줄·프렛을 지정한 뒤 내보내 주세요.')
    if not preview:
        seen=set()
        for n in p.notes:
            if n.technique=='percussion':continue
            bi=next((i for i,b in enumerate(p.bars) if b.start<=n.start<b.end),None)
            if bi is None:raise ValueError('마디 범위 밖에 시작하는 음표가 있습니다. 마디 또는 음표 시작 시간을 보정해 주세요.')
            bar=p.bars[bi];length=round(3840*bar.numerator/bar.denominator)
            tick=max(0,min(length-60,round((n.start-bar.start)/(bar.end-bar.start)*length/60)*60))
            key=(bi,n.string,tick)
            if key in seen:raise ValueError(f'{bi+1}마디 {n.string}번 줄에 너무 가까이 겹친 음표가 있습니다. 시작 시간이나 운지를 보정해 주세요.')
            seen.add(key)
    song=g.Song(title=p.title,artist=p.metadata.get('channel',''),tempo=round(p.bars[0].tempo if p.bars else p.tempo),tab='TabSight')
    song.notice=['Automatic transcription draft. Review suggested fingerings and techniques.',p.url]
    song.tracks=[];song.measureHeaders=[]
    capos=sorted(set([p.capo]+[s.capo for s in p.capo_segments]))
    track_specs=[(capo,False) for capo in capos]
    if any(n.technique=='percussion' for n in p.notes):track_specs.append((0,True))
    for ti,(capo,perc) in enumerate(track_specs):
        track=g.Track(song,number=ti+1,name='Body percussion' if perc else (f'Guitar · Capo {capo}' if len(capos)>1 else 'Acoustic Guitar'),offset=capo,isPercussionTrack=perc)
        track.measures=[]
        track.strings=[g.GuitarString(i+1,v) for i,v in enumerate(reversed(p.tuning))]
        track.channel=g.MidiChannel(channel=9 if perc else ti if ti<9 else ti+1,effectChannel=9 if perc else ti if ti<9 else ti+1,instrument=0 if perc else 25)
        song.tracks.append(track)
    start_tick=960
    previous_active=[set() for _ in track_specs]
    # A rearticulation stops the previous note on that physical string. Keeping
    # the old span active would incorrectly resurrect it after the new note ends.
    ends={n.id:n.end for n in p.notes}
    for string in range(1,7):
        sequence=sorted((n for n in p.notes if n.string==string and n.technique!='percussion'),key=lambda n:n.start)
        for current,nxt in zip(sequence,sequence[1:]):ends[current.id]=min(current.end,nxt.start)
    for bi,bar in enumerate(p.bars):
        length=round(3840*bar.numerator/bar.denominator)
        header=g.MeasureHeader(number=bi+1,start=start_tick,timeSignature=g.TimeSignature(numerator=bar.numerator,denominator=g.Duration(value=bar.denominator)))
        song.measureHeaders.append(header)
        for ti,((capo,perc),track) in enumerate(zip(track_specs,song.tracks)):
            measure=g.Measure(track,header);track.measures.append(measure);voice=measure.voices[0]
            voice.beats=[]
            selected=[n for n in p.notes if n.start<bar.end and ends[n.id]>bar.start and ((n.technique=='percussion')==perc) and (perc or (n.string and p.capo_at(n.start)==capo))]
            spans=[]
            for n in selected:
                a=max(0,min(length-60,round((n.start-bar.start)/(bar.end-bar.start)*length/60)*60))
                b=max(a+60,min(length,round((ends[n.id]-bar.start)/(bar.end-bar.start)*length/60)*60))
                spans.append((n,int(a),int(b)))
            boundaries=sorted({0,length}|{a for _,a,_ in spans}|{b for _,_,b in spans})
            cursor=0
            for a,b in zip(boundaries,boundaries[1:]):
                active={}
                for n,ns,ne in spans:
                    if ns<=a<ne:
                        key=n.midi if perc else n.string
                        if key not in active or n.start>active[key].start:active[key]=n
                for amount,duration in durations(b-a):
                    beat=g.Beat(voice,duration=duration,start=start_tick+cursor,status=g.BeatStatus.normal if active else g.BeatStatus.rest)
                    if cursor==0:
                        beat.effect.mixTableChange=g.MixTableChange(tempo=g.MixTableItem(round(bar.tempo)))
                    for n in active.values():
                        effect=g.NoteEffect()
                        tied=n.id in previous_active[ti]
                        if not tied and n.technique in ['hammer','pull']:effect.hammer=True
                        if not tied and n.technique=='slide':effect.slides=[g.SlideType.shiftSlideTo]
                        if n.technique=='harmonic':
                            effect.harmonic=g.NaturalHarmonic()
                        if n.technique=='mute':effect.palmMute=True
                        if n.technique=='vibrato':effect.vibrato=True
                        if n.technique=='bend':effect.bend=g.BendEffect(type=g.BendType.bend,value=50,points=[g.BendPoint(0,0),g.BendPoint(12,50)])
                        if n.technique=='slap':beat.effect.slapEffect=g.SlapEffect.slapping
                        if not tied and not n.reviewed and 'harmonic-candidate' in n.evidence:beat.text='Harmonic?'
                        value=n.midi if perc else n.fret
                        note=g.Note(beat,value=value,string=1 if perc else n.string,velocity=n.velocity,effect=effect,type=g.NoteType.tie if tied else g.NoteType.normal)
                        beat.notes.append(note)
                    voice.beats.append(beat)
                    previous_active[ti]={n.id for n in active.values()}
                    cursor+=amount
            # Empty second voice is intentional: no duplicate playback.
        start_tick+=length
    if not p.bars:
        raise ValueError('내보낼 악보가 없습니다.')
    out=io.BytesIO()
    guitarpro.write(song,out,version=(5,1,0),encoding='utf-8')
    return out.getvalue()
