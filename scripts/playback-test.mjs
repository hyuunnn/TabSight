import assert from 'node:assert/strict';
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {chromium} from 'playwright';

const base=process.env.TABSIGHT_URL||'http://127.0.0.1:8787';
const fixtureArgs=['-m','scripts.browser_fixture','create'];
if(process.env.TABSIGHT_E2E_PROJECT)fixtureArgs.push(process.env.TABSIGHT_E2E_PROJECT);
let id;try{id=execFileSync('.venv/bin/python',fixtureArgs,{encoding:'utf8'}).trim();}catch(e){if(e.status==null)console.error(e.message);process.exit(e.status||1);}
const project=await(await fetch(`${base}/api/projects/${id}`)).json();
const results=[],errors=[];
let browser,page;
fs.mkdirSync('test-results',{recursive:true});
const report=name=>{results.push(name);console.log('PASS',name);};
try{
 browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.setDefaultTimeout(12000);
 page.on('pageerror',error=>errors.push(error.message));
 await page.addInitScript(id=>localStorage.setItem('tabsight-project',id),id);
 await page.goto(base);
 await page.locator('.alpha-host svg').first().waitFor();
 await page.waitForTimeout(1500);
 const viewport=page.locator('.score-scroll');
 const seek=async time=>{
  await page.getByLabel('재생 위치',{exact:true}).evaluate((element,time)=>{
   Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(element,String(time));
   element.dispatchEvent(new Event('input',{bubbles:true}));
   element.dispatchEvent(new Event('change',{bubbles:true}));
  },time);
  await page.waitForTimeout(450);
 };
 const visibleCursor=()=>page.waitForFunction(()=>{
  const viewport=document.querySelector('.score-scroll').getBoundingClientRect();
  const cursor=document.querySelector('.at-cursor-beat').getBoundingClientRect();
  return cursor.width>=2&&cursor.height>20&&cursor.top>=viewport.top&&cursor.bottom<=viewport.bottom+1&&cursor.left>=viewport.left&&cursor.right<=viewport.right+1;
 });
 const waitBar=bar=>page.waitForFunction(bar=>document.querySelector('.alpha-host')?.dataset.playbackBar===String(bar),bar);
 const pause=()=>page.getByRole('button',{name:'일시정지',exact:true}).click();
 const play=()=>page.getByRole('button',{name:'재생',exact:true}).click();
 const scrollTop=()=>viewport.evaluate(element=>element.scrollTop);

 await visibleCursor();
 const playhead=await page.locator('.at-cursor-beat').boundingBox();
 assert(playhead.width>=2,'The scaled cursor must remain visible in actual screen pixels');
 report('재생선 실제 너비와 가시성');

 const pageTop=await page.evaluate(()=>window.scrollY);
 await seek(project.bars[12].start+.2);
 await waitBar(13);await visibleCursor();
 await page.waitForFunction(()=>document.querySelectorAll('.tabsight-active-beat').length>0);
 assert((await scrollTop())>300);
 assert.equal(await page.evaluate(()=>window.scrollY),pageTop,'Only the score viewport should scroll');
 assert.match(await page.getByLabel('악보 현재 위치',{exact:true}).textContent(),/13마디/);
 report('멀리 탐색할 때 마디·음표 강조와 악보 내부 스크롤');

 await seek(project.bars[12].start-.35);await waitBar(12);await visibleCursor();
 const beforeOriginal=await scrollTop();
 await play();
 await page.waitForFunction(time=>document.querySelector('video').currentTime>time,project.bars[12].start+.45);
 await visibleCursor();await pause();
 assert((await scrollTop())>beforeOriginal+20,`Original: before=${beforeOriginal}, after=${await scrollTop()}, position=${await page.getByLabel('악보 현재 위치',{exact:true}).textContent()}`);
 report('원음 재생 중 다음 악보 줄로 자동 넘김');
 await page.screenshot({path:'test-results/playback-follow.png',fullPage:true});

 await page.getByRole('button',{name:'자동 넘김 켜짐',exact:true}).click();
 await viewport.evaluate(element=>element.scrollTo({top:0,behavior:'instant'}));
 await seek(project.bars[20].start+.2);await waitBar(21);
 await page.waitForTimeout(300);
 assert.equal(await scrollTop(),0);
 await page.getByLabel('악보 현재 위치로 이동',{exact:true}).click();
 await visibleCursor();assert((await scrollTop())>300);
 await page.getByRole('button',{name:'자동 넘김 꺼짐',exact:true}).click();
 report('자동 넘김 해제와 현재 위치 복귀');

 await page.getByRole('button',{name:'악보음',exact:true}).click();
 await seek(project.bars[16].start-.35);await waitBar(16);await visibleCursor();
 const beforeSynth=await scrollTop();
 await play();
 await page.waitForFunction(time=>Number(document.querySelector('input[aria-label="재생 위치"]').value)>time,project.bars[16].start+.45);
 await visibleCursor();await pause();
 assert((await scrollTop())>beforeSynth+20);
 assert(await page.locator('video').evaluate(element=>element.paused));
 report('악보음 재생 중 다음 악보 줄로 자동 넘김');

 await page.getByRole('button',{name:'원음',exact:true}).click();
 const a=project.bars[6].start+.2,b=project.bars[14].start+.2;
 await seek(a);await page.locator('.loop-mark').first().click();
 await seek(b);await page.locator('.loop-mark').nth(1).click();
 await seek(b-.15);await waitBar(15);await visibleCursor();
 const beforeLoop=await scrollTop();await play();
 await page.waitForFunction(a=>{const v=document.querySelector('video');return !v.paused&&v.currentTime>=a&&v.currentTime<a+1.5;},a);
 await visibleCursor();await pause();
 assert((await scrollTop())<beforeLoop-100);
 await page.getByRole('button',{name:'A–B 반복',exact:true}).click();
 report('A–B 반복 시 앞쪽 악보로 자동 복귀');

 await seek(project.bars[12].start+.2);await waitBar(13);await visibleCursor();
 await page.getByLabel('악보 확대',{exact:true}).click();
 await page.waitForTimeout(700);await visibleCursor();
 await page.getByRole('button',{name:'TAB',exact:true}).click();
 await page.waitForTimeout(700);await visibleCursor();
 assert.match(await page.getByLabel('악보 현재 위치',{exact:true}).textContent(),/13마디/);
 report('확대·오선보 전환 후 재생 위치 유지');

 await page.getByRole('button',{name:'TAB + 오선보',exact:true}).click();
 await page.setViewportSize({width:390,height:844});
 await page.waitForTimeout(700);
 await seek(project.bars[20].start+.2);await waitBar(21);await visibleCursor();
 const mobileBar=await page.locator('.at-cursor-bar').boundingBox();
 const mobileViewport=await viewport.boundingBox();
 assert(mobileBar.width>mobileViewport.width*.7,'A narrow score should place one measure on each row');
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
 await page.locator('.notation-panel').scrollIntoViewIfNeeded();
 await page.screenshot({path:'test-results/playback-mobile.png',fullPage:true});
 report('모바일에서 재생선·자동 넘김·가로 넘침 없음');

 assert.deepEqual(errors,[]);
 fs.writeFileSync('test-results/playback-report.json',JSON.stringify({passed:results,errors},null,2));
}catch(error){
 if(page)await page.screenshot({path:'test-results/playback-failure.png',fullPage:true});
 throw error;
}finally{
 await browser?.close();
 execFileSync('.venv/bin/python',['-m','scripts.browser_fixture','delete',id]);
}
