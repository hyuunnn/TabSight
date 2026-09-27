export type Note={id:string;midi:number;start:number;end:number;string:number;fret:number;velocity:number;confidence:number;technique:string;reviewed:boolean;evidence:string[]};
export type Bar={start:number;end:number;numerator:number;denominator:number;tempo:number;beats?:number[]};
export type CapoSegment={start:number;capo:number};
export type Settings={tuning:number[];capo:number;capo_segments:CapoSegment[]};
export type Project={id:string;title:string;url:string;video_id:string;source:string;status:string;stage:string;progress:number;error:string;revision:number;duration:number;analysis_seconds:number;tuning:number[];capo:number;capo_segments:CapoSegment[];tempo:number;notes:Note[];bars:Bar[];warnings:string[];metadata:Record<string,any>;metrics:Record<string,any>};
export type Summary=Pick<Project,'id'|'title'|'status'|'stage'|'progress'|'source'|'duration'|'video_id'>&{note_count:number;review_count:number};
export const techniques:Record<string,string>={normal:'일반음',hammer:'해머온',pull:'풀오프',slide:'슬라이드',harmonic:'하모닉스',mute:'팜 뮤트',slap:'슬랩',percussion:'바디 퍼커션',bend:'벤딩',vibrato:'비브라토'};
export const pitch=(n:number)=>['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'][((n%12)+12)%12]+(Math.floor(n/12)-1);
export const clock=(s:number)=>`${Math.floor(Math.max(0,s)/60)}:${Math.floor(Math.max(0,s)%60).toString().padStart(2,'0')}`;
export function capoAt(p:Project,t:number){return [...p.capo_segments].sort((a,b)=>a.start-b.start).filter(x=>x.start<=t).at(-1)?.capo??p.capo;}
export const harmonicIntervals:Record<number,number>={12:12,7:19,5:24,4:28,9:28,3:31};
export function notePitch(n:Note,p:Project){return p.tuning[6-n.string]+capoAt(p,n.start)+(n.technique==='harmonic'?(harmonicIntervals[n.fret]??n.fret):n.fret);}
// Bars carry the tracked beat times, so a bar's ticks follow the player's timing beat by beat
// (server/rhythm.py bar_beats). Beats that no longer fit the bar, e.g. after an edit, fall back to equal steps.
const beatCount=(b:Bar)=>b.denominator===8&&b.numerator%3===0&&b.numerator>3?b.numerator/3:b.numerator;
function beatTimes(b:Bar){const n=beatCount(b);const t=b.beats??[];const ok=t.length===n&&Math.abs(t[0]-b.start)<1e-3&&t.every((x,i)=>!i||x>t[i-1])&&t[n-1]<b.end;return [...(ok?t:Array.from({length:n},(_,i)=>b.start+i*(b.end-b.start)/n)),b.end];}
export function barFraction(b:Bar,time:number){const t=beatTimes(b);const n=t.length-1;const k=Math.max(0,Math.min(n-1,t.findLastIndex(x=>x<=time)));return Math.max(0,Math.min(1,(k+(time-t[k])/(t[k+1]-t[k]))/n));}
export function barTime(b:Bar,fraction:number){const t=beatTimes(b);const n=t.length-1;const x=Math.max(0,Math.min(1,fraction))*n;const k=Math.min(n-1,Math.floor(x));return t[k]+(x-k)*(t[k+1]-t[k]);}
export async function request<T=Project>(path:string,options:RequestInit={}):Promise<T>{
 const r=await fetch(path,{...options,headers:options.body instanceof FormData?{}:{'Content-Type':'application/json',...options.headers}});
 if(!r.ok){const b=await r.json().catch(()=>({detail:`요청 실패 (${r.status})`}));throw new Error(typeof b.detail==='string'?b.detail:'입력값을 확인해 주세요.');}return r.json();
}
