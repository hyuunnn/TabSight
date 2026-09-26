export type Note={id:string;midi:number;start:number;end:number;string:number;fret:number;velocity:number;confidence:number;technique:string;reviewed:boolean;evidence:string[]};
export type Bar={start:number;end:number;numerator:number;denominator:number;tempo:number};
export type Board={nut:number[];fret12:number[];width:number;confidence?:number;source?:string};
export type Frame={time:number;hands:number[][][];board?:Board;positions:{string:number;fret:number}[]};
export type Project={id:string;title:string;url:string;video_id:string;source:string;status:string;stage:string;progress:number;error:string;revision:number;duration:number;analysis_seconds:number;tuning:number[];capo:number;capo_segments:{start:number;capo:number}[];tempo:number;notes:Note[];bars:Bar[];warnings:string[];metadata:Record<string,any>;vision:{frames?:Frame[];calibration?:Board;[key:string]:any};metrics:Record<string,any>};
export type Summary=Pick<Project,'id'|'title'|'status'|'stage'|'progress'|'source'|'duration'|'video_id'>&{note_count:number;review_count:number};
export const techniques:Record<string,string>={normal:'일반음',hammer:'해머온',pull:'풀오프',slide:'슬라이드',harmonic:'하모닉스',mute:'팜 뮤트',slap:'슬랩',percussion:'바디 퍼커션',bend:'벤딩',vibrato:'비브라토'};
export const pitch=(n:number)=>['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'][((n%12)+12)%12]+(Math.floor(n/12)-1);
export const clock=(s:number)=>`${Math.floor(Math.max(0,s)/60)}:${Math.floor(Math.max(0,s)%60).toString().padStart(2,'0')}`;
export function capoAt(p:Project,t:number){return [...p.capo_segments].sort((a,b)=>a.start-b.start).filter(x=>x.start<=t).at(-1)?.capo??p.capo;}
export const harmonicIntervals:Record<number,number>={12:12,7:19,5:24,4:28,9:28,3:31};
export function notePitch(n:Note,p:Project){return p.tuning[6-n.string]+capoAt(p,n.start)+(n.technique==='harmonic'?(harmonicIntervals[n.fret]??n.fret):n.fret);}
export async function request<T=Project>(path:string,options:RequestInit={}):Promise<T>{
 const r=await fetch(path,{...options,headers:options.body instanceof FormData?{}:{'Content-Type':'application/json',...options.headers}});
 if(!r.ok){const b=await r.json().catch(()=>({detail:`요청 실패 (${r.status})`}));throw new Error(typeof b.detail==='string'?b.detail:'입력값을 확인해 주세요.');}return r.json();
}
