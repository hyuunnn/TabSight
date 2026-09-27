"""Beats, bars and written note values.

Beat This! (Foscarin, Schlüter, Widmer, ISMIR 2024; MIT code and weights) finds beats and how likely
each is a downbeat. A small Viterbi over bar positions turns those into bars of 2, 3 or 4 beats,
including a short first bar when the music starts before a downbeat. Note onsets decide whether
beats divide in two (x/4) or three (6/8, 9/8, 12/8). For writing a file, each beat snaps its
onsets to the simplest grid that fits them (straight or triplet), and notes last until the next
onset in their voice, so fingerstyle bass and melody read as two voices.
"""
from __future__ import annotations

import math
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from .models import Bar, Project
from .store import DATA

BEAT_CHECKPOINT = 'https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt'
_TRACKER = None
FPS = 50
QUARTER = 960


# ---------------------------------------------------------------- beat tracking

def beat_tracker():
    """Beat This! final0 on the Apple GPU when possible, weights cached under .data/models."""
    global _TRACKER
    if _TRACKER is None:
        import torch
        from beat_this.inference import Audio2Frames
        path = DATA / 'models' / 'beat-this' / 'final0.ckpt'
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix('.part')
            with urllib.request.urlopen(BEAT_CHECKPOINT, timeout=60) as response, partial.open('wb') as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            partial.replace(path)
        device = 'mps' if torch.backends.mps.is_available() else 'cpu'
        _TRACKER = Audio2Frames(str(path), device=device)
    return _TRACKER


def track_beats(y, sr):
    """Beat times and, for each beat, the probability that it starts a bar."""
    import torch
    from beat_this.model.postprocessor import Postprocessor
    tracker = beat_tracker()
    try:
        beat_logits, down_logits = tracker(y.astype(np.float64), sr)
    except (RuntimeError, NotImplementedError):
        # Unsupported Metal kernels fall back to CPU without changing the model.
        tracker.model.to('cpu'); tracker.spect.to('cpu'); tracker.device = torch.device('cpu')
        beat_logits, down_logits = tracker(y.astype(np.float64), sr)
    beats, _ = Postprocessor('minimal')(beat_logits.cpu(), down_logits.cpu())
    down = torch.sigmoid(down_logits).cpu().numpy()
    strength = np.array([down[max(0, round(t * FPS) - 2):round(t * FPS) + 3].max(initial=0) for t in beats])
    return np.asarray(beats, float), strength


def _beat_level(beats, strength):
    """Halve or double a beat list whose tempo is far from a walking pace (≈57–174 BPM)."""
    if len(beats) < 8:
        return beats, strength
    tempo = 60 / np.median(np.diff(beats))
    if tempo > 174:
        # Keep the phase whose beats look more like downbeats.
        phase = int(np.mean(strength[1::2]) > np.mean(strength[0::2]))
        return beats[phase::2], strength[phase::2]
    if tempo < 57:
        mids = (beats[:-1] + beats[1:]) / 2
        both = np.empty(len(beats) * 2 - 1)
        both[0::2], both[1::2] = beats, mids
        weak = np.empty_like(both)
        weak[0::2], weak[1::2] = strength, 0.
        return both, weak
    return beats, strength


def bar_positions(strength, accent=None, prior={2: .1, 3: .3, 4: .6}, change=1e-8, sharpness=.7, bar_weight=.3, accent_weight=0.):
    """Viterbi over (beats per bar, position in bar) for each beat. Returns the position of every beat.

    Each bar start pays a share of its meter's prior, so a meter has to explain the downbeat
    evidence clearly before it wins with more bars: a strong beat 3 does not make 4/4 into 2/4.
    `accent` (per beat, around 0) adds note evidence: bars tend to start with more notes and a lower bass.
    """
    states = [(m, k) for m in sorted(prior) for k in range(m)]
    a = np.clip(strength, .02, .98)
    down, other = sharpness * np.log(a), sharpness * np.log(1 - a)
    if accent is not None:
        down = down + accent_weight * np.asarray(accent)
    start_cost = {m: bar_weight * math.log(prior[m]) for m in prior}
    value = np.array([math.log(prior[m] / m) + (down[0] + start_cost[m] if k == 0 else other[0]) for m, k in states])
    index = {s: i for i, s in enumerate(states)}
    back = []
    for i in range(1, len(a)):
        new, ptr = np.empty(len(states)), np.empty(len(states), int)
        for s, (m, k) in enumerate(states):
            if k:
                ptr[s] = index[(m, k - 1)]
                new[s] = value[ptr[s]]
            else:
                options = [(value[index[(q, q - 1)]] + math.log(1 - change if q == m else change / (len(prior) - 1)), index[(q, q - 1)]) for q in prior]
                new[s], ptr[s] = max(options)
                new[s] += start_cost[m]
            new[s] += down[i] if k == 0 else other[i]
        value = new
        back.append(ptr)
    s = int(np.argmax(value))
    path = [s]
    for ptr in reversed(back):
        s = int(ptr[s])
        path.append(s)
    return [states[s] for s in reversed(path)]


def _compound(notes, beats):
    """True when most off-beat onsets fall on thirds of a beat rather than halves or quarters."""
    if len(beats) < 4:
        return False
    votes = [0, 0]
    for n in notes:
        k = np.searchsorted(beats, n.start, side='right') - 1
        if k < 0 or k + 1 >= len(beats):
            continue
        phi = (n.start - beats[k]) / (beats[k + 1] - beats[k])
        if .1 < phi < .9:
            binary = min(abs(phi - x) for x in (.25, .5, .75))
            ternary = min(abs(phi - x) for x in (1 / 3, 2 / 3))
            if abs(binary - ternary) > .03:
                votes[ternary < binary] += 1
    return votes[1] > 1.5 * votes[0] and votes[1] >= 8


def accents(notes, beats):
    """Per beat, how much it looks like a bar start from the notes: onsets at the beat and a low bass."""
    starts = np.array([n.start for n in notes if n.technique != 'percussion'])
    pitches = np.array([n.midi for n in notes if n.technique != 'percussion'])
    count, low = np.zeros(len(beats)), np.full(len(beats), np.nan)
    for i, t in enumerate(beats):
        near = np.abs(starts - t) < .05
        count[i] = near.sum()
        if near.any():
            low[i] = pitches[near].min()
    def z(x):
        x = np.where(np.isnan(x), np.nanmean(x) if np.any(~np.isnan(x)) else 0, x)
        return (x - x.mean()) / (x.std() + 1e-9)
    return z(count) - z(low)


def _regular(beats, strength):
    """Drop doubled beats and fill gaps where the tracker lost the beat (a pause, a quiet ending),
    so no beat is far longer or shorter than its neighbours. Filled beats carry no downbeat evidence."""
    if len(beats) < 4:
        return beats, strength
    step = float(np.median(np.diff(beats)))
    kept_b, kept_s = [beats[0]], [strength[0]]
    for t, s in zip(beats[1:], strength[1:]):
        if t - kept_b[-1] < .5 * step:
            if s > kept_s[-1]:
                kept_b[-1], kept_s[-1] = t, s
            continue
        kept_b.append(t); kept_s.append(s)
    b, s = np.array(kept_b), np.array(kept_s)
    gaps = np.diff(b)
    out_b, out_s = [b[0]], [s[0]]
    for i in range(1, len(b)):
        local = float(np.median(gaps[max(0, i - 5):i + 4]))
        count = int(round(gaps[i - 1] / local))
        if gaps[i - 1] > 1.5 * local and count >= 2:
            for k in range(1, count):
                out_b.append(b[i - 1] + k * gaps[i - 1] / count); out_s.append(.5)
        out_b.append(b[i]); out_s.append(s[i])
    return np.array(out_b), np.array(out_s)


def bars_from_beats(notes, duration, beats, strength, accent_weight=1.2, accent_sharpness=.7, lock=.98):
    """Bars that follow the tracked beats, a short first bar for a pickup, and plain bars to fill the rest.

    The beat tracker's downbeat probabilities alone decide how many beats a bar has. With that meter
    favoured (`lock`), note accents then help place the bar lines, which fixes bars shifted by half a
    bar; on the reference tabs they made the meter itself worse, so they do not choose it.
    """
    beats, strength = _beat_level(np.asarray(beats, float), np.asarray(strength, float))
    beats, strength = _regular(beats, strength)
    if len(beats) < 8:
        return None
    plain = bar_positions(strength)
    positions = plain
    if accent_weight:
        main = Counter(m for m, _ in plain).most_common(1)[0][0]
        prior = {main: 1.} if lock >= 1 else {m: lock if m == main else (1 - lock) / 2 for m in (2, 3, 4)}
        positions = bar_positions(strength, accents(notes, beats), prior=prior, sharpness=accent_sharpness, accent_weight=accent_weight)
    compound = _compound(notes, beats)
    step = float(np.median(np.diff(beats)))
    times, positions = list(beats), list(positions)
    first = min((n.start for n in notes), default=times[0])
    # Beats before the first tracked one (continuing its bar count) so every note is inside a bar.
    while times[0] > first + 1e-6:
        m, k = positions[0]
        times.insert(0, max(0., times[0] - step))
        positions.insert(0, (m, (k - 1) % m))
        if times[0] == 0.:
            break
    end = max([duration] + [n.end for n in notes])
    while times[-1] < end:
        m, k = positions[-1]
        times.append(times[-1] + step)
        positions.append((m, (k + 1) % m))
    times.append(times[-1] + step)  # end of the last beat
    # Start at the beat holding the first note; an empty lead-in is not written.
    start = max(0, int(np.searchsorted(times, first, side='right')) - 1)
    groups = []
    for i in range(start, len(times) - 1):
        if positions[i][1] == 0 or not groups:
            groups.append([])
        groups[-1].append(i)
    out = []
    for group in groups:
        numerator, denominator = (len(group) * 3, 8) if compound else (len(group), 4)
        a, b = times[group[0]], times[group[-1] + 1]
        out.append(Bar(start=round(a, 4), end=round(b, 4), numerator=numerator, denominator=denominator,
                       tempo=float(np.clip(60 * numerator * 4 / denominator / (b - a), 20, 300)),
                       beats=[round(times[i], 4) for i in group]))
    return out


# ---------------------------------------------------------------- beat grid of a bar

def beat_count(bar: Bar):
    if bar.denominator == 8 and bar.numerator % 3 == 0 and bar.numerator > 3:
        return bar.numerator // 3
    return bar.numerator


def bar_beats(bar: Bar):
    """Beat start times inside a bar: the tracked ones when they are consistent, else equal steps."""
    n = beat_count(bar)
    b = bar.beats
    if len(b) == n and abs(b[0] - bar.start) < 1e-3 and all(x < y for x, y in zip(b, b[1:])) and b[-1] < bar.end:
        return list(b)
    return [bar.start + i * (bar.end - bar.start) / n for i in range(n)]


def bar_length(bar: Bar):
    return round(QUARTER * 4 * bar.numerator / bar.denominator)


# ---------------------------------------------------------------- written note values

STRAIGHT = {1: 0., 2: .02, 4: .05, 3: .1, 6: .16}   # simple meters: 3 and 6 are triplets
COMPOUND = {1: 0., 3: .02, 6: .05}                  # dotted-quarter beats divide in three natively


@dataclass
class Timing:
    """Where each note sits in the file: absolute ticks from the first bar, and its voice."""
    bar_ticks: list[int]                       # start tick of each bar
    grids: list[list[int]]                     # per bar, per beat: subdivisions used (3 or 6 in simple meter = triplets)
    notes: dict[str, tuple[int, int, int]] = field(default_factory=dict)  # id -> (start, end, voice)
    two_voices: list[bool] = field(default_factory=list)


def _locate(t, bars):
    """(bar index, beat index, fraction of the beat) for a time, or None outside the bars."""
    i = int(np.searchsorted([b.start for b in bars], t, side='right')) - 1
    if i < 0 or t >= bars[-1].end + 1e-6:
        return None
    i = min(i, len(bars) - 1)
    beats = bar_beats(bars[i]) + [bars[i].end]
    k = max(0, min(len(beats) - 2, int(np.searchsorted(beats, t, side='right')) - 1))
    return i, k, (t - beats[k]) / (beats[k + 1] - beats[k])


def quantize(p: Project, notes=None) -> Timing:
    notes = [n for n in (p.notes if notes is None else notes)]
    bars = p.bars
    ticks, total = [], 960
    for bar in bars:
        ticks.append(total)
        total += bar_length(bar)
    ticks.append(total)
    beat_len = [bar_length(b) // beat_count(b) for b in bars]
    compound = [beat_count(b) != b.numerator and b.denominator == 8 for b in bars]
    where = {n.id: _locate(n.start, bars) for n in notes}
    by_beat = defaultdict(list)
    for n in notes:
        if where[n.id]:
            i, k, phi = where[n.id]
            by_beat[(i, k)].append((n, phi))
    grids = [[1] * beat_count(b) for b in bars]
    for (i, k), items in by_beat.items():
        choices = COMPOUND if compound[i] else STRAIGHT
        def cost(g):
            snapped = [round(phi * g) / g for _, phi in items]
            error = sum(abs(phi - s) for (_, phi), s in zip(items, snapped))
            # Two plucks of one string cannot share a grid point.
            keys = [(n.string, s) for (n, _), s in zip(items, snapped) if n.string]
            clash = len(keys) - len(set(keys))
            return error + choices[g] + clash
        grids[i][k] = min(choices, key=cost)

    def to_tick(t, snap_grid=None):
        loc = _locate(t, bars)
        if loc is None:
            return ticks[-1] if t >= bars[-1].end else ticks[0]
        i, k, phi = loc
        g = snap_grid or grids[i][k]
        step = round(phi * g)
        if snap_grid is None and (i, k) in no_round_up:
            step = min(step, g - 1)
        return ticks[i] + k * beat_len[i] + round(step / g * beat_len[i])

    # Two plucks of one string cannot share a tick. A beat where that happens falls back to 64th
    # notes (the old fixed grid), and a note there stops rounding up into the next beat.
    fine = [max(1, n // 60) for n in beat_len]
    no_round_up = set()
    for _ in range(4):
        starts = {n.id: to_tick(n.start) for n in notes if where[n.id]}
        seen, changed = {}, False
        for n in sorted((n for n in notes if n.id in starts and n.string and n.technique != 'percussion'), key=lambda n: n.start):
            key = (n.string, starts[n.id])
            if key in seen:
                for m in (seen[key], n):
                    i, k, _ = where[m.id]
                    if grids[i][k] != fine[i]:
                        grids[i][k], changed = fine[i], True
                    elif m is seen[key] and (i, k) not in no_round_up:
                        no_round_up.add((i, k)); changed = True
            else:
                seen[key] = n
        if not changed:
            break

    timing = Timing(bar_ticks=ticks[:-1], grids=grids)
    placed = [n for n in notes if n.id in starts]
    bar_of = lambda tick: min(len(bars) - 1, max(0, int(np.searchsorted(ticks, tick, side='right')) - 1))  # noqa: E731

    def next_beat(a):
        """The first beat line after tick `a`; every grid fits between beat lines."""
        i = bar_of(a)
        return min(ticks[i + 1], ticks[i] + ((a - ticks[i]) // beat_len[i] + 1) * beat_len[i])
    string_next = defaultdict(list)
    for n in placed:
        if n.string and n.technique != 'percussion':
            string_next[n.string].append(starts[n.id])
    string_next = {s: sorted(set(x)) for s, x in string_next.items()}

    def spans(voice_of):
        onsets = defaultdict(set)
        for n in placed:
            onsets[voice_of(n)].add(starts[n.id])
        onsets = {v: sorted(x) for v, x in onsets.items()}
        out = {}
        for n in placed:
            a, v = starts[n.id], voice_of(n)
            later = onsets[v][int(np.searchsorted(onsets[v], a, side='right')):]
            gap_end = later[0] if later else ticks[-1]
            i = bar_of(a)
            beat = beat_len[i]
            real = to_tick(n.end, snap_grid=3 if compound[i] else 2)
            # Short gaps are legato. Over a long gap the note's own end leaves a rest, but it lasts
            # to the next beat line so a phrase end does not turn into a tiny value.
            b = gap_end if gap_end - a <= beat else min(gap_end, max(real, next_beat(a)))
            if n.string and n.technique != 'percussion':
                again = string_next[n.string][int(np.searchsorted(string_next[n.string], a, side='right')):]
                if again:
                    b = min(b, again[0])  # a string stops when it is plucked again
            out[n.id] = (a, max(b, a + 1), v)
        return out

    # Fingerstyle bass (strings 4-6) gets its own voice in bars where it sounds under a moving melody.
    bass = lambda n: n.string >= 4 and n.technique != 'percussion'  # noqa: E731
    trial = spans(lambda n: int(bass(n)))
    two = [False] * len(bars)
    melody_starts = [a for a, _, v in trial.values() if v == 0]
    for a, b, v in trial.values():
        if v == 1:
            for m in melody_starts:
                if a < m < b:
                    two[bar_of(m)] = True
    timing.notes = spans(lambda n: int(bass(n) and two[bar_of(starts[n.id])]))
    for a, b, v in timing.notes.values():
        if v == 1:
            for i in range(bar_of(a), bar_of(b - 1) + 1):
                two[i] = True  # a bass note tied into the next bar keeps that bar's second voice
    timing.two_voices = two
    return timing
