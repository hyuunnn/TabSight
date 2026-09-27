// Read a Guitar Pro file (gp3-5, gpx, gp) as a performed reference for evaluation.
// Unlike score-bridge.mjs `inspect`, repeats and jumps are expanded in playback order, so the
// notes follow what a player records. Times are seconds from the score's tempo map; strings are
// numbered like TabSight (1 = highest). Usage: node scripts/reference-tab.mjs <file>
import fs from 'node:fs';
import * as at from '@coderline/alphatab';

const [input] = process.argv.slice(2);
const settings = new at.Settings();
settings.importer.encoding = 'utf-8';
const score = at.importer.ScoreLoader.loadScoreFromBytes(new Uint8Array(fs.readFileSync(input)), settings);

const guitar = score.tracks.find(t => !t.staves[0]?.isPercussion && t.staves[0]?.tuning?.length >= 6) || score.tracks[0];
const staff = guitar.staves[0];
const stringCount = staff.tuning.length;

// Playback order with repeats comes from alphaTab's own MIDI generation.
const generator = new at.midi.MidiFileGenerator(score, settings, new at.midi.AlphaSynthMidiFileHandler(new at.midi.MidiFile()));
generator.generate();
const lookups = generator.tickLookup.masterBars;

const changes = lookups.flatMap(l => l.tempoChanges.map(c => ({ tick: c.tick, tempo: c.tempo })))
  .sort((a, b) => a.tick - b.tick);
if (!changes.length || changes[0].tick > 0) changes.unshift({ tick: 0, tempo: score.tempo || 120 });
const anchors = [];
let seconds = 0;
for (let i = 0; i < changes.length; i++) {
  if (i) seconds += (changes[i].tick - changes[i - 1].tick) / 960 * 60 / changes[i - 1].tempo;
  anchors.push({ ...changes[i], seconds });
}
function time(tick) {
  let a = anchors[0];
  for (const b of anchors) { if (b.tick <= tick) a = b; else break; }
  return a.seconds + (tick - a.tick) / 960 * 60 / a.tempo;
}
function tempoAt(tick) {
  let a = anchors[0];
  for (const b of anchors) { if (b.tick <= tick) a = b; else break; }
  return a.tempo;
}

const harmonicNames = ['none', 'natural', 'artificial', 'pinch', 'tap', 'semi', 'feedback'];
const bars = [];
const notes = [];
const byNote = new Map();
lookups.forEach((lookup, playIndex) => {
  const mb = lookup.masterBar;
  bars.push({ index: mb.index, start: time(lookup.start), end: time(lookup.end), startTick: lookup.start, endTick: lookup.end,
    numerator: mb.timeSignatureNumerator, denominator: mb.timeSignatureDenominator, tempo: tempoAt(lookup.start),
    anacrusis: !!mb.isAnacrusis, tripletFeel: mb.tripletFeel ?? 0 });
  const bar = staff.bars[mb.index];
  if (!bar) return;
  for (const voice of bar.voices) for (const beat of voice.beats) {
    if (beat.isRest) continue;
    const a = lookup.start + beat.playbackStart;
    const b = a + beat.playbackDuration;
    for (const n of beat.notes) {
      if (n.isTieDestination && n.tieOrigin && byNote.has(n.tieOrigin)) {
        const origin = byNote.get(n.tieOrigin);
        origin.end = Math.max(origin.end, time(b));
        origin.endTick = Math.max(origin.endTick, b);
        byNote.set(n, origin);
        continue;
      }
      const string = stringCount - n.string + 1;
      let technique = 'normal';
      if (n.isHammerPullOrigin) technique = n.hammerPullDestination?.fret < n.fret ? 'pull' : 'hammer';
      if (n.slideOutType) technique = 'slide';
      if (n.isPalmMute) technique = 'mute';
      if (n.vibrato) technique = 'vibrato';
      if (n.hasBend) technique = 'bend';
      if (beat.slap || beat.pop) technique = 'slap';
      const harmonic = harmonicNames[n.harmonicType] || 'none';
      if (harmonic !== 'none') technique = 'harmonic';
      const record = { midi: n.isDead ? null : n.realValue, fret: n.fret, string, start: time(a), end: time(b), startTick: a, endTick: b,
        bar: playIndex, voice: voice.index, technique, harmonic, harmonicValue: n.harmonicValue || 0, dead: !!n.isDead,
        ghost: !!n.isGhost, grace: beat.graceType !== 0, letRing: !!n.isLetRing, tuplet: beat.tupletNumerator > 1 ? [beat.tupletNumerator, beat.tupletDenominator] : null };
      notes.push(record);
      byNote.set(n, record);
    }
  }
});

// A let-ring note keeps sounding until its string is played again.
const nextOnString = new Map();
for (const n of [...notes].sort((x, y) => y.start - x.start)) {
  if (n.letRing) {
    const next = nextOnString.get(n.string);
    n.end = Math.max(n.end, Math.min(next ?? Infinity, n.start + 4));
  }
  nextOnString.set(n.string, n.start);
}

notes.sort((x, y) => x.start - y.start || (x.midi ?? 0) - (y.midi ?? 0));
console.log(JSON.stringify({
  file: input, title: score.title, subtitle: score.subTitle, artist: score.artist, album: score.album, music: score.music,
  words: score.words, tab: score.tab, copyright: score.copyright, instructions: score.instructions, notices: score.notices,
  track: guitar.name, tracks: score.tracks.map(t => ({ name: t.name, percussion: !!t.staves[0]?.isPercussion, capo: t.staves[0]?.capo ?? 0, tuning: [...(t.staves[0]?.tuning || [])].reverse() })),
  tuning: [...staff.tuning].reverse(), capo: staff.capo, transposition: staff.transpositionPitch, tempo: score.tempo,
  duration: bars.length ? bars.at(-1).end : 0, masterBarCount: score.masterBars.length, bars, notes,
}));
