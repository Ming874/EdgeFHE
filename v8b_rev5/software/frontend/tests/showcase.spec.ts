import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
const raw=readFileSync(resolve('public/data/showcase.json'));
const data=JSON.parse(raw.toString());
const liveReplies=JSON.parse(readFileSync(resolve('tests/fixtures/live_replies_local_qa.json')).toString());

test.beforeEach(async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    return url.hostname==='127.0.0.1'?route.continue():route.abort();
  });
});
async function ready(page: import('@playwright/test').Page) {
  await page.goto('/');await expect(page.getByRole('heading',{name:/資料留在邊緣。.*讓價值安全流動。/})).toBeVisible();
}
test('offline overview renders current checked values with clear scope',async({page},info)=>{
  await ready(page);
  await expect(page.getByText('OFFLINE',{exact:true})).toBeVisible();
  await expect(page.getByText('目前未連接開發板')).toBeAttached();
  await expect(page.locator('.metric-card').nth(0)).toContainText('7.81');
  await expect(page.locator('.metric-card').nth(1)).toContainText('3.746');
  await expect(page.locator('.scope-strip')).toContainText('不含網路 / TLS');
  await expect(page.locator('.metric-card').nth(2)).toContainText('237,145');
  await expect(page.locator('.recharts-surface').first()).toBeVisible();
  await expect(page.locator('.lan-band')).toContainText('1.81×');
  await expect(page.locator('.lan-band')).toContainText('360 窗口全數通過');
  await page.screenshot({path:info.outputPath('overview.png'),fullPage:true});
});
test('three model catalog uses current native models rather than legacy scenarios',async({page})=>{
  await ready(page);await page.getByLabel('展示模型').selectOption('ai4i-failure-logreg-v1');
  await expect(page.locator('.metric-card').first()).toContainText('4.77');
  await page.getByRole('button',{name:'應用體驗',exact:true}).click();
  await expect(page.locator('.feature-item')).toHaveCount(4);
  await page.getByLabel('展示模型').selectOption('diabetes-symptom-logreg-v1');
  await expect(page.locator('.feature-item')).toHaveCount(16);
  await page.getByLabel('展示模型').selectOption('diabetes-progression-ridge-v1');
  await expect(page.locator('.feature-item')).toHaveCount(10);
  await expect(page.getByText('公開合成向量，非個人資料。',{exact:false})).toBeVisible();
});
test('all public cases finish as reference results with no hardware request',async({page},info)=>{
  // Only the read-only GET status probe may touch /api/; a public case must never submit an inference.
  const calls:string[]=[];page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/')&&!(r.method()==='GET'&&u.pathname==='/api/live/v1/status'))calls.push(r.method()+' '+u.pathname);});
  await ready(page);await page.getByRole('button',{name:'應用體驗',exact:true}).click();
  for(const model of data.models){
    await page.getByLabel('展示模型').selectOption(model.id);
    for(const sample of data.cases.filter((c:{modelId:string})=>c.modelId===model.id)){
      await page.getByLabel('公開案例',{exact:true}).selectOption(sample.id);
      for(let i=0;i<6;i++)await page.getByRole('button',{name:'下一步',exact:true}).click();
      await expect(page.locator('.case-result')).toContainText('明文 affine 參考');
      await expect(page.locator('.case-result')).toContainText(sample.score.toLocaleString('en-US',{minimumFractionDigits:6,maximumFractionDigits:6}));
      await expect(page.locator('.case-result')).toContainText('沒有新 latency');
    }
  }
  expect(calls).toEqual([]);
  await page.screenshot({path:info.outputPath('application.png'),fullPage:true});
});
test('switching public cases never retains a completed old result',async({page})=>{
  await ready(page);await page.getByRole('button',{name:'應用體驗',exact:true}).click();
  for(let i=0;i<6;i++)await page.getByRole('button',{name:'下一步',exact:true}).click();
  await expect(page.locator('.case-result')).toBeVisible();
  await page.getByLabel('公開案例',{exact:true}).selectOption('public-1-1');
  await expect(page.locator('.case-result')).toHaveCount(0);
  await expect(page.getByText('播放流程後查看參考結果')).toBeVisible();
});
test('engineering shows real intervals and archived precision exceptions',async({page},info)=>{
  await ready(page);await page.getByRole('button',{name:'工程分析',exact:true}).click();
  await expect(page.getByText('3.737–3.755×')).toBeVisible();
  await expect(page.locator('.recharts-surface')).toHaveCount(2);
  await page.screenshot({path:info.outputPath('performance.png'),fullPage:true});
  await page.getByRole('tab',{name:'精度與能源'}).click();
  await expect(page.getByText('AI4I 1 筆、症狀模型 25 筆',{exact:false})).toBeVisible();
  await expect(page.getByText('不是插座／整板功耗',{exact:false})).toBeVisible();
  await expect(page.getByText('沒有 CI',{exact:false})).toBeVisible();
  await expect(page.locator('.recharts-surface')).toBeVisible();
  await page.screenshot({path:info.outputPath('precision-energy.png'),fullPage:true});
});
test('two-machine view shows reviewed LAN evidence with explicit scope',async({page},info)=>{
  await ready(page);await page.getByRole('button',{name:'兩機實測',exact:true}).click();
  await expect(page.getByRole('heading',{name:'邊緣加密、雲端算密文，真的跑在兩台機器上。'})).toBeVisible();
  const wired=data.lan.runs[0].models[1];
  await expect(page.locator('.metric-card').first()).toContainText(wired.latency.point.toFixed(3));
  await expect(page.locator('.scope-strip')).toContainText('含網路與 TLS');
  await expect(page.locator('.tiers')).toContainText('明文與私鑰只在這裡');
  await expect(page.locator('.tiers')).toContainText('只收密文');
  await expect(page.locator('table').first().locator('tbody tr')).toHaveCount(4);
  await expect(page.locator('.decomp')).toHaveCount(2);
  await expect(page.getByText('360/360 窗口通過',{exact:true})).toBeVisible();
  await expect(page.locator('.soak-chart .recharts-surface')).toBeVisible();
  await expect(page.getByText('浸泡在第 4 個窗口',{exact:false})).toBeVisible();
  expect(await page.locator('body').evaluate(b=>b.scrollWidth<=window.innerWidth+1)).toBeTruthy();
  await page.screenshot({path:info.outputPath('system.png'),fullPage:true});
  await page.getByLabel('展示模型').selectOption('diabetes-symptom-logreg-v1');
  await expect(page.locator('.callout.amber')).toContainText('沒有納入兩機正式比較');
  await page.getByLabel('展示模型').selectOption('ai4i-failure-logreg-v1');
  await expect(page.locator('.metric-card').first()).toContainText(data.lan.runs[0].models[0].latency.point.toFixed(3));
  await expect(page.locator('.callout.amber')).toHaveCount(0);
});
test('architecture retains real routed resources and open acceptance gates',async({page},info)=>{
  await ready(page);await page.getByRole('button',{name:'工程分析',exact:true}).click();await page.getByRole('tab',{name:'架構與關卡'}).click();
  await expect(page.getByText('65.63%',{exact:true})).toBeVisible();
  await expect(page.getByText('原生兩機 E2E',{exact:true})).toBeVisible();
  await expect(page.getByText('待完成',{exact:true})).toHaveCount(2);
  await expect(page.getByText('即時前中後端展示',{exact:true})).toBeVisible();
  await page.screenshot({path:info.outputPath('architecture.png'),fullPage:true});
});
test('sources show full pinned identities without personal filesystem paths',async({page})=>{
  await ready(page);await page.getByRole('button',{name:'來源與範圍'}).click();
  const modal=page.getByRole('dialog');await expect(modal).toBeVisible();
  await expect(modal.getByText(data.sources[0].sha256,{exact:true})).toBeVisible();
  expect(await modal.innerText()).not.toMatch(/[CD]:[\\/]|C:\\Users|\/home\/ubuntu/);
  await page.keyboard.press('Escape');await expect(modal).toHaveCount(0);
});
test('unknown or tampered evidence file is rejected without changing metrics',async({page})=>{
  await ready(page);await page.getByRole('button',{name:'關於這份展示'}).click();
  await page.getByLabel('選擇公開資料包').setInputFiles({name:'modified.json',mimeType:'application/json',buffer:Buffer.from('{"schema":"forged"}')});
  await expect(page.getByRole('alert')).toContainText('來源身分不符');
  await page.getByRole('button',{name:'關閉對話框'}).click();
  await expect(page.locator('.metric-card').first()).toContainText('7.81');
});
test('exact approved evidence file can be checked locally',async({page})=>{
  await ready(page);await page.getByRole('button',{name:'關於這份展示'}).click();
  await page.getByLabel('選擇公開資料包').setInputFiles({name:'showcase.json',mimeType:'application/json',buffer:raw});
  await expect(page.getByRole('alert')).toContainText('完整雜湊與格式相符');
});
test('missing or corrupted data fails closed rather than inventing numbers',async({page})=>{
  await page.route('**/data/showcase.json',route=>route.fulfill({status:200,contentType:'application/json',body:'{}'}));
  await page.goto('/');await expect(page.getByRole('alert')).toContainText('展示資料尚未通過核對');
  await expect(page.locator('.metric-card')).toHaveCount(0);
});
test('presentation guide connects all three views and can finish',async({page})=>{
  await ready(page);await page.getByRole('button',{name:'五分鐘導覽'}).click();
  await expect(page.locator('.guide-count')).toHaveText('01 / 06');
  await page.getByRole('button',{name:'下一段',exact:true}).click();
  await expect(page.getByRole('heading',{name:'從一筆資料，理解整個流程。'})).toBeVisible();
  await page.getByRole('button',{name:'下一段',exact:true}).click();
  await expect(page.getByRole('heading',{name:'邊緣加密、雲端算密文，真的跑在兩台機器上。'})).toBeVisible();
  await page.getByRole('button',{name:'下一段',exact:true}).click();await page.getByRole('button',{name:'下一段',exact:true}).click();
  await expect(page.getByRole('tab',{name:'精度與能源'})).toHaveAttribute('aria-selected','true');
  await page.getByRole('button',{name:'下一段',exact:true}).click();
  await expect(page.getByRole('tab',{name:'架構與關卡'})).toHaveAttribute('aria-selected','true');
  await page.getByRole('button',{name:'結束導覽'}).click();await expect(page.locator('.guide-bar')).toHaveCount(0);
});
test('reduced motion, keyboard access and narrow layouts remain usable',async({page})=>{
  await ready(page);await expect(page.getByRole('button',{name:'動畫關'})).toHaveAttribute('aria-pressed','false');
  await page.keyboard.press('Tab');await expect(page.getByRole('link',{name:'跳到主要內容'})).toBeFocused();
  expect(await page.locator('body').evaluate(b=>b.scrollWidth<=window.innerWidth+1)).toBeTruthy();
  await page.getByRole('button',{name:'工程分析',exact:true}).click();await page.getByRole('tab',{name:'原件與參考'}).click();
  expect(await page.locator('body').evaluate(b=>b.scrollWidth<=window.innerWidth+1)).toBeTruthy();
  await page.setViewportSize({width:320,height:800});
  expect(await page.locator('body').evaluate(b=>b.scrollWidth<=window.innerWidth+1)).toBeTruthy();
});
test('built page contains no external runtime resource requests or page errors',async({page})=>{
  const errors:string[]=[];const external:string[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(new URL(r.url()).hostname!=='127.0.0.1')external.push(r.url());});
  await ready(page);await page.getByRole('button',{name:'兩機實測',exact:true}).click();await expect(page.locator('.soak-chart .recharts-surface')).toBeVisible();
  await page.getByRole('button',{name:'工程分析',exact:true}).click();
  await page.getByRole('tab',{name:'精度與能源'}).click();await page.getByRole('tab',{name:'架構與關卡'}).click();
  expect(errors).toEqual([]);expect(external).toEqual([]);
});
test('static service rejects execution endpoints',async({request})=>{
  const health=await request.get('/health');expect(await health.json()).toEqual({status:'OFFLINE_SHOWCASE_READY',board_connected:false});
  expect((await request.post('/api/showcase/v1/inference',{data:{modelId:'x'}})).status()).toBe(405);
  expect((await request.get('/api/v1/system')).status()).toBe(503);
});
test('print summary keeps scope, confidence intervals and public limitations',async({page},info)=>{
  await ready(page);await page.emulateMedia({media:'print'});
  await expect(page.locator('.print-report')).toBeVisible();await expect(page.locator('.sidebar')).not.toBeVisible();
  await expect(page.locator('.print-report')).toContainText('同板、單boot');
  await expect(page.locator('.print-report')).toContainText('兩機 LAN 實測');
  if(info.project.name==='desktop-1366')await page.pdf({path:info.outputPath('EdgeFHE-public-summary.pdf'),format:'A4',printBackground:true});
});

test('live view stays honest when no console or board is present',async({page},info)=>{
  await ready(page);await page.getByRole('button',{name:'即時體驗',exact:true}).click();
  await expect(page.getByRole('heading',{name:'輸入一筆資料，看它在 KV260 上加密、在 PC 上算密文。'})).toBeVisible();
  await expect(page.getByText('目前是離線展示，沒有連到開發板。')).toBeVisible();
  await expect(page.getByRole('button',{name:'加密並送出'})).toBeDisabled();
  await expect(page.locator('.live-result')).toHaveCount(0);
  expect(await page.locator('body').evaluate(b=>b.scrollWidth<=window.innerWidth+1)).toBeTruthy();
  await page.screenshot({path:info.outputPath('live-offline.png'),fullPage:true});
});
const gateway={type:'hello',protocol:'edgefhe.live.v1',boot_tag:'e26925f5',session:'live_00',fastpath:'3',irqs:{'121':2879161,'136':2879161},models:data.models.map((m:{id:string})=>m.id),requests:0,session_dma:0,fixture:false};
async function mockConsole(page: import('@playwright/test').Page, reply: object, status=200){
  const seen:unknown[]=[];
  await page.route('**/api/live/v1/status',r=>r.fulfill({json:{connected:true,gateway,error:null,board:'192.0.2.10',port:18450,server_log:true,console:'edgefhe.live.v1'}}));
  await page.route('**/api/live/v1/infer',async r=>{expect(r.request().headers()['x-edgefhe-live']).toBe('1');seen.push(r.request().postDataJSON());await r.fulfill({status,json:reply});});
  return seen;
}
test('live request shows the decrypted result, measured stages and matching server digest',async({page},info)=>{
  // Board-shaped variant of the local-QA reply (fixture=false, DMA counted) only to exercise the hardware rendering path.
  const reply=structuredClone(liveReplies['diabetes-progression-ridge-v1']);
  Object.assign(reply.results.pl,{fixture:false,real_PL:true,dma:4,PL_hz:199999998});reply.results.cpu.fixture=false;
  reply.irq_after={'121':2879165,'136':2879165};reply.irq_before={'121':2879161,'136':2879161};reply.session_dma=4;
  const seen=await mockConsole(page,reply);
  await page.goto('/#live');await expect(page.getByText('boot e26925f5 · live_00 · fastpath 3')).toBeVisible();
  await expect(page.getByText('已連接開發板（即時）')).toBeAttached();
  await page.getByLabel('模型',{exact:true}).selectOption('diabetes-progression-ridge-v1');
  await page.getByLabel('BMI').fill('31.25');
  await page.getByRole('button',{name:'加密並送出'}).click();
  await expect(page.locator('.live-score strong').first()).toContainText(reply.results.pl.score.toFixed(6));
  await expect(page.getByText('LIVE · 實機')).toBeVisible();
  await expect(page.locator('.live-backend')).toHaveCount(2);
  await expect(page.locator('.live-evidence').first()).toContainText('FPGA DMA +4');
  await expect(page.getByText('PC 伺服器紀錄找到同一個雜湊',{exact:false}).first()).toBeVisible();
  await expect(page.getByText('回傳雜湊一致',{exact:false}).first()).toBeVisible();
  await expect(page.locator('.live-compare')).toContainText('不是統計結論');
  await expect(page.locator('.lan-table tbody tr')).toHaveCount(1);
  const body=seen[0] as {model:string;features:number[];backends:string[]};
  expect(body.model).toBe('diabetes-progression-ridge-v1');expect(body.features[2]).toBe(31.25);expect(body.backends).toEqual(['cpu','pl']);
  expect((seen[0] as {repeat:number}).repeat).toBe(3);
  expect(await page.locator('body').evaluate(b=>b.scrollWidth<=window.innerWidth+1)).toBeTruthy();
  await page.screenshot({path:info.outputPath('live-result.png'),fullPage:true});
});
test('local-QA replies are labelled as software simulation, never as FPGA',async({page})=>{
  await mockConsole(page,liveReplies['ai4i-failure-logreg-v1']);
  await page.goto('/#live');await page.getByLabel('模型',{exact:true}).selectOption('ai4i-failure-logreg-v1');
  await page.getByRole('button',{name:'加密並送出'}).click();
  await expect(page.getByText('本機模擬（非 FPGA）')).toBeVisible();
  await expect(page.getByText('以軟體規格模擬 PL，沒有實際 DMA')).toBeVisible();
  await expect(page.getByText('LIVE · 實機')).toHaveCount(0);
});
test('board refusal is shown with the field name and no result is drawn',async({page})=>{
  await mockConsole(page,{type:'error',code:'INPUT_OUT_OF_RANGE',detail:'rotational_speed_rpm'},422);
  await page.goto('/#live');await page.getByLabel('模型',{exact:true}).selectOption('ai4i-failure-logreg-v1');
  await page.getByLabel('轉速').fill('99999');await page.getByRole('button',{name:'加密並送出'}).click();
  await expect(page.getByRole('alert')).toContainText('「轉速」超出模型可接受範圍');
  await expect(page.locator('.live-result')).toHaveCount(0);
});
test('malformed console replies are rejected instead of displayed',async({page})=>{
  const reply=structuredClone(liveReplies['ai4i-failure-logreg-v1']);reply.results.pl.request_sha256='forged';
  await mockConsole(page,reply);await page.goto('/#live');
  await page.getByLabel('模型',{exact:true}).selectOption('ai4i-failure-logreg-v1');await page.getByRole('button',{name:'加密並送出'}).click();
  await expect(page.getByRole('alert')).toContainText('即時結果數值');await expect(page.locator('.live-result')).toHaveCount(0);
});
test('a non-numeric field is caught in the browser before anything is sent',async({page})=>{
  const seen=await mockConsole(page,liveReplies['ai4i-failure-logreg-v1']);await page.goto('/#live');
  await page.getByLabel('模型',{exact:true}).selectOption('ai4i-failure-logreg-v1');await page.getByLabel('扭矩').fill('abc');
  await page.getByRole('button',{name:'加密並送出'}).click();await expect(page.getByRole('alert')).toContainText('「扭矩」不是有效數字');expect(seen).toEqual([]);
});

test('repeated runs show the median and the observed range, not a single noisy sample',async({page})=>{
  const reply=structuredClone(liveReplies['diabetes-progression-ridge-v1']);
  for(const b of ['cpu','pl']){const r=reply.results[b];const base=r.request_ms;
    r.repeat=3;r.samples=[0,1,2].map(k=>({run:k,request_ms:base+(k-1)*7,prepare_ms:r.prepare_ms,network_ms:r.network_ms,decrypt_ms:r.decrypt_ms,request_sha256:r.request_sha256,response_sha256:r.response_sha256,dma:r.dma}));}
  const seen=await mockConsole(page,reply);await page.goto('/#live');
  await page.getByLabel('模型',{exact:true}).selectOption('diabetes-progression-ridge-v1');
  await expect(page.getByLabel('重複量測')).toHaveValue('3');
  await page.getByRole('button',{name:'加密並送出'}).click();
  await expect(page.locator('.live-backend h3').first()).toContainText('3 次中位數');
  await expect(page.locator('.live-backend h3').first()).toContainText('範圍');
  await expect(page.locator('.live-compare')).toContainText('交替先後順序後取中位數');
  expect((seen[0] as {repeat:number}).repeat).toBe(3);
});
