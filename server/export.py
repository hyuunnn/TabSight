from __future__ import annotations

import io
from collections import defaultdict

import guitarpro
from guitarpro import models as g

from .models import Project
from .music import sounding_pitch,HARMONICS
from .rhythm import bar_length,beat_count,quantize


def durations(ticks,triplet=False):
    """Note values that exactly fill `ticks`: straight or dotted, or triplet values inside a triplet beat."""
    if triplet:
        for amount,value in [(640,4),(320,8),(160,16),(80,32)]:
            while ticks>=amount:
                yield amount,g.Duration(value=value,tuplet=g.Tuplet(3,2))
                ticks-=amount
        return
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
    if not p.bars:
        raise ValueError('내보낼 악보가 없습니다.')
    written=[n for n in p.notes if n.string or n.technique=='percussion']
    timing=quantize(p,written)
    if not preview:
        seen={}
        for n in written:
            if n.technique=='percussion':continue
            if n.id not in timing.notes:raise ValueError('마디 범위 밖에 시작하는 음표가 있습니다. 마디 또는 음표 시작 시간을 보정해 주세요.')
            key=(n.string,timing.notes[n.id][0])
            if key in seen:
                bi=max(i for i,t in enumerate(timing.bar_ticks) if t<=key[1])
                raise ValueError(f'{bi+1}마디 {n.string}번 줄에 너무 가까이 겹친 음표가 있습니다. 시작 시간이나 운지를 보정해 주세요.')
            seen[key]=n.id
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
    members=defaultdict(list)
    for n in written:
        if n.id not in timing.notes:continue
        for ti,(capo,perc) in enumerate(track_specs):
            if (n.technique=='percussion')==perc and (perc or p.capo_at(n.start)==capo):
                members[ti].append(n)
    previous_active=defaultdict(set)
    second_voice=set()
    for bi,bar in enumerate(p.bars):
        lo=timing.bar_ticks[bi];length=bar_length(bar);hi=lo+length
        header=g.MeasureHeader(number=bi+1,start=lo,timeSignature=g.TimeSignature(numerator=bar.numerator,denominator=g.Duration(value=bar.denominator)))
        song.measureHeaders.append(header)
        beat=length//beat_count(bar)
        straight=beat_count(bar)==bar.numerator
        # Triplet beats keep their own boundaries so each triplet group is complete.
        triplets=[(lo+k*beat,lo+(k+1)*beat) for k,grid in enumerate(timing.grids[bi]) if straight and grid in (3,6)]
        for ti,((capo,perc),track) in enumerate(zip(track_specs,song.tracks)):
            measure=g.Measure(track,header);track.measures.append(measure)
            for vi,voice in enumerate(measure.voices):
                voice.beats=[]
                if vi and (perc or not timing.two_voices[bi]):
                    if ti in second_voice:
                        # alphaTab (1.8.4) fails to read a used second voice followed by an empty one,
                        # so later bars carry invisible placeholder beats instead of nothing.
                        cursor=lo
                        for amount,duration in durations(length):
                            voice.beats.append(g.Beat(voice,duration=duration,start=cursor,status=g.BeatStatus.empty));cursor+=amount
                    continue
                if vi:second_voice.add(ti)
                spans=[(n,a,b) for n in members[ti] for a,b,v in [timing.notes[n.id]] if (v==vi or perc) and a<hi and b>lo]
                boundaries=sorted({lo,hi}|{x for _,a,b in spans for x in (a,b) if lo<x<hi}|{x for r in triplets for x in r})
                for a,b in zip(boundaries,boundaries[1:]):
                    active={}
                    for n,ns,ne in spans:
                        if ns<=a<ne:
                            key=n.midi if perc else n.string
                            if key not in active or n.start>active[key].start:active[key]=n
                    triplet=any(x<=a and b<=y for x,y in triplets)
                    cursor=a
                    for amount,duration in durations(b-a,triplet):
                        event=g.Beat(voice,duration=duration,start=cursor,status=g.BeatStatus.normal if active else g.BeatStatus.rest)
                        if cursor==lo and vi==0:
                            event.effect.mixTableChange=g.MixTableChange(tempo=g.MixTableItem(round(bar.tempo)))
                        for n in active.values():
                            effect=g.NoteEffect()
                            tied=n.id in previous_active[(ti,vi)]
                            if not tied and n.technique in ['hammer','pull']:effect.hammer=True
                            if not tied and n.technique=='slide':effect.slides=[g.SlideType.shiftSlideTo]
                            if n.technique=='harmonic':
                                effect.harmonic=g.NaturalHarmonic()
                            if n.technique=='mute':effect.palmMute=True
                            if n.technique=='vibrato':effect.vibrato=True
                            if n.technique=='bend':effect.bend=g.BendEffect(type=g.BendType.bend,value=50,points=[g.BendPoint(0,0),g.BendPoint(12,50)])
                            if n.technique=='slap':event.effect.slapEffect=g.SlapEffect.slapping
                            if not tied and not n.reviewed and 'harmonic-candidate' in n.evidence:event.text='Harmonic?'
                            value=n.midi if perc else n.fret
                            event.notes.append(g.Note(event,value=value,string=1 if perc else n.string,velocity=n.velocity,effect=effect,type=g.NoteType.tie if tied else g.NoteType.normal))
                        voice.beats.append(event)
                        previous_active[(ti,vi)]={n.id for n in active.values()}
                        cursor+=amount
    out=io.BytesIO()
    guitarpro.write(song,out,version=(5,1,0),encoding='utf-8')
    return out.getvalue()
