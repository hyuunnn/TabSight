"""Aligns an uploaded score with the original recording.

The score's notes, in playback order with repeats unrolled, become a chroma sequence; the recording
becomes one from its constant-Q spectrum. A coarse search first finds how many semitones the
recording sits above the file and the performer's overall tempo against the written one. A finer
subsequence DTW then finds where the whole score is played inside the recording (talk before or
after it is skipped) and how the tempo moves along the way. The path gives the recording time of
every bar start and note onset, which the browser interpolates between.
"""
from __future__ import annotations

import numpy as np

TICKS_PER_QUARTER = 960
# Budget for the fine DTW matrices (cells). Keeps memory near 100 MB for a long video.
MAX_CELLS = 6_000_000
# Guitar notes fade, so the score's energy fades like the recording's instead of holding flat.
DECAY_SECONDS = .7
# Cost multiplier for holding the score or the recording while the other moves. 3 kept every test
# case within 0.45 s; 2 let a quiet final chord slide 1.7 s early and 4 loosened the rest.
STRETCH_COST = 3.
# Below these the score probably is not this recording (or not this arrangement). In the checks,
# right pairs scored contrast ≥ 0.16 and match ≥ 0.23, other songs ≤ 0.034 and ≤ 0.11.
MIN_CONTRAST = .1
MIN_MATCH = .17


def written_seconds(tempos):
    """Seconds at the written tempo for a tick, from alphaTab's [tick, bpm] tempo events."""
    changes = {}
    for tick, bpm in sorted(tempos, key=lambda x: x[0]):
        changes[int(tick)] = float(bpm)  # the last change at a tick wins
    if not changes:
        changes[0] = 120.
    if min(changes) > 0:
        changes[0] = changes[min(changes)]
    ticks = np.array(sorted(changes), float)
    bpms = np.array([changes[int(t)] for t in ticks])
    starts = np.concatenate([[0.], np.cumsum(np.diff(ticks) / TICKS_PER_QUARTER * 60 / bpms[:-1])])

    def at(tick):
        tick = np.asarray(tick, float)
        i = np.clip(np.searchsorted(ticks, tick, side='right') - 1, 0, len(ticks) - 1)
        return starts[i] + (tick - ticks[i]) / TICKS_PER_QUARTER * 60 / bpms[i]
    return at


def _normalize(chroma):
    # Silent frames become flat vectors, so they match quiet parts of the recording moderately.
    chroma = chroma + 1e-3 * max(float(chroma.max()), 1e-9)
    return chroma / np.linalg.norm(chroma, axis=0, keepdims=True)


def _dtw_kernels():
    # numba ships with librosa, which uses it for its own DTW. Compiled once per server process.
    import numba

    @numba.njit
    def accumulate(cost, stretch):
        # A diagonal step plays score and recording at the same pace. Holding one while the other
        # moves (a pause, a rushed passage) costs `stretch` times more, so the path only does it
        # where the sound demands it rather than wherever frames happen to look alike.
        n, m = cost.shape
        total = np.empty((n, m))
        total[0, :] = cost[0, :]  # the score may start anywhere in the recording
        for i in range(1, n):
            total[i, 0] = total[i - 1, 0] + stretch * cost[i, 0]
            for j in range(1, m):
                c = cost[i, j]
                best = total[i - 1, j - 1] + c
                up = total[i - 1, j] + stretch * c
                if up < best:
                    best = up
                left = total[i, j - 1] + stretch * c
                if left < best:
                    best = left
                total[i, j] = best
        return total

    @numba.njit
    def backtrack(total, cost, stretch, end):
        i, j = total.shape[0] - 1, end
        path = np.empty((total.shape[0] + total.shape[1], 2), np.int64)
        k = 0
        while True:
            path[k, 0] = i
            path[k, 1] = j
            k += 1
            if i == 0:
                break
            if j == 0:
                i -= 1
                continue
            extra = (stretch - 1) * cost[i, j]
            diagonal, up, left = total[i - 1, j - 1], total[i - 1, j] + extra, total[i, j - 1] + extra
            if diagonal <= up and diagonal <= left:
                i -= 1
                j -= 1
            elif up <= left:
                i -= 1
            else:
                j -= 1
        return path[:k][::-1]

    return accumulate, backtrack


_KERNELS = None


def _subsequence_dtw(score, audio, stretch=STRETCH_COST):
    """Whole score (rows) against any stretch of the recording (columns).

    librosa's subsequence mode swaps the two when the score is the longer one, which happens when
    the file's written tempo is slower than the performance, so this keeps a fixed meaning.
    """
    global _KERNELS
    if _KERNELS is None:
        _KERNELS = _dtw_kernels()
    accumulate, backtrack = _KERNELS
    cost = np.ascontiguousarray(1 - score.T @ audio)
    total = accumulate(cost, stretch)
    return cost, total, backtrack(total, cost, stretch, int(np.argmin(total[-1])))


def score_chroma(notes, seconds, fps, frames):
    chroma = np.zeros((12, frames))
    centers = (np.arange(frames) + .5) / fps
    for start, length, key in notes:
        a = float(seconds(start))
        b = float(seconds(start + length))
        i0 = max(0, int(a * fps))
        i1 = min(frames, max(i0 + 1, int(np.ceil(b * fps))))
        if i0 >= frames:
            continue
        chroma[int(key) % 12, i0:i1] += np.exp(-np.clip(centers[i0:i1] - a, 0, None) / DECAY_SECONDS)
    return chroma


def audio_chroma(y, sr, fps):
    import librosa
    hop = 512
    raw = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop, norm=None)
    raw = np.log1p(100 * raw / max(float(raw.max()), 1e-9))
    frames = max(1, int(np.ceil(len(y) / sr * fps)))
    index = np.minimum((librosa.frames_to_time(np.arange(raw.shape[1]), sr=sr, hop_length=hop) * fps).astype(int), frames - 1)
    counts = np.maximum(np.bincount(index, minlength=frames), 1)
    return np.stack([np.bincount(index, weights=raw[b], minlength=frames) / counts for b in range(12)])


def _coarse(notes, written, written_duration, y, sr):
    """Key shift and overall tempo, from a search at two frames per second.

    The shift is how many semitones the recording sounds above the file (a capo the file does not
    write, a guitar tuned differently). The scale is recording seconds per written second: every
    scale on a grid is tried, and each fit is judged by its cost per path step relative to the
    average cost of that whole comparison. Without that relative measure a squeezed score wins:
    many notes per frame make flat chroma vectors, which sit close to any sound.
    Contrast says how much better the best shift fits than the other eleven at that tempo: a
    different song fits every shift about equally badly.
    """
    fps = 2.
    audio = _normalize(audio_chroma(y, sr, fps))

    def fit(scale, shifts):
        frames = max(10, int(np.ceil(written_duration * scale * fps)))
        chroma = score_chroma(notes, lambda tick: written(tick) * scale, fps, frames)
        found = []
        for shift in shifts:
            cost, total, path = _subsequence_dtw(_normalize(np.roll(chroma, shift, axis=0)), audio)
            per_step = float(total[-1, path[-1, 1]]) / len(path)
            found.append((per_step / max(float(cost.mean()), 1e-9), float(scale), shift))
        return found

    results = [r for scale in np.geomspace(.4, 2.5, 12) for r in fit(scale, range(12))]
    cost, scale, shift = min(results)
    others = sorted(c for c, s, k in results if s == scale and k != shift)
    contrast = 1 - cost / max(float(np.median(others)), 1e-9)
    for refined in scale * np.array([.92, .96, 1.04, 1.08]):
        results += fit(refined, [shift])
    _, scale, shift = min(results)
    return (shift if shift <= 6 else shift - 12), scale, contrast


def align(timeline, y, sr):
    """Returns (tick, seconds) anchors and how well the score matched the recording."""
    notes = [n for n in timeline['notes'] if n[1] > 0]
    if not notes:
        raise ValueError('악보에 연주할 음표가 없습니다.')
    written = written_seconds(timeline['tempos'])
    end_tick = max(int(timeline['end']), max(n[0] + n[1] for n in notes))
    written_duration = float(written(end_tick))
    recording = len(y) / sr
    shift, ratio, contrast = _coarse(notes, written, written_duration, y, sr)
    # The fine pass compares the score at the performer's overall tempo, so its diagonal steps
    # are the likely ones and the stretch cost only has to absorb rubato.
    seconds = lambda tick: written(tick) * ratio
    duration = written_duration * ratio

    fps = float(np.clip(np.sqrt(MAX_CELLS / max(duration * recording, 1e-9)), 2, 20))
    frames = max(2, int(np.ceil(duration * fps)))
    score = _normalize(np.roll(score_chroma(notes, seconds, fps, frames), shift % 12, axis=0))
    audio = _normalize(audio_chroma(y, sr, fps))
    cost, _, path = _subsequence_dtw(score, audio)

    # Each score frame's matching recording time, averaged where the path lingers, then kept moving
    # forward: a note later in the score is never placed earlier in the video.
    i, j = path[:, 0], path[:, 1]
    counts = np.maximum(np.bincount(i, minlength=frames), 1)
    matched = (np.bincount(i, weights=j, minlength=frames) / counts + .5) / fps
    matched = np.convolve(np.pad(matched, 2, mode='edge'), np.ones(5) / 5, mode='valid')
    matched = np.maximum.accumulate(matched)
    centers = (np.arange(frames) + .5) / fps

    ticks = sorted({0, end_tick, *(int(b[1]) for b in timeline['bars']), *(max(0, int(n[0])) for n in notes)})
    at = seconds(np.array(ticks, float))
    # Extend the ends of the curve along its slope instead of clamping them.
    slope_start = (matched[min(frames - 1, 4)] - matched[0]) / max(centers[min(frames - 1, 4)] - centers[0], 1e-9)
    slope_end = (matched[-1] - matched[max(0, frames - 5)]) / max(centers[-1] - centers[max(0, frames - 5)], 1e-9)
    times = np.interp(at, centers, matched)
    times = np.where(at < centers[0], matched[0] - (centers[0] - at) * slope_start, times)
    times = np.where(at > centers[-1], matched[-1] + (at - centers[-1]) * slope_end, times)
    times = np.clip(times, 0, recording)
    # Strictly increasing, so the browser can invert the map.
    for k in range(1, len(times)):
        times[k] = max(times[k], times[k - 1] + 1e-3)

    on_path = float(cost[i, j].mean())
    baseline = float(cost.mean())
    match = max(0., 1 - on_path / max(baseline, 1e-9))
    return {
        'anchors': [[int(t), round(float(s), 4)] for t, s in zip(ticks, times)],
        'shift': shift,
        'match': round(match, 3),
        'contrast': round(float(contrast), 3),
        'reliable': bool(contrast >= MIN_CONTRAST and match >= MIN_MATCH),
        'start': round(float(times[0]), 3),
        'end': round(float(times[-1]), 3),
        # Above 1 the performance is faster than the tempo written in the file.
        'tempo_ratio': round(written_duration / max(float(times[-1] - times[0]), 1e-9), 3),
        'fps': round(fps, 2),
    }
