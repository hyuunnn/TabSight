import {useEffect,useRef,useState} from 'react';
import * as at from '@coderline/alphatab';
import {LoaderCircle,Music2} from 'lucide-react';
import {type Project,type Note,isSynced} from './types';

export type ScoreControl={play:()=>boolean;pause:()=>void;seek:(time:number)=>void;speed:(rate:number)=>void;isReady:()=>boolean;follow:()=>void};
type Props={project:Project;staff:boolean;zoom:number;time:number;followPlayback:boolean;onSelect:(note:Note)=>void;onSeek:(time:number)=>void;onPosition:(time:number)=>void;onPlaying:(value:boolean)=>void;onLocation?:(bar:number,beat:number)=>void;control:React.RefObject<ScoreControl|null>};

export default function Score(props:Props){
 const host=useRef<HTMLDivElement>(null);const apiRef=useRef<at.AlphaTabApi|null>(null);const latest=useRef(props);latest.current=props;
 const followRef=useRef<(()=>void)|null>(null);
 const [busy,setBusy]=useState(true);const [error,setError]=useState('');
 // An uploaded score is timed by the server's [tick, seconds] anchors, interpolated either way.
 // A transcription is timed by its own bars, one per master bar.
 const synced=()=>isSynced(latest.current.project);
 const anchored=(value:number,from:0|1)=>{const a=latest.current.project.sync!;const to=1-from;if(value<=a[0][from])return a[0][to];if(value>=a[a.length-1][from])return a[a.length-1][to];let lo=0,hi=a.length-1;while(hi-lo>1){const mid=(lo+hi)>>1;if(a[mid][from]<=value)lo=mid;else hi=mid;}return a[lo][to]+(value-a[lo][from])/(a[hi][from]-a[lo][from])*(a[hi][to]-a[lo][to]);};
 const toTick=(time:number)=>{if(synced())return anchored(time,1);const p=latest.current.project;const api=apiRef.current;if(!api?.score)return 0;const idx=Math.max(0,p.bars.findLastIndex(b=>b.start<=time));const b=p.bars[idx];const mb=api.score.masterBars[idx];return mb&&b?mb.start+Math.max(0,Math.min(1,(time-b.start)/(b.end-b.start)))*mb.calculateDuration():0;};
 const fromTick=(tick:number)=>{if(synced())return anchored(tick,0);const api=apiRef.current;const p=latest.current.project;if(!api?.score)return 0;const i=api.score.masterBars.findLastIndex(b=>b.start<=tick);const mb=api.score.masterBars[i];const b=p.bars[i];return b&&mb?b.start+(tick-mb.start)/mb.calculateDuration()*(b.end-b.start):0;};
 // Original-media time where a rendered beat starts, from its bar's actual start and end.
 const beatTime=(beat:at.model.Beat)=>{const b=latest.current.project.bars[beat.voice.bar.index];const mb=beat.voice.bar.masterBar;return b?b.start+beat.playbackStart/mb.calculateDuration()*(b.end-b.start):undefined;};
 // Where a clicked beat is played. An uploaded score may repeat a bar, so it takes the playing of
 // that bar closest to the current position.
 const clickedTime=(beat:at.model.Beat)=>{if(!synced())return beatTime(beat);const cache=apiRef.current?.tickCache;if(!cache)return undefined;const offset=cache.getRelativeBeatPlaybackRange(beat)?.startTick??beat.playbackStart;const now=latest.current.time;const times=cache.masterBars.filter(mb=>mb.masterBar.index===beat.voice.bar.masterBar.index).map(mb=>fromTick(mb.start+offset));return times.length?times.reduce((a,b)=>Math.abs(b-now)<Math.abs(a-now)?b:a):undefined;};
 const canonical=(n:at.model.Note)=>{const p=latest.current.project;const t=beatTime(n.beat);if(t===undefined)return undefined;const candidates=p.notes.filter(x=>(n.beat.voice.bar.staff.isPercussion?x.technique==='percussion':x.string===7-n.string)&&Math.abs(x.start-t)<.3);return candidates.sort((a,b)=>Math.abs(a.start-t)-Math.abs(b.start-t))[0];};
 useEffect(()=>{
  if(!host.current)return;
  const element=host.current;
  const viewport=element.closest<HTMLElement>('.score-scroll');
  // Follow both the original media and the synth through the same bounds lookup.
  // alphaTab only runs its built-in auto-scroll/highlighting while its synth plays.
  const api=new at.AlphaTabApi(element,{core:{includeNoteBounds:true,fontDirectory:'/alphatab/font/',scriptFile:'/alphatab/alphaTab.mjs'},display:{scale:props.zoom/100,layoutMode:'page',barsPerRow:element.clientWidth<500?1:2,padding:[18,24,18,24]},player:{enablePlayer:true,soundFont:'/alphatab/soundfont/sonivox.sf2',enableCursor:true,enableUserInteraction:true,scrollElement:viewport||element,scrollMode:'off'}});
  for(const key of ['ScoreTitle','ScoreSubTitle','ScoreArtist','ScoreAlbum','ScoreWords','ScoreMusic','ScoreCopyright','ScoreWordsAndMusic'] as const)api.settings.notation.elements.set(at.NotationElement[key],false);
  apiRef.current=api;
  let trackIds=new Set<number>();
  let highlighted:Element[]=[];
  let lastHighlight='';
  let lastRow:number|null=null;
  let pendingFrame=0;
  const syncVisuals=(tick:number,force=false)=>{
   const lookup=api.tickCache?.findBeat(trackIds,tick);
   if(!lookup)return;
   if(synced()){
    // Bar and beat for the position label; the page counts them from its own bars otherwise.
    const mb=lookup.masterBar.masterBar;
    latest.current.onLocation?.(mb.index+1,Math.max(1,Math.min(mb.timeSignatureNumerator,Math.floor((tick-lookup.masterBar.start)/(3840/mb.timeSignatureDenominator))+1)));
   }
   const bounds=api.renderer.boundsLookup?.findBeat(lookup.beat);
   if(!bounds)return;
   element.dataset.playbackBar=String(lookup.beat.voice.bar.index+1);
   const beats=lookup.beatLookup.highlightedBeats.filter(item=>trackIds.has(item.beat.voice.bar.staff.track.index));
   const key=beats.map(item=>item.beat.id).join(',');
   if(force||key!==lastHighlight||!highlighted.length){
    for(const node of highlighted)node.classList.remove('tabsight-active-beat');
    // alphaTab's SVG beat groups use b<beat.id>. Use our own class so a paused
    // synth's cursor refresh cannot erase the original media's highlight.
    highlighted=beats.flatMap(item=>Array.from(element.querySelectorAll(`.b${item.beat.id}`)));
    for(const node of highlighted)node.classList.add('tabsight-active-beat');
    lastHighlight=key;
   }
   if(!viewport||(!latest.current.followPlayback&&!force))return;
   const bar=bounds.barBounds.masterBarBounds.visualBounds;
   const changedRow=lastRow!==bar.y;
   if(!force&&!changedRow)return;
   const first=lastRow===null;lastRow=bar.y;
   const hostTop=element.getBoundingClientRect().top-viewport.getBoundingClientRect().top+viewport.scrollTop;
   const top=hostTop+bar.y;
   const bottom=top+bar.h;
   if(first&&!force&&top>=viewport.scrollTop+16&&bottom<=viewport.scrollTop+viewport.clientHeight-16)return;
   const target=Math.max(0,Math.min(viewport.scrollHeight-viewport.clientHeight,top-24));
   if(Math.abs(viewport.scrollTop-target)<1)return;
   const instant=force||Math.abs(viewport.scrollTop-target)>viewport.clientHeight||window.matchMedia('(prefers-reduced-motion: reduce)').matches;
   viewport.scrollTo({top:target,behavior:instant?'instant':'smooth'});
  };
  const follow=()=>syncVisuals(toTick(latest.current.time),true);
  followRef.current=follow;
  // The app's own GP5 repeats a tempo change per track and marks notes to review. An uploaded score
  // is shown as written, apart from the TAB/staff choice.
  api.scoreLoaded.on(score=>{const own=!synced();if(own)for(const mb of score.masterBars){mb.tempoAutomations=mb.tempoAutomations.filter((a,i,all)=>!all.slice(0,i).some(b=>a.ratioPosition===b.ratioPosition&&a.value===b.value));for(const a of mb.tempoAutomations)a.text='';}for(const t of score.tracks)for(const s of t.staves){s.showStandardNotation=s.isPercussion||latest.current.staff;s.showTablature=!s.isPercussion;if(own)for(const b of s.bars)for(const v of b.voices)for(const beat of v.beats)for(const n of beat.notes){const orig=canonical(n);if(orig&&!orig.reviewed&&orig.confidence<.65){n.style=new at.model.NoteStyle();n.style.colors.set(at.model.NoteSubElement.GuitarTabFretNumber,new at.model.Color(177,112,29));}}}});
  // Only the tracks on the page: an uploaded score shows the one it was timed with.
  api.scoreLoaded.on(score=>{trackIds=synced()?new Set([latest.current.project.metadata.score_file?.track??0]):new Set(score.tracks.map(track=>track.index));lastRow=null;lastHighlight='';highlighted=[];});
  const restorePosition=()=>{
   cancelAnimationFrame(pendingFrame);
   pendingFrame=requestAnimationFrame(()=>{for(const node of highlighted)node.classList.remove('tabsight-active-beat');lastHighlight='';highlighted=[];lastRow=null;const tick=toTick(latest.current.time);api.tickPosition=tick;syncVisuals(tick);});
  };
  api.renderFinished.on(()=>setBusy(false));
  api.postRenderFinished.on(restorePosition);
  api.midiLoaded.on(restorePosition);
  api.error.on(e=>{setBusy(false);setError(String(e.message||e));});
  // alphaTab moves only its own player to a clicked beat, and the video follows that in 악보음 mode
  // alone. Seek both here, so 원음 mode also goes to a click beside a fret number or on a tied one.
  let pendingSeek:number|null=null;
  api.beatMouseDown.on(beat=>{const t=clickedTime(beat);pendingSeek=synced()&&t!==undefined?t:null;if(t!==undefined)latest.current.onSeek(t);});
  // On release alphaTab puts its position on a bar's first playing. For a repeated bar of an
  // uploaded score, keep the playing chosen on the press (a dragged range is left to alphaTab).
  api.beatMouseUp.on(()=>{if(pendingSeek!==null&&!api.playbackRange)latest.current.onSeek(pendingSeek);pendingSeek=null;});
  // A fret number then also selects its note and goes to where the note actually starts.
  api.noteMouseDown.on(n=>{const original=canonical(n);if(original){latest.current.onSelect(original);latest.current.onSeek(original.start);}});
  api.playerPositionChanged.on(e=>{syncVisuals(e.currentTick);latest.current.onPosition(fromTick(e.currentTick));});
  api.playerStateChanged.on(e=>latest.current.onPlaying(e.state===at.synth.PlayerState.Playing));
  props.control.current={play:()=>{if(latest.current.followPlayback)follow();return api.play();},pause:()=>api.pause(),seek:time=>{const tick=toTick(time);api.tickPosition=tick;syncVisuals(tick);},speed:rate=>{api.playbackSpeed=rate;},isReady:()=>api.isReadyForPlayback,follow};
  const resize=new ResizeObserver(()=>{
   const barsPerRow=element.clientWidth<500?1:2;
   if(api.settings.display.barsPerRow!==barsPerRow){api.settings.display.barsPerRow=barsPerRow;api.updateSettings();if(api.score)api.render();}
  });
  resize.observe(element);
  return()=>{resize.disconnect();cancelAnimationFrame(pendingFrame);followRef.current=null;props.control.current=null;apiRef.current=null;api.destroy();};
 },[props.project.id]);
 useEffect(()=>{const api=apiRef.current;if(!api)return;setBusy(true);setError('');const abort=new AbortController();const p=props.project;const uploaded=isSynced(p);
  // An uploaded score is displayed from the file itself, with the track it was timed with. A
  // transcription is rendered from the app's GP5, one track per capo plus body percussion.
  const url=uploaded?`/api/projects/${p.id}/score/original?r=${p.revision}`:`/api/projects/${p.id}/score/gp5?preview=true&r=${p.revision}`;
  const tracks=uploaded?[p.metadata.score_file?.track??0]:Array.from({length:new Set([p.capo,...p.capo_segments.map(s=>s.capo)]).size+(p.notes.some(n=>n.technique==='percussion')?1:0)},(_,i)=>i);
  fetch(url,{signal:abort.signal}).then(async r=>{if(!r.ok){const e=await r.json();throw new Error(e.detail);}return new Uint8Array(await r.arrayBuffer());}).then(data=>{if(!abort.signal.aborted)api.load(data,tracks);}).catch(e=>{if(e.name!=='AbortError'){setError(e.message);setBusy(false);}});return()=>abort.abort();},[props.project.id,props.project.revision,isSynced(props.project)]);
 useEffect(()=>{const api=apiRef.current;if(!api)return;api.settings.display.scale=props.zoom/100;api.updateSettings();if(api.score){for(const t of api.score.tracks)for(const s of t.staves)s.showStandardNotation=s.isPercussion||props.staff;api.render();}},[props.staff,props.zoom]);
 useEffect(()=>{if(props.followPlayback)followRef.current?.();},[props.followPlayback]);
 return <><div ref={host} className="alpha-host"/>{busy&&<div className="score-loading"><LoaderCircle className="spin" size={22}/><span>악보를 그리는 중</span></div>}{error&&<div className="empty-score"><Music2/><p>{error}</p></div>}</>;
}
