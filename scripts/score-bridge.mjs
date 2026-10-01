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
  const legato=n=>n.slideOutType?'slide':n.isHammerPullOrigin?(n.hammerPullDestination?.fret<n.fret?'pull':'hammer'):null;
  for (let bi=0;bi<staff.bars.length;bi++) {
    const bar=staff.bars[bi];const timing=bars[bi];
    const ticks=score.masterBars[bi].calculateDuration();
    for (const voice of bar.voices) for (const beat of voice.beats) {
      const start=timing.start+(beat.playbackStart/ticks)*(timing.end-timing.start);
      const end=start+(beat.playbackDuration/ticks)*(timing.end-timing.start);
      for (const n of beat.notes) {
        if(n.isTieDestination) {
          const prev=notes.findLast(x=>x.string===7-n.string && x.fret===n.fret);
          // A hammer-on, pull-off or slide leaves from the last tied segment of its note, so it is read there too.
          if(prev){prev.end=end;const tie=legato(n);if(tie&&['normal','hammer','pull'].includes(prev.technique))prev.technique=tie;}
          continue;
        }
        let technique=legato(n)??'normal';
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
} else if (mode === 'timeline') {
  // The score in playback order, as alphaTab's player plays it: repeats and alternate endings
  // unrolled, on the same tick axis as the browser's tickPosition (960 ticks per quarter note).
  // The sync step matches these notes against the video's sound.
  const guitar=t=>t.staves[0]&&!t.staves[0].isPercussion&&t.staves[0].tuning.length>0;
  const track=score.tracks.find(guitar);
  if(!track){console.error('No guitar track');process.exit(3);}
  const staff=track.staves[0];const notes=[];const tempos=[];
  const ignore=()=>{};
  const handler={addTickShift:ignore,addTimeSignature:ignore,addRest:ignore,addControlChange:ignore,addProgramChange:ignore,addBend:ignore,addNoteBend:ignore,finishTrack:ignore,
    addTempo:(tick,bpm)=>tempos.push([tick,bpm]),
    addNote:(trackIndex,start,length,key)=>{if(trackIndex===track.index)notes.push([start,length,key]);}};
  const generator=new at.midi.MidiFileGenerator(score,settings,handler);
  generator.generate();
  const played=generator.tickLookup.masterBars.map(b=>[b.masterBar.index,b.start,b.end]);
  console.log(JSON.stringify({title:score.title,artist:score.artist,track:track.index,track_name:track.name,track_count:score.tracks.length,
    tuning:[...staff.tuning].reverse(),capo:staff.capo,tempo:score.tempo,bar_count:score.masterBars.length,
    end:played.length?played.at(-1)[2]:0,tempos,bars:played,notes}));
} else throw new Error('Unknown mode');
