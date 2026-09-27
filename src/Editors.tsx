import {useEffect,useState} from 'react';
import {Check,Plus,Trash2,ArrowRight,RotateCcw,Sparkles} from 'lucide-react';
import {type Project,type Note,type Bar,type CapoSegment,type Settings,techniques,pitch,notePitch,clock,harmonicIntervals} from './types';

export function NoteEditor({note,project,disabled,onSave,onDelete}:{note:Note;project:Project;disabled:boolean;onSave:(n:Note)=>void;onDelete:()=>void}){
 const [draft,setDraft]=useState(note);useEffect(()=>setDraft(note),[note]);
 const change=(key:keyof Note,value:any)=>setDraft(n=>{const next={...n,[key]:value};if(key==='technique'&&value==='percussion'){next.string=0;next.fret=0;next.midi=37;}if(key==='technique'&&value==='harmonic'&&!harmonicIntervals[next.fret])next.fret=12;if(next.string&&['string','fret','start','technique'].includes(key))next.midi=notePitch(next,project);return next;});
 return <form className="note-editor" onSubmit={e=>{e.preventDefault();onSave({...draft,reviewed:true});}}><div className="editor-heading"><div><span className="eyebrow">선택한 음표</span><h3>{draft.technique==='percussion'?'바디 퍼커션':pitch(draft.midi)} <span>{clock(draft.start)} · {draft.start.toFixed(2)}초</span></h3></div><button type="button" className="icon-button danger" title="음표 삭제" aria-label="음표 삭제" onClick={onDelete} disabled={disabled}><Trash2 size={17}/></button></div><div className="field-grid">
 {draft.technique!=='percussion'&&<><label>줄<select aria-label="음표 줄" value={draft.string} onChange={e=>change('string',+e.target.value)}><option value={0}>미정</option>{[1,2,3,4,5,6].map(s=><option key={s} value={s}>{s}번 줄</option>)}</select></label>
 <label>프렛<input aria-label="음표 프렛" type="number" min={0} max={24} value={draft.fret} onChange={e=>change('fret',+e.target.value)}/></label></>}
 <label>시작 (초)<input aria-label="음표 시작" type="number" step="any" min={0} value={draft.start} onChange={e=>change('start',+e.target.value)}/></label>
 <label>끝 (초)<input aria-label="음표 끝" type="number" step="any" min={draft.start+.01} value={draft.end} onChange={e=>change('end',+e.target.value)}/></label>
 <label className="wide">연주 기법<select aria-label="연주 기법" value={draft.technique} onChange={e=>change('technique',e.target.value)}>{Object.entries(techniques).map(([v,l])=><option key={v} value={v}>{l}</option>)}</select></label>
 </div><div className="note-evidence">{!draft.reviewed&&draft.evidence.includes('harmonic-candidate')&&<p>하모닉스일 가능성이 있습니다. 터치 프렛과 줄을 확인해 주세요.</p>}음성과 연주 가능한 운지를 바탕으로 추정{draft.evidence.includes('imported-score')&&' · 기존 악보'}</div><button className="primary small full" disabled={disabled||draft.end<=draft.start}><Check size={16}/>수정 저장 · 검토 완료</button></form>;
}

export function SettingsEditor({project,tunings,disabled,time,onApply}:{project:Project;tunings:Record<string,number[]>;disabled:boolean;time:number;onApply:(v:any)=>void}){
 const [tuning,setTuning]=useState(project.tuning);const [capo,setCapo]=useState(project.capo);const [segments,setSegments]=useState(project.capo_segments);
 useEffect(()=>{setTuning(project.tuning);setCapo(project.capo);setSegments(project.capo_segments);},[project.id,project.revision]);
 const changed=tuning.join()!==project.tuning.join()||capo!==project.capo||JSON.stringify(segments)!==JSON.stringify(project.capo_segments);
 // Same rule as the server: a note needs a string whose fret 0-24, counted from the capo, gives its pitch.
 const draftCapo=(t:number)=>[...segments].sort((a,b)=>a.start-b.start).filter(s=>s.start<=t).at(-1)?.capo??capo;
 const blocked=changed?project.notes.filter(n=>n.technique!=='percussion'&&!tuning.some(open=>{const f=n.midi-open-draftCapo(n.start);return (f>=0&&f<=24)||(n.technique==='harmonic'&&Object.values(harmonicIntervals).includes(f));})):[];
 const lowestOpen=Math.min(...tuning);const tooLow=blocked.filter(n=>n.midi<lowestOpen+draftCapo(n.start));const lowestNote=tooLow.reduce<Note|null>((a,n)=>!a||n.midi<a.midi?n:a,null);
 const lowestCapo=lowestNote?draftCapo(lowestNote.start):0;
 const confirmed=!!project.metadata.settings_confirmed;
 // When the confirmed tuning left notes without a string, the analysis may name a preset that fits them.
 const suggested:number[]|undefined=project.metadata.suggested_tuning;
 const suggestion=suggested&&![project.tuning.join(),tuning.join()].includes(suggested.join())&&project.notes.some(n=>!n.string&&n.technique!=='percussion')?Object.entries(tunings).find(([,v])=>v.join()===suggested.join())?.[0]??suggested.map(pitch).join(' '):null;
 return <div className="settings-editor"><div className="section-heading"><h3>튜닝 & 카포</h3><span className="tag neutral">{confirmed?'직접 확인한 설정':project.metadata.tuning_source==='description'?'영상 설명 기준':'자동 추정 · 확인 필요'}</span></div><TuningFields tunings={tunings} tuning={tuning} capo={capo} segments={segments} time={time} onTuning={setTuning} onCapo={c=>setCapo(c??0)} onSegments={setSegments}/>{suggestion&&<p className="settings-hint">확인한 설정으로 배치하지 못한 음이 있어요. 카포가 맞다면 소리로는 {suggestion} 튜닝이 더 맞아 보여요. <button className="text-button" onClick={()=>setTuning([...suggested!])}>{suggestion} 불러오기</button></p>}{project.source!=='score'&&!confirmed&&project.metadata.capo_source!=='description'&&<p className="settings-hint">카포는 소리만으로 구분할 수 없어 추정한 값이에요. 영상에서 카포 위치를 확인해 주세요.</p>}<p className="muted tiny">프렛은 카포를 0으로 센 값입니다. 보정 시 원래 음높이를 유지하며 운지를 다시 배치합니다.</p>{blocked.length>0&&<p className="settings-warning" role="status">이 설정의 음역을 벗어나는 음이 {blocked.length}개 있어요.{lowestNote&&` ${tooLow.length}개는 가장 낮은 줄인 ${6-tuning.indexOf(lowestOpen)}번 줄(${pitch(lowestOpen+lowestCapo)}${lowestCapo?`, 카포 ${lowestCapo} 포함`:''})보다 낮아요(가장 낮은 음 ${pitch(lowestNote.midi)}).`}{blocked.length>tooLow.length&&` ${blocked.length-tooLow.length}개는 24프렛보다 높아요.`} 줄의 옥타브(A1·A2의 숫자)와 카포를 확인해 주세요. 그대로 적용하면 이 음들은 운지 미정으로 남아요.</p>}{changed&&<button className="primary small full" disabled={disabled} onClick={()=>onApply({tuning,capo,capo_segments:segments,allow_unplayable:true})}><RotateCcw size={14}/>원음 유지하고 운지 다시 계산</button>}</div>;
}

// Tuning preset, open strings, base capo and timed capo changes. Before transcription the tuning or
// capo stays empty when the description names neither, so the player enters it rather than a guess.
function TuningFields({tunings,tuning,capo,segments,time,onTuning,onCapo,onSegments}:{tunings:Record<string,number[]>;tuning:number[]|null;capo:number|null;segments:CapoSegment[];time:number;onTuning:(t:number[])=>void;onCapo:(c:number|null)=>void;onSegments:(s:CapoSegment[])=>void}){
 const preset=!tuning?'':Object.entries(tunings).find(([,v])=>v.join()===tuning.join())?.[0]||'custom';
 return <><div className="tuning-row"><label>튜닝<select aria-label="튜닝 프리셋" value={preset} onChange={e=>{if(tunings[e.target.value])onTuning([...tunings[e.target.value]]);}}>{!tuning&&<option value="" disabled>튜닝 선택</option>}{Object.keys(tunings).map(k=><option key={k}>{k}</option>)}{preset==='custom'&&<option value="custom" disabled>사용자 지정</option>}</select></label><label>카포<input aria-label="기본 카포" type="number" value={capo??''} min={0} max={12} onChange={e=>onCapo(e.target.value===''?null:+e.target.value)}/></label><span className="fret-unit">프렛</span></div>{tuning&&<div className="six-strings">{tuning.map((v,i)=><label key={i}><span>{6-i}번 줄</span><select aria-label={`${6-i}번 줄 튜닝`} value={v} onChange={e=>onTuning(tuning.map((x,j)=>j===i?+e.target.value:x))}>{Array.from({length:61},(_,j)=>j+24).map(n=><option key={n} value={n}>{pitch(n)}</option>)}</select></label>)}</div>}<details className="capo-segments"><summary>구간별 카포 변경 {segments.length>0&&`· ${segments.length}개`}</summary>{segments.map((s,i)=><div className="segment-row" key={i}><label>시작 (초)<input type="number" min={0} step="any" value={s.start} onChange={e=>onSegments(segments.map((v,j)=>j===i?{...v,start:+e.target.value}:v))}/></label><ArrowRight size={14}/><label>카포<input type="number" min={0} max={12} value={s.capo} onChange={e=>onSegments(segments.map((v,j)=>j===i?{...v,capo:+e.target.value}:v))}/></label><button type="button" className="icon-button" title="카포 구간 삭제" onClick={()=>onSegments(segments.filter((_,j)=>i!==j))}><Trash2 size={14}/></button></div>)}<button type="button" className="text-button" onClick={()=>onSegments([...segments,{start:+time.toFixed(1),capo:capo??0}])}><Plus size={14}/>현재 위치에 변경 추가</button></details></>;
}

// Shown between preparing the media and transcribing it: fingering follows these settings, and
// the capo cannot be told apart by sound, so the player checks them against the video first.
export function SettingsConfirm({project,tunings,disabled,time,onStart}:{project:Project;tunings:Record<string,number[]>;disabled:boolean;time:number;onStart:(v:Settings)=>void}){
 const detected:{tuning:number[]|null;capos:number[];text:string}={tuning:null,capos:[],text:'',...project.metadata.detected_settings};
 const previous=!!project.metadata.settings_confirmed;const described=!!detected.tuning;const capos=detected.capos.join(', ');
 // Filled in only from the description or an earlier confirmation; otherwise the player enters them.
 const [tuning,setTuning]=useState<number[]|null>(previous||described?project.tuning:null);
 const [capo,setCapo]=useState<number|null>(previous||described||detected.capos.length?project.capo:null);
 const [segments,setSegments]=useState(project.capo_segments);
 const capoValid=capo!==null&&Number.isInteger(capo)&&capo>=0&&capo<=12;
 const missing=tuning===null&&!capoValid?'튜닝을 고르고 카포 프렛을 입력하면 시작할 수 있어요. 카포가 없으면 0이에요.':tuning===null?'튜닝을 고르면 시작할 수 있어요.':!capoValid?'카포 프렛을 0~12로 입력하면 시작할 수 있어요. 카포가 없으면 0이에요.':!segments.every(s=>s.start>=0&&Number.isInteger(s.capo)&&s.capo>=0&&s.capo<=12)?'구간별 카포는 0~12프렛, 시작은 0초 이후로 입력해 주세요.':'';
 const source=previous?'지난번 채보에 쓴 설정을 채워 두었어요.':described?'영상 설명에서 찾은 설정을 채워 두었어요.':`${detected.text?'설명의 튜닝 표기를 읽지 못했어요.':project.source==='youtube'?'영상 설명에 튜닝 정보가 없어요.':'가져온 파일에는 튜닝 정보가 없어요.'} ${detected.capos.length?'튜닝을 직접 골라':'튜닝과 카포를 직접 입력해'} 주세요.`;
 return <div className="settings-confirm"><span className="eyebrow">BEFORE TRANSCRIPTION</span><h2>튜닝과 카포를 확인해 주세요</h2><p className="confirm-intro">채보는 이 설정으로 줄과 프렛을 정해요. 원본 연주를 재생해 카포 위치와 줄 튜닝을 확인한 뒤 시작하세요.</p>
 <div className={`settings-source ${previous||described?'':'missing'}`}><p>{source}</p>{detected.text&&<q>{detected.text}</q>}{!previous&&!described&&detected.capos.length>0&&<p>카포 {capos}프렛은 설명에서 찾아 채워 두었어요.</p>}</div>
 <TuningFields tunings={tunings} tuning={tuning} capo={capo} segments={segments} time={time} onTuning={setTuning} onCapo={setCapo} onSegments={setSegments}/>
 {!previous&&described&&!detected.capos.length&&<p className="settings-hint">설명에 카포 표기가 없어 카포 0(없음)으로 두었어요. 영상에 카포가 보이면 바꿔 주세요.</p>}
 {detected.capos.length>1&&!segments.length&&<p className="settings-hint">설명에 카포 {capos}프렛이 있어요. 곡 중간에 카포를 옮긴다면 영상을 그 시점으로 옮긴 뒤 구간별 카포 변경을 추가해 주세요.</p>}
 <p className="muted tiny">카포는 소리로 구분할 수 없어 영상으로 확인해야 해요. 프렛은 카포를 0으로 센 값이에요.</p>
 <button className="primary full" disabled={disabled||!!missing} onClick={()=>{if(tuning&&capo!==null&&!missing)onStart({tuning,capo,capo_segments:segments});}}><Sparkles size={16}/>이 설정으로 채보 시작</button>
 {missing&&<p className="muted tiny confirm-missing" role="status">{missing}</p>}</div>;
}

export function BarEditor({project,index,disabled,onSave}:{project:Project;index:number;disabled:boolean;onSave:(bar:Bar,all:boolean)=>void}){
 const bar=project.bars[index];const [draft,setDraft]=useState(bar);const [all,setAll]=useState(false);useEffect(()=>{setDraft(bar);setAll(false);},[bar,index]);if(!draft)return null;
 return <form className="bar-editor" onSubmit={e=>{e.preventDefault();onSave(draft,all);}}><div className="section-heading"><h3>{index+1}마디 · 박자 보정</h3><span className="muted tiny">영상 시간 기준</span></div><div className="field-grid"><label>박자 분자<input type="number" aria-label="박자 분자" min={1} max={12} value={draft.numerator} onChange={e=>setDraft({...draft,numerator:+e.target.value})}/></label><label>박자 분모<select value={draft.denominator} onChange={e=>setDraft({...draft,denominator:+e.target.value})}>{[2,4,8,16].map(v=><option key={v}>{v}</option>)}</select></label><label>템포 BPM<input type="number" aria-label="마디 템포" step="any" min={20} max={300} value={+draft.tempo.toFixed(1)} onChange={e=>setDraft({...draft,tempo:+e.target.value})}/></label><label>마디 시작 (초)<input type="number" min={0} step="any" value={+draft.start.toFixed(2)} onChange={e=>setDraft({...draft,start:+e.target.value})}/></label></div><label className="checkbox-label"><input type="checkbox" checked={all} onChange={e=>setAll(e.target.checked)}/>이 마디부터 같은 박자·템포 적용</label><p className="muted tiny">템포를 바꾸면 마디 길이와 이후 경계가 바뀝니다. 음표의 영상 시간은 유지됩니다.</p><button className="secondary small full" disabled={disabled}>마디 보정 저장</button></form>;
}
