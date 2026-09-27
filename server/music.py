from __future__ import annotations

import itertools
import math
import re
from collections import defaultdict

import numpy as np

from .models import Bar, Note, Project, STANDARD

TUNINGS = {
    'Standard': STANDARD, 'Half step down': [39,44,49,54,58,63],
    'Drop D': [38,45,50,55,59,64], 'DADGAD': [38,45,50,55,57,62],
    'Open D': [38,45,50,54,57,62], 'Open G': [38,43,50,55,59,62],
    'Whole step down': [38,43,48,53,57,62], 'Open C': [36,43,48,55,60,64],
}
HARMONICS={12:12,7:19,5:24,4:28,9:28,3:31}


def metadata_settings(description: str):
    line = next((s for s in description.splitlines() if re.search(r'tun(?:ing|e)\s*[:=]', s, re.I)), '')
    low = line.lower().replace('-', ' ')
    tuning = None
    for name in ['Half step down', 'Whole step down', 'Drop D', 'DADGAD', 'Open D', 'Open G', 'Open C', 'Standard']:
        if name.lower() in low:
            tuning = TUNINGS[name].copy()
            break
    capos = [int(v) for v in re.findall(r'(\d{1,2})\s*(?:th\s*|nd\s*|rd\s*|st\s*)?capo', line, re.I)]
    capos += [int(v) for v in re.findall(r'capo\s*(?:on\s*)?(?:[:=]\s*)?(\d{1,2})', line, re.I)]
    return tuning, sorted(set(v for v in capos if v <= 12)), line


def candidates(midi, tuning, capo):
    return [(6-i, midi-p-capo) for i,p in enumerate(tuning) if 0 <= midi-p-capo <= 24]


def harmonic_candidates(midi,tuning,capo):
    return [(6-i,fret) for i,base in enumerate(tuning) for fret,interval in HARMONICS.items() if midi==base+capo+interval]


def sounding_pitch(n,p):
    interval=HARMONICS.get(n.fret,n.fret) if n.technique=='harmonic' else n.fret
    return p.tuning[6-n.string]+p.capo_at(n.start)+interval


def choose_settings(notes: list[Note], supplied_tuning=None, supplied_capo=None):
    """Compare physical feasibility, rather than treating a key estimate as a tuning."""
    tunings = [supplied_tuning] if supplied_tuning is not None else list(TUNINGS.values())
    capos = [supplied_capo] if supplied_capo is not None else list(range(8))
    best = None
    sample = [n for n in notes if n.technique != 'percussion'][::max(1, len(notes)//500)]
    # Descriptions can be wrong, or refer to an earlier recording. Repeated notes
    # below the described open bass are evidence of a conflict, not noise to drop.
    if supplied_tuning is not None and supplied_capo is not None:
        outside=[n for n in sample if n.end-n.start>=.15 and not candidates(n.midi,supplied_tuning,supplied_capo)]
        if len(outside)>=max(5,round(len(sample)*.015)):
            tunings=list(TUNINGS.values())
    for tuning,capo in itertools.product(tunings, capos):
        cost = 0.
        for n in sample:
            opts = candidates(n.midi,tuning,capo)
            cost += min((f*.025 + (.05 if f else 0) for _,f in opts), default=3)
        cost += .06*capo + (.1 if tuning != STANDARD else 0)
        if supplied_tuning is not None and tuning!=supplied_tuning:
            cost+=.12*sum(abs(a-b) for a,b in zip(tuning,supplied_tuning))
        if best is None or cost < best[0]:
            best = (cost, tuning, capo)
    return best[1], best[2]


def assign_fingering(p: Project, *, strict=False):
    """Beam search over simultaneous notes; a string cannot sound two pitches at once."""
    notes = sorted((n for n in p.notes if n.technique != 'percussion'), key=lambda n:(n.start,n.midi))
    groups = []
    for note in notes:
        if not groups or note.start-groups[-1][0].start > .045:
            groups.append([note])
        else:
            groups[-1].append(note)
    previous_position = 3.
    unplayable = []
    for group in groups:
        # Missing notes are kept explicitly unassigned instead of changing pitch.
        states = [(0., [], set())]
        for n in group:
            capo = p.capo_at(n.start)
            opts = (harmonic_candidates if n.technique=='harmonic' else candidates)(n.midi, p.tuning, capo)
            if not opts:
                unplayable.append(n.id)
                n.string, n.fret, n.confidence = 0,0,.1
                continue
            next_states = []
            for cost, placements, used in states:
                for string,fret in opts:
                    if string in used:
                        continue
                    local = .06*fret + .12*abs(max(1,fret)-previous_position)
                    if fret == 0:
                        local -= .25
                    frets = [f for _,_,f in placements if f > 0] + ([fret] if fret > 0 else [])
                    if frets and max(frets)-min(frets) > 5:
                        local += (max(frets)-min(frets)-5)*1.4
                    next_states.append((cost+local,placements+[(n,string,fret)],used|{string}))
            if next_states:
                states = sorted(next_states,key=lambda s:s[0])[:24]
            else:
                # More simultaneous notes than physically available strings: preserve and flag.
                unplayable.append(n.id)
                n.string,n.fret,n.confidence = 0,0,.1
        if states:
            placements = states[0][1]
            for n,string,fret in placements:
                n.string,n.fret = string,int(fret)
                # This is a review priority, not a calibrated correctness probability.
                n.confidence = min(n.confidence, .58)
            fs = [f for _,_,f in placements if f]
            if fs:
                previous_position = float(np.median(fs))
    if strict and unplayable:
        raise ValueError(f'이 카포·튜닝에서는 {len(unplayable)}개 음을 원음대로 배치할 수 없습니다. 설정을 확인해 주세요.')
    return {'unassigned':len(unplayable)}


def build_bars(notes, duration, tempo=90, beat_times=None, numerator=4, denominator=4):
    if beat_times is not None and len(beat_times) > 8:
        ts = list(map(float,beat_times))
        step = 60/tempo
        while ts[0] > step:
            ts.insert(0,ts[0]-step)
        if ts[0] > .02:
            ts.insert(0,0.)
        while ts[-1] < duration+step*4:
            ts.append(ts[-1]+step)
        return [Bar(start=ts[i],end=ts[i+4],tempo=float(np.clip(240/(ts[i+4]-ts[i]),20,300))) for i in range(0,len(ts)-4,4) if ts[i] < duration]
    step = 60/tempo*numerator*4/denominator
    return [Bar(start=i*step,end=(i+1)*step,tempo=tempo,numerator=numerator,denominator=denominator) for i in range(max(1,math.ceil(duration/step)))]


def suggest_techniques(p: Project, y, sr):
    """Conservative signal/gesture candidates. Not a trained TART classifier.

    Candidate annotations remain unreviewed; the UI exposes this distinction.
    """
    by_string = defaultdict(list)
    for n in sorted(p.notes,key=lambda n:n.start):
        if n.string and n.technique == 'normal':
            by_string[n.string].append(n)
    for notes in by_string.values():
        for prev,n in zip(notes,notes[1:]):
            delta=n.start-prev.end
            if -.08 <= delta <= .06 and 0 < abs(n.fret-prev.fret) <= 4:
                onset = y[int(n.start*sr):int((n.start+.025)*sr)]
                old = y[max(0,int((n.start-.03)*sr)):int(n.start*sr)]
                if len(onset) and len(old) and np.sqrt(np.mean(onset**2)) < np.sqrt(np.mean(old**2))*1.35:
                    prev.technique = 'hammer' if n.fret > prev.fret else 'pull'
                    prev.confidence = min(prev.confidence,.4)
                    prev.evidence.append('technique-candidate')
    for n in p.notes:
        if n.technique != 'normal':
            continue
        segment=y[int(n.start*sr):int(min(n.end,n.start+.16)*sr)]
        if len(segment)<256:
            continue
        if n.end-n.start < .09:
            n.technique='mute'
            n.confidence=min(n.confidence,.35)
            n.evidence.append('technique-candidate')

    # Spectral candidates remain tentative: mixtures and normal plucks can resemble
    # a harmonic or a percussive attack. Do not claim a trained technique classifier.
    import librosa
    for n in p.notes:
        if n.technique!='normal' or n.end-n.start<.18:continue
        segment=y[int((n.start+.03)*sr):int(min(n.end,n.start+.23)*sr)]
        if len(segment)<1024:continue
        spec=np.abs(np.fft.rfft(segment*np.hanning(len(segment))))
        freqs=np.fft.rfftfreq(len(segment),1/sr);f0=440*2**((n.midi-69)/12)
        def power(f):return float(np.max(spec[np.abs(freqs-f)<max(15,f*.025)],initial=0))
        fundamental=power(f0);upper=sum(power(f0*k) for k in [2,3,4])
        all_opts=harmonic_candidates(n.midi,p.tuning,p.capo_at(n.start))
        opts=[o for o in all_opts if o[0]==n.string]
        if all_opts and n.midi>=72 and fundamental>0 and upper/fundamental<.28:
            if opts:
                n.string,n.fret=min(opts,key=lambda o:abs(o[1]-n.fret));n.technique='harmonic'
            else:
                # Do not move the note to an occupied string just to force a
                # harmonic annotation. Keep a review suggestion instead.
                n.evidence.append('harmonic-candidate')
            n.confidence=min(n.confidence,.3);n.evidence.append('technique-candidate')
    # Continuous pitch motion between same-string notes suggests a slide. Inspect
    # a short transition only, and require a monotonic path through several pitches.
    for notes in by_string.values():
        for prev,n in zip(notes,notes[1:]):
            if prev.technique not in ['normal','hammer','pull'] or n.technique=='harmonic':continue
            if abs(n.midi-prev.midi)<2 or n.start-prev.end>.07:continue
            seg=y[max(0,int((n.start-.12)*sr)):int((n.start+.07)*sr)]
            if len(seg)<1536:continue
            f0=librosa.yin(seg,fmin=65,fmax=1200,sr=sr,frame_length=1024,hop_length=128)
            midi=librosa.hz_to_midi(f0);direction=np.sign(n.midi-prev.midi)
            inside=midi[(midi>min(prev.midi,n.midi)-.5)&(midi<max(prev.midi,n.midi)+.5)]
            if len(inside)>=6 and np.ptp(inside)>1.8 and np.mean(np.diff(inside)*direction>-.25)>.8:
                prev.technique='slide';prev.confidence=min(prev.confidence,.3);prev.evidence.append('technique-candidate')
    # Broad-band onsets: attach slap to a nearby pitched attack, otherwise keep a
    # separate low-confidence body-percussion event for explicit user review.
    spectrum=np.abs(librosa.stft(y,n_fft=1024,hop_length=256))
    flat=librosa.feature.spectral_flatness(S=spectrum)[0]
    flux=librosa.onset.onset_strength(S=librosa.amplitude_to_db(spectrum+1e-8),sr=sr,hop_length=256)
    from scipy.signal import find_peaks
    peaks,_=find_peaks(flux,height=max(2,float(np.percentile(flux,94))),distance=round(.15*sr/256))
    import uuid
    for idx in peaks:
        if flat[idx]<.06:continue
        t=max(0,(idx*256-512)/sr)
        near=min(p.notes,key=lambda n:abs(n.start-t),default=None)
        if near and abs(near.start-t)<.07:
            if near.technique=='normal':near.technique='slap';near.confidence=min(near.confidence,.25);near.evidence.append('technique-candidate')
        elif t<p.duration-.1:
            p.notes.append(Note(id=uuid.uuid4().hex[:12],midi=37,start=t,end=t+.1,string=0,fret=0,technique='percussion',confidence=.2,evidence=['audio','technique-candidate']))
