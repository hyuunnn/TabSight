import fs from 'node:fs';
import * as at from '@coderline/alphatab';

const [mode, input, output] = process.argv.slice(2);
const settings = new at.Settings();
settings.importer.encoding = 'utf-8';
const score = at.importer.ScoreLoader.loadScoreFromBytes(new Uint8Array(fs.readFileSync(input)), settings);
if (mode === 'convert') {
  fs.writeFileSync(output, new at.exporter.Gp7Exporter().export(score, settings));
} else if (mode === 'inspect') {
  const bars=[]; let time=0;
  let tempo=score.tempo || 90;
  for (const mb of score.masterBars) {
    if (mb.tempoAutomations?.length) tempo=mb.tempoAutomations.at(-1).value;
    const duration=mb.calculateDuration()/960*60/tempo;
    bars.push({start:time,end:time+duration,numerator:mb.timeSignatureNumerator,denominator:mb.timeSignatureDenominator,tempo});
    time+=duration;
  }
  const track=score.tracks.find(t=>!t.staves[0]?.isPercussion) || score.tracks[0];
  const staff=track.staves[0];const notes=[];
  for (let bi=0;bi<staff.bars.length;bi++) {
    const bar=staff.bars[bi];const timing=bars[bi];
    const ticks=score.masterBars[bi].calculateDuration();
    for (const voice of bar.voices) for (const beat of voice.beats) {
      const start=timing.start+(beat.playbackStart/ticks)*(timing.end-timing.start);
      const end=start+(beat.playbackDuration/ticks)*(timing.end-timing.start);
      for (const n of beat.notes) {
        if(n.isTieDestination) {
          const prev=notes.findLast(x=>x.string===7-n.string && x.fret===n.fret);
          if(prev) prev.end=end;
          continue;
        }
        let technique='normal';
        if(n.isHammerPullOrigin)technique=n.hammerPullDestination?.fret<n.fret?'pull':'hammer';
        if(n.slideOutType)technique='slide';
        if(n.harmonicType)technique='harmonic';
        if(n.isPalmMute)technique='mute';
        if(n.vibrato)technique='vibrato';
        if(n.hasBend)technique='bend';
        if(beat.slap)technique='slap';
        notes.push({id:`import-${bi}-${voice.index}-${beat.index}-${n.index}`,midi:n.realValue,start,end:Math.max(start+.02,end),string:Math.max(0,7-n.string),fret:Math.max(0,n.fret),velocity:80,confidence:1,technique,reviewed:true,evidence:['imported-score']});
      }
    }
  }
  console.log(JSON.stringify({title:score.title||'가져온 악보',tuning:[...staff.tuning].reverse(),capo:staff.capo,tempo:score.tempo||90,duration:time,bars,notes,metadata:{channel:score.artist,imported_track:track.name,track_count:score.tracks.length}}));
} else throw new Error('Unknown mode');
