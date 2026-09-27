"""Measure TabSight against reference Guitar Pro tabs of recorded performances.

Each tab is paired with the YouTube performance it transcribes. The audio is transcribed once with
GAPS and cached, so changes to fingering, beats and bars can be re-scored in minutes. The tab is
aligned to the recording (chroma DTW, then refined on the transcribed notes) and the current code's
draft is scored against it. Tabs and audio stay in the data folder; nothing here is committed.

    .venv/bin/python scripts/evaluate.py prepare path/to/tabs/"Artist Name" ...   # find videos, download audio
    .venv/bin/python scripts/evaluate.py transcribe                              # GAPS once per song
    .venv/bin/python scripts/evaluate.py score --out test-results/eval.json       # score the current code
    .venv/bin/python scripts/evaluate.py tune                                    # fit server.music.FINGERING

Scores come from an automatic alignment and a tab that may differ from the recording in places,
so they compare versions of the code; they are not exact accuracies.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.store import DATA  # noqa: E402  (respects TABSIGHT_DATA_DIR)

EVAL = DATA / 'eval'
SONGS = EVAL / 'songs'
MANIFEST = EVAL / 'manifest.json'
SR = 16000
FPS = 10  # chroma frames per second for the coarse alignment
TAB_SUFFIXES = {'.gp', '.gp5', '.gp4', '.gp3', '.gpx'}
ALIASES = {'sungha jung': ['sungha jung', '정성하'], 'masaaki kishibe': ['masaaki kishibe', '岸部']}
STOP = {'the', 'of', 'and', 'ver', 'version', 'full', 'gp7', 'gp5', 'gp', 'sungha', 'jung', 'masaaki', 'massaki',
        'kishibe', 'masaki', 'guitar', 'cover', 'standard'}
SKIP_TITLES = re.compile(r'\b(tab|tabs|tutorial|lesson|playlist)\b|커버하기|강좌|레슨', re.I)


# ---------------------------------------------------------------- manifest and references

def load_manifest():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def save_manifest(manifest):
    EVAL.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def slug(path: Path):
    return re.sub(r'[^\w]+', '-', unicodedata.normalize('NFKC', path.stem).lower()).strip('-')[:60]


def tab_files(args):
    """(artist, file) pairs. A folder name is the artist."""
    for arg in args:
        path = Path(arg).expanduser()
        files = sorted(f for f in path.rglob('*') if f.suffix.lower() in TAB_SUFFIXES) if path.is_dir() else [path]
        artist = (path if path.is_dir() else path.parent).name.strip()
        for f in files:
            yield artist, f


def fingerprint(ref):
    """Two files with the same title, artist and notes are copies of one tab."""
    return clean(ref.get('title')), clean(ref.get('artist')), len(ref['notes']), round(ref['duration'])


def read_reference(path: Path):
    result = subprocess.run(['node', str(ROOT / 'scripts' / 'reference-tab.mjs'), str(path)], capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        # alphaTab's reason, e.g. "No compatible importer found for file", rather than the stack trace.
        reason = re.search(r'Error\(?"?([^"\n]+)', result.stderr)
        raise RuntimeError(reason.group(1).strip() if reason else result.stderr.strip()[-200:])
    return json.loads(result.stdout)


def load_audio(path):
    import soundfile as sf
    y, sr = sf.read(path, dtype='float32')
    if y.ndim > 1:
        y = y.mean(axis=1)
    assert sr == SR, path
    return y


# ---------------------------------------------------------------- finding the recording

def clean(text):
    """Readable words only: some GP5 files store Korean/Japanese in another encoding (U+FFFD here)."""
    text = unicodedata.normalize('NFKC', text or '').replace('\ufffd', ' ')
    text = re.sub(r'\(\s*\)', ' ', re.sub(r'[_\s]+', ' ', ''.join(ch for ch in text if ch.isprintable())))
    return text.strip()


def tokens(text):
    return {t for t in re.findall(r'[^\W_]+', clean(text).lower()) if len(t) >= 2 and t not in STOP}


def title_matches(ref_tokens, title):
    norm = clean(title).lower()
    words = tokens(title)
    for t in ref_tokens:
        if t in words or (not t.isascii() and t in norm):
            return True
        if len(t) >= 5 and any(len(w) >= 5 and w[:5] == t[:5] for w in words):
            return True
    return False


def search(query, count=8):
    import yt_dlp
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'extract_flat': True, 'skip_download': True}) as ydl:
        info = ydl.extract_info(f'ytsearch{count}:{query}', download=False)
    return [{'id': e.get('id'), 'title': e.get('title') or '', 'channel': e.get('channel') or e.get('uploader') or '',
             'duration': e.get('duration') or 0} for e in info.get('entries', []) if e.get('id')]


def candidates(artist, path, ref):
    names = ALIASES.get(artist.lower(), [artist.lower()])
    stem, title = clean(path.stem), clean(ref.get('title'))
    ref_tokens = tokens(title) | tokens(stem)
    # The file name often repeats the artist; searching with it twice buries the recording.
    bare = ' '.join(w for w in re.split(r'[\s\-,()]+', stem) if w and w.lower() not in STOP and w.lower() not in names)
    # YouTube localizes titles, so one video can come back under different titles for each query.
    results = {}
    for query in dict.fromkeys(q for q in [f'{artist} {title}', f'{artist} {bare}'] if q.strip() != artist):
        for c in search(query):
            results.setdefault(c['id'], {**c, 'titles': []})['titles'].append(c['title'])
    found = []
    for c in results.values():
        if not any(n in c['channel'].lower() for n in names) or any(SKIP_TITLES.search(t) for t in c['titles']):
            continue
        if not any(title_matches(ref_tokens, t) for t in c['titles']) or not .4 * ref['duration'] <= c['duration'] <= 2.2 * ref['duration']:
            continue
        found.append({k: c[k] for k in ('id', 'title', 'channel', 'duration')})
    return found[:4]


def download_audio(video, folder: Path):
    import yt_dlp
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / f'cand-{video}.wav'
    if wav.exists():
        return wav
    # YouTube answers 403 for some streams now and then while another stream (or a retry) works.
    for fmt in ['bestaudio/best', '251/250/249', '140/139', '234/233', 'bestaudio/best']:
        opts = {'format': fmt, 'outtmpl': str(folder / f'cand-{video}.%(ext)s'), 'quiet': True, 'noprogress': True,
                'no_warnings': True, 'noplaylist': True, 'socket_timeout': 20, 'retries': 2}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(f'https://www.youtube.com/watch?v={video}', download=True)
            break
        except yt_dlp.utils.DownloadError as e:
            error = e
            for partial in folder.glob(f'cand-{video}.*'):
                partial.unlink()
            time.sleep(3)
    else:
        raise error
    source = next(f for f in folder.glob(f'cand-{video}.*') if f.suffix != '.wav')
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source), '-ac', '1', '-ar', str(SR), str(wav)],
                   check=True, timeout=300)
    source.unlink()
    return wav


# ---------------------------------------------------------------- alignment

def synthesize(notes, duration):
    """A plain additive rendering of the tab, only for chroma features comparable to the recording."""
    y = np.zeros(int((duration + 3) * SR), np.float32)
    for n in notes:
        if n['midi'] is None:
            continue
        f0 = 440 * 2 ** ((n['midi'] - 69) / 12)
        t = np.arange(int(min(max(n['end'] - n['start'], .25), 2.5) * SR)) / SR
        wave = sum(.6 ** k * np.sin(2 * np.pi * (k + 1) * f0 * t) for k in range(4) if (k + 1) * f0 < SR / 2)
        segment = (np.exp(-t / .5) * np.minimum(1, t / .005) * wave).astype(np.float32)
        i = int(n['start'] * SR)
        y[i:i + len(segment)] += segment[:max(0, len(y) - i)]
    return y / (np.abs(y).max() + 1e-9)


def chroma(y):
    import librosa
    c = librosa.feature.chroma_cqt(y=y, sr=SR, hop_length=SR // FPS)
    from scipy.ndimage import uniform_filter1d
    c = uniform_filter1d(c, 3, axis=1) + 1e-4  # silent frames become flat instead of undefined
    return c / np.linalg.norm(c, axis=0, keepdims=True)


def coarse_alignment(ref, y, shifts=range(-3, 4), stretches=None):
    """Best chroma DTW of the whole tab against part of the recording, trying small transpositions.

    Subsequence DTW needs the tab to be the shorter sequence (librosa swaps them otherwise), so a
    tab written slower than the performance can be compressed in time before matching."""
    import librosa
    audio_chroma = chroma(y)
    duration = len(y) / SR
    if stretches is None:
        # Costs per path step are not comparable across stretches, so compress only when needed.
        stretches = [1.] if ref['duration'] < duration * .98 else [s for s in (.9, .8, .7, .6) if ref['duration'] * s < duration * .98][:3] or [.6]
    best = None
    for stretch in stretches:
        notes = [dict(n, start=n['start'] * stretch, end=n['end'] * stretch) for n in ref['notes']]
        ref_chroma = chroma(synthesize(notes, ref['duration'] * stretch))
        for shift in shifts:
            D, wp = librosa.sequence.dtw(X=np.roll(ref_chroma, shift, axis=0), Y=audio_chroma, metric='cosine', subseq=True)
            cost = float(D[-1, wp[0][1]] / len(wp))
            if best is None or cost < best[0]:
                best = (cost, shift, stretch, wp[::-1])
    cost, shift, stretch, wp = best
    ref_t, audio_t = wp[:, 0] / FPS / stretch, wp[:, 1] / FPS
    xs, inverse = np.unique(ref_t, return_inverse=True)
    ys = np.bincount(inverse, weights=audio_t) / np.bincount(inverse)
    return {'cost': cost, 'shift': shift, 'stretch': stretch, 'ref': xs, 'audio': np.maximum.accumulate(ys)}


def local_median(times, values, half=2.5, minimum=3):
    order = np.argsort(times)
    t, v = times[order], values[order]
    out = np.full(len(t), np.nan)
    for i, x in enumerate(t):
        a, b = np.searchsorted(t, x - half), np.searchsorted(t, x + half, side='right')
        window = v[a:b][~np.isnan(v[a:b])]
        if len(window) >= minimum:
            out[i] = np.median(window)
    result = np.empty_like(out)
    result[order] = out
    return result


def refined_mapping(coarse, ref_notes, pred_notes):
    """ref seconds -> audio seconds: the DTW path, corrected by nearby same-pitch transcribed onsets."""
    ref_t = np.array([n['start'] for n in ref_notes])
    ref_m = [n['midi'] for n in ref_notes]
    onsets = defaultdict(list)
    for n in pred_notes:
        onsets[n.midi].append(n.start)
    onsets = {m: np.array(sorted(v)) for m, v in onsets.items()}
    base = lambda t: np.interp(t, coarse['ref'], coarse['audio'])  # noqa: E731
    knots, corr = np.array([0.]), np.array([0.])
    # Search nearby onsets in shrinking windows and smooth over shrinking spans, so the mapping
    # follows rubato without being pulled by any single note.
    for window, half in [(.5, 2.5), (.25, 1.5), (.12, 1.), (.08, .6), (.06, .4)]:
        mapped = base(ref_t) + np.interp(ref_t, knots, corr)
        resid = np.full(len(ref_t), np.nan)
        for i, (t, m) in enumerate(zip(mapped, ref_m)):
            arr = onsets.get(m)
            if arr is None:
                continue
            k = np.searchsorted(arr, t)
            near = [arr[j] - t for j in (k - 1, k) if 0 <= j < len(arr)]
            d = min(near, key=abs) if near else None
            if d is not None and abs(d) <= window:
                resid[i] = d
        step = local_median(ref_t, resid, half=half)
        valid = ~np.isnan(step)
        if valid.sum() < 5:
            break
        step = np.interp(ref_t, ref_t[valid], step[valid])
        total = np.interp(ref_t, knots, corr) + step
        knots, idx = np.unique(ref_t, return_index=True)
        corr = total[idx]
    return lambda t: base(t) + np.interp(t, knots, corr)


def match(ref_times, ref_pitch, pred_times, pred_pitch, tol):
    from scipy.optimize import linear_sum_assignment
    pairs = []
    ref_pitch, pred_pitch = np.asarray(ref_pitch), np.asarray(pred_pitch)
    for m in set(ref_pitch.tolist()) & set(pred_pitch.tolist()):
        ri, pi = np.flatnonzero(ref_pitch == m), np.flatnonzero(pred_pitch == m)
        cost = np.abs(ref_times[ri][:, None] - pred_times[pi][None, :])
        cost[cost > tol] = 1e6
        r, c = linear_sum_assignment(cost)
        keep = cost[r, c] <= tol
        pairs += list(zip(ri[r[keep]].tolist(), pi[c[keep]].tolist()))
    return pairs


def f_measure(reference, estimate, tol=.07):
    reference, estimate = np.asarray(reference), np.asarray(estimate)
    if not len(reference) or not len(estimate):
        return 0.
    hits = len(match(reference, np.zeros(len(reference)), estimate, np.zeros(len(estimate)), tol))
    p, r = hits / len(estimate), hits / len(reference)
    return 2 * p * r / (p + r) if hits else 0.


def align_to_transcription(ref, y, shift, pitched, gaps_notes):
    """ref seconds -> audio seconds. Tries a few time compressions of the tab and keeps the one whose
    refined mapping pairs the most tab notes with transcribed notes of the same pitch (±50 ms). The
    GAPS output is the same for every code version, so every version is scored on the same mapping."""
    starts = np.array([n['start'] for n in pitched])
    best = None
    for stretch in (1., .9, .8, .7):
        if stretch < 1 and ref['duration'] * stretch > len(y) / SR * 1.2:
            continue
        coarse = coarse_alignment(ref, y, shifts=[shift], stretches=[stretch])
        to_audio = refined_mapping(coarse, pitched, gaps_notes)
        pairs = len(match(to_audio(starts), [n['midi'] for n in pitched], np.array([n.start for n in gaps_notes]), [n.midi for n in gaps_notes], .05))
        if best is None or pairs > best[0]:
            best = (pairs, coarse, to_audio)
    return best[1], best[2]


# ---------------------------------------------------------------- reference rhythm

def beat_ticks(numerator, denominator):
    if denominator == 8 and numerator % 3 == 0 and numerator > 3:
        return 1440  # compound meter: dotted quarters
    return {2: 1920, 4: 960, 8: 480, 16: 240}.get(denominator, 960)


def reference_beats(ref):
    beats, downbeats = [], []
    for bar in ref['bars']:
        unit = beat_ticks(bar['numerator'], bar['denominator'])
        span = bar['endTick'] - bar['startTick']
        for tick in np.arange(0, span - 1, unit):
            beats.append(bar['start'] + tick / span * (bar['end'] - bar['start']))
        if not bar['anacrusis']:
            downbeats.append(bar['start'])
    return np.array(beats), np.array(downbeats)


def meter(numerator, denominator):
    return numerator // 3 if denominator == 8 and numerator % 3 == 0 and numerator > 3 else numerator


def estimated_beats(p):
    """Beat and downbeat times of the draft's bars (tracked beats when the code stores them)."""
    try:
        from server.rhythm import bar_beats
    except ImportError:  # before bars carried beats
        def bar_beats(b):
            return [b.start + k * (b.end - b.start) / b.numerator for k in range(b.numerator)]
    beats = [t for b in p.bars for t in bar_beats(b)]
    counts = [len(bar_beats(b)) for b in p.bars]
    # A first bar shorter than the next is a pickup: its start is not a downbeat.
    downbeats = [b.start for i, b in enumerate(p.bars) if not (i == 0 and len(counts) > 1 and counts[0] < counts[1])]
    return np.array(beats), np.array(downbeats)


def written_times(p, notes):
    """Where the file puts each note: (time on the draft's beat grid, beat start, beat length, fraction of the beat)."""
    try:
        from server.rhythm import bar_beats, bar_length, beat_count, quantize
    except ImportError:
        # Earlier export: a 64th-note grid laid evenly across each bar.
        out = {}
        starts = [b.start for b in p.bars]
        for n in notes:
            i = int(np.searchsorted(starts, n.start, side='right')) - 1
            if i < 0 or n.start >= p.bars[-1].end:
                continue
            b = p.bars[i]
            length = round(3840 * b.numerator / b.denominator)
            tick = max(0, min(length - 60, round((n.start - b.start) / (b.end - b.start) * length / 60) * 60))
            beat = length / b.numerator
            k = int(tick // beat)
            step = (b.end - b.start) / b.numerator
            out[n.id] = (b.start + tick / length * (b.end - b.start), b.start + k * step, step, tick / beat - k)
        return out
    timing = quantize(p, notes)
    out = {}
    for n in notes:
        if n.id not in timing.notes:
            continue
        tick = timing.notes[n.id][0]
        i = min(len(p.bars) - 1, int(np.searchsorted(timing.bar_ticks, tick, side='right')) - 1)
        beats = bar_beats(p.bars[i]) + [p.bars[i].end]
        beat = bar_length(p.bars[i]) / beat_count(p.bars[i])
        k = min(len(beats) - 2, int((tick - timing.bar_ticks[i]) // beat))
        f = (tick - timing.bar_ticks[i]) / beat - k
        out[n.id] = (beats[k] + f * (beats[k + 1] - beats[k]), beats[k], beats[k + 1] - beats[k], f)
    return out


def written_stats(p):
    """Readability of the exported file: ties per note, very short values and triplet groups."""
    import io
    import guitarpro
    from server.export import gp5_bytes
    song = guitarpro.parse(io.BytesIO(gp5_bytes(p, preview=True)), encoding='utf-8')
    notes = ties = short = tuplets = 0
    for measure in song.tracks[0].measures:
        for voice in measure.voices:
            for beat in voice.beats:
                if not beat.notes:
                    continue
                tuplets += beat.duration.tuplet.enters != 1
                for n in beat.notes:
                    if n.type == guitarpro.NoteType.tie:
                        ties += 1
                    else:
                        notes += 1
                        short += beat.duration.value >= 32
    return {'ties_per_note': round(ties / max(1, notes), 3), 'short_values': round(short / max(1, notes), 3),
            'triplet_beats': tuplets}


# ---------------------------------------------------------------- commands

def prepare(args):
    manifest = load_manifest()
    seen = {}
    for artist, path in tab_files(args.tabs):
        key = slug(path)
        folder = SONGS / key
        entry = manifest.get(key, {})
        if entry.get('video') and (folder / 'audio.wav').exists() and not args.refresh:
            seen.setdefault(fingerprint(json.loads((folder / 'reference.json').read_text())), key)
            print(f'= {key}: {entry["video"]} (kept)')
            continue
        try:
            ref = read_reference(path)
        except RuntimeError as e:
            # Password-protected or damaged files cannot be read; another copy of the tab may be.
            print(f'! {key}: cannot read tab ({e})')
            continue
        if fingerprint(ref) in seen:
            print(f'= {key}: same tab as {seen[fingerprint(ref)]}, skipped')
            continue
        seen[fingerprint(ref)] = key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'reference.json').write_text(json.dumps(ref, ensure_ascii=False))
        pinned = entry.get('pinned')
        found = [{'id': pinned, 'title': entry.get('title', ''), 'channel': entry.get('channel', ''), 'duration': entry.get('duration', 0)}] if pinned else candidates(artist, path, ref)
        if not found:
            print(f'! {key}: no matching video from {artist}')
            manifest[key] = {'tab': str(path), 'artist': artist, 'error': 'no candidate'}
            continue
        scored = []
        for c in found:
            try:
                wav = download_audio(c['id'], folder)
            except Exception as e:  # unavailable, region-locked, ...
                print(f'  {c["id"]}: download failed ({str(e)[:80]})')
                continue
            a = coarse_alignment(ref, load_audio(wav))
            scored.append({**c, 'cost': round(a['cost'], 4), 'shift': a['shift']})
            print(f'  {c["id"]} cost={a["cost"]:.3f} shift={a["shift"]:+d} {c["duration"]}s {c["title"][:60]}')
        if not scored:
            continue
        best = min(scored, key=lambda c: c['cost'])
        (folder / f'cand-{best["id"]}.wav').replace(folder / 'audio.wav')
        for c in scored:
            (folder / f'cand-{c["id"]}.wav').unlink(missing_ok=True)
        manifest[key] = {'tab': str(path), 'artist': artist, 'title': best['title'], 'channel': best['channel'], 'video': best['id'],
                         'url': f'https://www.youtube.com/watch?v={best["id"]}', 'duration': best['duration'],
                         'reference_duration': round(ref['duration'], 1), 'shift': best['shift'], 'cost': best['cost'],
                         'candidates': [{k: c[k] for k in ('id', 'cost', 'shift', 'duration')} for c in scored],
                         **({'pinned': pinned} if pinned else {})}
        print(f'+ {key}: {best["id"]} cost={best["cost"]:.3f} shift={best["shift"]:+d}')
        save_manifest(manifest)
    save_manifest(manifest)


def selected(manifest, only):
    for key, entry in sorted(manifest.items()):
        if entry.get('video') and not entry.get('exclude') and (not only or key in only) and (SONGS / key / 'audio.wav').exists():
            yield key, entry


def transcribe(args):
    from server import pipeline
    for key, entry in selected(load_manifest(), args.only):
        out = SONGS / key / 'gaps.json'
        if out.exists() and not args.refresh:
            continue
        started = time.monotonic()
        notes, device = pipeline.transcribe_audio(load_audio(SONGS / key / 'audio.wav'), SR, lambda f: None, lambda: False)
        out.write_text(json.dumps({'device': device, 'seconds': round(time.monotonic() - started, 1), 'notes': [n.model_dump() for n in notes]}))
        print(f'{key}: {len(notes)} notes in {time.monotonic() - started:.0f}s on {device}')


def run_draft(p, y):
    """The app's steps after note inference, on this checkout of the code."""
    from server import pipeline
    if hasattr(pipeline, 'draft_score'):
        return pipeline.draft_score(p, y, SR)
    # Checkouts from before draft_score existed ran the same steps inside the analysis job.
    import librosa
    from server.music import assign_fingering, build_bars, suggest_techniques
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=256)
    tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=256, trim=False)
    tempo = float(np.clip(np.asarray(tempo).reshape(-1)[0], 30, 240))
    p.duration, p.tempo = len(y) / SR, tempo
    p.bars = build_bars(p.notes, p.duration, tempo, librosa.frames_to_time(beats, sr=SR, hop_length=256))
    stats = assign_fingering(p)
    suggest_techniques(p, y, SR)
    return stats


def score_song(key, entry):
    from server.models import Note, Project
    from server.music import assign_fingering
    folder = SONGS / key
    ref = json.loads((folder / 'reference.json').read_text())
    gaps = json.loads((folder / 'gaps.json').read_text())
    y = load_audio(folder / 'audio.wav')
    shift = entry.get('shift', 0)
    # The player confirms the settings before transcription; the tab's are the confirmed ones here.
    # A transposed recording is taken as the same shapes on a retuned guitar.
    tuning, capo = [t + shift for t in ref['tuning']], ref['capo']
    pitched = [dict(n, midi=n['midi'] + shift) for n in ref['notes'] if n['midi'] is not None and not n['dead']]
    p = Project(id=uuid.uuid4().hex, title=key, tuning=tuning, capo=capo, notes=[Note(**n) for n in gaps['notes']])
    run_draft(p, y)

    coarse, to_audio = align_to_transcription(ref, y, shift, pitched, [Note(**n) for n in gaps['notes']])
    pred = [n for n in p.notes if n.technique != 'percussion']
    ref_on = to_audio(np.array([n['start'] for n in pitched]))
    lo, hi = ref_on.min() - .5, to_audio(np.array([max(n['end'] for n in pitched)]))[0] + .5
    pred = [n for n in pred if lo <= n.start <= hi]
    pred_on = np.array([n.start for n in pred])
    result = {'song': key, 'video': entry['video'], 'shift': shift, 'align_cost': round(coarse['cost'], 3),
              'ref_notes': len(pitched), 'pred_notes': len(pred)}
    for tol in (.05, .1):
        pairs = match(ref_on, [n['midi'] for n in pitched], pred_on, [n.midi for n in pred], tol)
        prec, rec = len(pairs) / max(1, len(pred)), len(pairs) / max(1, len(pitched))
        result[f'note_f1_{int(tol * 1000)}'] = round(2 * prec * rec / (prec + rec), 4) if pairs else 0.
        if tol == .05:
            result.update(note_precision=round(prec, 4), note_recall=round(rec, 4))
            scored = [(pitched[i], pred[j]) for i, j in pairs if pitched[i]['technique'] != 'harmonic' and pred[j].technique != 'harmonic' and pred[j].string]
            correct = sum(r['string'] == q.string for r, q in scored)
            result['string_acc_e2e'] = round(correct / max(1, len(scored)), 4)
            result['e2e_scored'] = len(scored)
            tp, tr = correct / max(1, len(pred)), correct / max(1, len(pitched))
            result['tab_f1'] = round(2 * tp * tr / (tp + tr), 4) if correct else 0.

    # Fingering alone: the tab's own notes and settings, so transcription errors do not count.
    normal = [n for n in pitched if n['technique'] != 'harmonic']
    oracle = Project(id=uuid.uuid4().hex, tuning=tuning, capo=capo,
                     notes=[Note(id=str(i), midi=n['midi'], start=n['start'], end=max(n['end'], n['start'] + .05)) for i, n in enumerate(normal)])
    assign_fingering(oracle)
    by_id = {n.id: n for n in oracle.notes}
    same = [by_id[str(i)].string == n['string'] for i, n in enumerate(normal)]
    result['string_acc_oracle'] = round(float(np.mean(same)), 4) if same else 0.
    result['oracle_scored'] = len(same)

    ref_beats_score, ref_down = reference_beats(ref)
    ref_beats, ref_down = to_audio(ref_beats_score), to_audio(ref_down)
    inside = lambda t: t[(t >= lo) & (t <= hi)]  # noqa: E731
    est_beats, est_down = estimated_beats(p)
    rb, rd, eb, ed = inside(ref_beats), inside(ref_down), inside(est_beats), inside(est_down)
    result['beat_f1'] = round(f_measure(rb, eb), 4)
    result['downbeat_f1'] = round(f_measure(rd, ed), 4)
    result['tempo_ratio'] = round(float(np.median(np.diff(rb)) / np.median(np.diff(eb))), 3) if len(rb) > 2 and len(eb) > 2 else None
    result['meter_ref'] = Counter(meter(b['numerator'], b['denominator']) for b in ref['bars']).most_common(1)[0][0]
    result['meter_est'] = Counter(meter(b.numerator, b.denominator) for b in p.bars).most_common(1)[0][0] if p.bars else None

    # Written rhythm. Loosely: is each matched onset written near where the tab has it (1/12 beat)?
    # Exactly: where our beat is the tab's beat, is the note on the same subdivision of it?
    pairs = match(ref_on, [n['midi'] for n in pitched], pred_on, [n.midi for n in pred], .05)
    written = written_times(p, pred)
    index = np.arange(len(ref_beats_score))
    ref_beat_len = np.diff(np.append(ref_beats, ref_beats[-1] + np.median(np.diff(ref_beats))))
    agree = raw = counted = same_beat = exact = 0
    for i, j in pairs:
        if pred[j].id not in written:
            continue
        t, beat_start, beat_len, fraction = written[pred[j].id]
        u_ref = np.interp(pitched[i]['start'], ref_beats_score, index)
        counted += 1
        agree += abs(np.interp(t, ref_beats, index) - u_ref) < 1 / 12
        raw += abs(np.interp(pred[j].start, ref_beats, index) - u_ref) < 1 / 12
        k = int(np.floor(u_ref + 1e-6))
        if 0 <= k < len(ref_beats) and ref_beat_len[k] > 0 and abs(ref_beats[k] - beat_start) < .07 and .8 < beat_len / ref_beat_len[k] < 1.25:
            same_beat += 1
            exact += abs(fraction - (u_ref - k)) < .01
    result['written_onset_agree'] = round(agree / max(1, counted), 4)
    result['raw_onset_agree'] = round(raw / max(1, counted), 4)
    result['written_grid_agree'] = round(exact / max(1, same_beat), 4)
    result['beat_matched_share'] = round(same_beat / max(1, counted), 4)
    result.update(written_stats(p))
    return result


SUMMARY = [('note_f1_50', 'note F1'), ('string_acc_e2e', 'string (e2e)'), ('tab_f1', 'tab F1'), ('string_acc_oracle', 'string (oracle)'),
           ('beat_f1', 'beat F1'), ('downbeat_f1', 'downbeat F1'), ('written_grid_agree', 'written grid'), ('ties_per_note', 'ties/note')]


# ---------------------------------------------------------------- fitting the fingering costs

def fingering_case(key, entry):
    """The tab's notes for the oracle check and, once transcribed, GAPS notes paired with tab notes."""
    from server.models import Note
    folder = SONGS / key
    ref = json.loads((folder / 'reference.json').read_text())
    shift = entry.get('shift', 0)
    pitched = [dict(n, midi=n['midi'] + shift) for n in ref['notes'] if n['midi'] is not None and not n['dead']]
    case = {'key': key, 'artist': entry.get('artist', ''), 'tuning': [t + shift for t in ref['tuning']], 'capo': ref['capo'],
            'oracle': [(n['midi'], n['start'], max(n['end'], n['start'] + .05), n['string']) for n in pitched if n['technique'] != 'harmonic']}
    if (folder / 'gaps.json').exists():
        pred = [Note(**n) for n in json.loads((folder / 'gaps.json').read_text())['notes']]
        _, to_audio = align_to_transcription(ref, load_audio(folder / 'audio.wav'), shift, pitched, pred)
        pairs = match(to_audio(np.array([n['start'] for n in pitched])), [n['midi'] for n in pitched],
                      np.array([n.start for n in pred]), [n.midi for n in pred], .05)
        case['pred'] = [(n.midi, n.start, n.end) for n in pred]
        case['pairs'] = [(j, pitched[i]['string']) for i, j in pairs if pitched[i]['technique'] != 'harmonic']
    return case


_CASES = []


def _load_cases(cases):
    global _CASES
    _CASES = cases


def _case_accuracy(job):
    from server.models import Note, Project
    from server.music import assign_fingering
    index, weights = job
    case = _CASES[index]

    def run(rows):
        p = Project(id='0' * 32, tuning=case['tuning'], capo=case['capo'],
                    notes=[Note(id=str(i), midi=m, start=a, end=b) for i, (m, a, b, *_) in enumerate(rows)])
        assign_fingering(p, weights=weights)
        return [n.string for n in p.notes]
    strings = run(case['oracle'])
    out = {'oracle': float(np.mean([s == r[3] for s, r in zip(strings, case['oracle'])]))}
    if case.get('pairs'):
        strings = run(case['pred'])
        out['e2e'] = float(np.mean([strings[j] == s for j, s in case['pairs']]))
    return out


def objective(pool, indices, weights):
    results = pool.map(_case_accuracy, [(i, weights) for i in indices])
    per_song = [np.mean([r['oracle']] + ([r['e2e']] if 'e2e' in r else [])) for r in results]
    return float(np.mean(per_song)), results


GRID = {'fret': [0, .01, .02, .04, .06, .09], 'open': [0, .1, .25, .4, .6], 'span': [.5, 1.4, 3], 'span_free': [3, 4, 5],
        'move': [.03, .06, .12, .25, .5, 1], 'rest': [.25, .5, 1, 2, 1e6], 'cut': [0, .3, .6, 1.2, 2], 'ring': [.25, .5, 1, 2]}


def tune(args):
    from multiprocessing import Pool
    from server.music import FINGERING
    manifest = load_manifest()
    cases = [fingering_case(key, entry) for key, entry in selected(manifest, args.only)]
    train = [i for i, c in enumerate(cases) if not args.train_artist or c['artist'].lower() == args.train_artist.lower()]
    test = [i for i in range(len(cases)) if i not in train] or train
    print(f'{len(cases)} songs ({sum("pairs" in c for c in cases)} transcribed); fitting on {len(train)}')
    with Pool(min(8, len(cases)), initializer=_load_cases, initargs=(cases,)) as pool:
        start = dict(FINGERING)
        weights = dict(start)
        best, _ = objective(pool, train, weights)
        print(f'current weights {best:.4f}')
        for sweep in range(args.sweeps):
            changed = False
            for name, values in GRID.items():
                for value in values:
                    if value == weights[name]:
                        continue
                    score, _ = objective(pool, train, {**weights, name: value})
                    if score > best + 1e-4:
                        best, weights, changed = score, {**weights, name: value}, True
                        print(f'  sweep {sweep + 1}: {name}={value} -> {best:.4f}')
            if not changed:
                break
        print('fitted weights:', json.dumps(weights))
        for label, indices in [('fit', train), ('held out', test)]:
            old, _ = objective(pool, indices, start)
            new, results = objective(pool, indices, weights)
            print(f'{label}: current {old:.4f} -> fitted {new:.4f}')
        _, old = objective(pool, list(range(len(cases))), start)
        _, new = objective(pool, list(range(len(cases))), weights)
        for c, a, b in zip(cases, old, new):
            print(f'  {c["key"][:40]:40} oracle {a["oracle"]:.3f}->{b["oracle"]:.3f}' + (f'  e2e {a["e2e"]:.3f}->{b["e2e"]:.3f}' if 'e2e' in a else ''))


def score(args):
    manifest = load_manifest()
    rows = []
    for key, entry in selected(manifest, args.only):
        if not (SONGS / key / 'gaps.json').exists():
            print(f'{key}: not transcribed yet')
            continue
        row = score_song(key, entry)
        rows.append(row)
        print(f'{key[:34]:34} ' + ' '.join(f'{label}={row[k]:.3f}' for k, label in SUMMARY)
              + f' tempo×{row["tempo_ratio"]} meter {row["meter_est"]}/{row["meter_ref"]}')
    if not rows:
        return
    mean = {k: round(float(np.mean([r[k] for r in rows])), 4) for k, _ in SUMMARY}
    notes = sum(r['oracle_scored'] for r in rows)
    micro_oracle = sum(r['string_acc_oracle'] * r['oracle_scored'] for r in rows) / max(1, notes)
    print(f'\nmean over {len(rows)} songs: ' + ', '.join(f'{label} {mean[k]:.3f}' for k, label in SUMMARY)
          + f'; oracle string accuracy over {notes} notes {micro_oracle:.3f}; meter correct {sum(r["meter_est"] == r["meter_ref"] for r in rows)}/{len(rows)}')
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({'songs': rows, 'mean': mean, 'oracle_micro': round(micro_oracle, 4)}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    a = sub.add_parser('prepare', help='read tabs, find the recordings and download their audio')
    a.add_argument('tabs', nargs='+', help='tab files or folders named after the artist')
    a.add_argument('--refresh', action='store_true', help='search again even if a video was chosen')
    for name, helptext in [('transcribe', 'run GAPS once per song'), ('score', 'score the current code'), ('tune', 'fit the fingering costs')]:
        s = sub.add_parser(name, help=helptext)
        s.add_argument('--only', nargs='*', default=[], help='song keys (folder names under songs/)')
        if name == 'transcribe':
            s.add_argument('--refresh', action='store_true')
        elif name == 'score':
            s.add_argument('--out', help='write the per-song results as JSON')
        else:
            s.add_argument('--train-artist', help='fit on this artist only and report the others as held out')
            s.add_argument('--sweeps', type=int, default=3)
    args = parser.parse_args()
    {'prepare': prepare, 'transcribe': transcribe, 'score': score, 'tune': tune}[args.command](args)


if __name__ == '__main__':
    main()
