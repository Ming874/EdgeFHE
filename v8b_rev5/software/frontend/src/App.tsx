import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Activity, ArrowRight, ArrowUpRight, BookOpen, Boxes, ChevronLeft, ChevronRight,
  CircleHelp, Cloud, Code2, Cpu, Download, ExternalLink, FileCheck2, Fingerprint, FlaskConical,
  Fullscreen, Gauge, HeartPulse, LayoutDashboard, LockKeyhole, Monitor, Network, Pause,
  Play, Printer, Radio, RotateCcw, ShieldCheck, Sparkles, Thermometer, Upload, Workflow } from 'lucide-react';

import { ci, decodePinnedBundle, downloadJSON, fetchLiveStatus, lanModelFor, loadBundle, MAX_BUNDLE_BYTES, number } from './data';
import { Badge, BoardIllustration, CheckItem, Empty, Metric, Modal, Panel } from './ui';
import type { Bundle, Model, ModelEvidence, PublicCase, View } from './types';
import { SystemView } from './system';
import { LiveView } from './live';
import { featureNames } from './labels';

const ComparisonChart=lazy(()=>import('./charts'));
const SensorChart=lazy(()=>import('./charts').then(m=>({default:m.SensorChart})));
const steps = [
  ['驗證與正規化','ARM C++','公開欄位依模型卡轉成標準化特徵。'],
  ['CKKS 編碼','ARM C++','以 canonical embedding 與 scale 表示多項式。'],
  ['公鑰加密','FPGA PL','ChaCha20／CDT63 與三分量 NTT 資料路徑。'],
  ['密文 affine','服務端','只對密文做公開權重乘加；離線播放不建立連線。'],
  ['解密與解碼','OWNER','私鑰與結果留在資料擁有者的信任範圍。'],
  ['結果解讀','RESEARCH','顯示公開案例的明文 affine 參考分數。'],
];
const stateLabels = { accepted:'此範圍已接受',partial:'部分完成',pending:'待完成' };
const fmt = (n: number) => number(n,2);
const metricFor = (b: Bundle,id: string) => b.campaign.models.find(m => m.model === id)!;

function SourceButton({ onClick }: { onClick: () => void }) { return <button className="text-button" onClick={onClick}><FileCheck2 size={15}/>來源與範圍<ArrowUpRight size={13}/></button>; }
function Scope({ compact = false }: { compact?: boolean }) {
  return <div className={`scope-strip ${compact?'compact':''}`}><Badge tone="teal">歷史實測</Badge><span>app_v5 · 10 sessions · 同板 ARM＋PL</span><strong>不含網路 / TLS</strong></div>;
}
function MetricGrid({ m }: { m: ModelEvidence }) {
  return <div className="metrics-grid"><Metric label="單筆 PL 延遲 · W1 P50" value={fmt(m.W1_candidate_P50_ms)} unit="ms" foot="歷史完整同板數學流程" accent/>
    <Metric label="相較同板 CPU3" value={number(m.W1_latency_speedup.point,3)} unit="×" foot={`95% CI ${ci(m.W1_latency_speedup)}`}/>
    <Metric label="滿槽持續吞吐 · W2" value={number(m.W2_candidate_records_s)} unit="筆/s" foot="60 秒窗口，包含 drain"/>
    <Metric label="持續吞吐加速比" value={number(m.W2_sustained_speedup.point,3)} unit="×" foot={`95% CI ${ci(m.W2_sustained_speedup)}`}/></div>;
}

function LanBand({ bundle, model, go }: { bundle: Bundle; model: Model; go: (v: View) => void }) {
  const id=lanModelFor(model.id);const r=bundle.lan.runs[0].models.find(x=>x.model===id)!;const short=bundle.models.find(x=>x.id===id)!.short;
  return <div className="lan-band"><span className="circle-icon"><Network size={19}/></span><div><p className="eyebrow">TWO MACHINES · WIRED LAN · mTLS</p>
    <strong>{short}：含網路的單筆延遲 {number(r.latency.point,2)}×、持續吞吐 {number(r.records.point,2)}×，六小時 360 窗口全數通過。</strong>
    <p>KV260 加密與解密、PC 只算密文；每個數字都經獨立審查，包含網路與 TLS。</p></div>
    <button className="button secondary" onClick={()=>go('system')}>看兩機實測<ArrowRight size={15}/></button></div>;
}

function Tour({ bundle, model, go, sources }: { bundle: Bundle; model: Model; go: (v: View) => void; sources: () => void }) {
  const m=metricFor(bundle,model.id);
  return <div className="view-enter">
    <section className="hero"><div className="hero-copy"><div className="hero-kicker"><span/>PRIVACY-PRESERVING EDGE COMPUTING</div>
      <h1>資料留在邊緣。<br/><em>讓價值安全流動。</em></h1><p>從一筆公開研究資料出發，看見 KV260 如何以 ARM 與 FPGA 協同運算，連接隱私、效能與可驗證的證據。</p>
      <div className="hero-actions"><button className="button primary" onClick={()=>go('application')}>體驗公開案例<ArrowRight size={17}/></button><button className="button ghost" onClick={()=>go('engineering')}>查看工程證據<ArrowUpRight size={16}/></button></div>
      <div className="hero-foot"><ShieldCheck size={15}/><span>研究原型</span><span>公開案例先行</span><span>離線可展示</span></div></div><BoardIllustration/></section>
    <div className="section-title"><div><p className="eyebrow">MEASURED, NOT ASSUMED</p><h2>{model.short}，有原件支持的成果</h2></div><SourceButton onClick={sources}/></div>
    <Scope/><MetricGrid m={m}/>
    <LanBand bundle={bundle} model={model} go={go}/>
    <div className="two-column tour-bottom"><Panel eyebrow="ONE SYSTEM · THREE PERSPECTIVES" title="把隱私分析的故事說完整">
      <div className="perspective-list"><button onClick={()=>go('application')}><span className="circle-icon"><HeartPulse size={19}/></span><div><strong>應用體驗</strong><p>選擇公開案例，理解輸入、流程與研究分數。</p></div><ArrowUpRight size={16}/></button>
        <button onClick={()=>go('engineering')}><span className="circle-icon"><Gauge size={19}/></span><div><strong>工程分析</strong><p>比較 CPU／PL、精度、管線與資源使用。</p></div><ArrowUpRight size={16}/></button>
        <button onClick={sources}><span className="circle-icon"><Fingerprint size={19}/></span><div><strong>可追溯證據</strong><p>每個數字都有版本、雜湊與量測邊界。</p></div><ArrowUpRight size={16}/></button></div>
    </Panel><Panel eyebrow="W1 · SAME BOARD" title="單筆時間，直接比較" action={<span className="tiny muted">越低越好 · ms</span>}><Suspense fallback={<div className="chart chart-loading">載入歷史圖表…</div>}><ComparisonChart m={m}/></Suspense><div className="chart-legend"><span><i className="legend-dot cpu"/>CPU3 {fmt(m.W1_cpu_P50_ms)} ms</span><span><i className="legend-dot"/>PL {fmt(m.W1_candidate_P50_ms)} ms</span></div></Panel></div>
    <Panel eyebrow="TRUST BOUNDARY" title="哪些資料，在哪裡可見？" className="boundary-panel"><div className="boundary-grid"><div><LockKeyhole size={23}/><h3>資料擁有者範圍</h3><p>原始特徵、私鑰、解密結果與研究判讀。</p><Badge tone="teal">需可信的邊緣環境</Badge></div><ArrowRight className="boundary-arrow"/><div><Cloud size={25}/><h3>密文服務端</h3><p>密文、公開模型身分與加密的 affine 結果。</p><Badge>不取得私鑰</Badge></div><div className="boundary-note"><CircleHelp size={17}/><p>兩機（KV260 ↔ PC，mTLS）已正式量測；本頁本身沒有連線。HE 與 TLS 不會隱藏全部流量 metadata，也不代表零洩漏。</p></div></div></Panel>
  </div>;
}

function WorkflowPlayer({ model, sample, motion }: { model: Model; sample: PublicCase; motion: boolean }) {
  const [step,setStep]=useState(-1);const [playing,setPlaying]=useState(false);
  useEffect(()=>{
    if(!playing)return;
    const timer=setTimeout(()=>{if(step>=5)setPlaying(false);else setStep(step+1);},motion?800:200);
    return()=>clearTimeout(timer);
  },[playing,step,motion]);
  const start=()=>{setStep(0);setPlaying(true);};
  const complete=step>=5;
  return <div className="workflow-area">
    <div className="reference-banner"><Badge tone="amber">參考示範</Badge><span>公開 C++ affine 參考；不執行當次加密、網路或 FPGA 工作。</span></div>
    <div className="workflow-controls"><button className="button primary" onClick={start} disabled={playing}><Play size={16}/>播放公開案例流程</button>
      <button className="button secondary" onClick={()=>setPlaying(!playing)} disabled={step<0||complete}>{playing?<Pause size={16}/>:<Play size={16}/>} {playing?'暫停':'繼續'}</button>
      <button className="button icon-only secondary" onClick={()=>{setStep(-1);setPlaying(false);}} aria-label="重設案例播放"><RotateCcw size={16}/></button>
      <button className="text-button" disabled={playing||complete} onClick={()=>setStep(Math.min(5,step+1))}>下一步<ChevronRight size={15}/></button></div>
    <div className="step-list" aria-label="公開案例流程">{steps.map(([title,where,description],i)=><div className={`flow-step ${step===i?'active':''} ${step>i?'complete':''}`} key={title}>
      <span className="step-number">{String(i+1).padStart(2,'0')}</span><div><div className="step-title"><h3>{title}</h3><span>{where}</span></div><p>{description}</p>
      {step===i&&i===2&&<div className="flow-detail"><Code2 size={15}/>c₀ = bu + e₀ + m · c₁ = au + e₁ <small>加密方程示意，沒有在此產生密文。</small></div>}
      {step===i&&i===3&&<div className="flow-detail"><LockKeyhole size={15}/>Enc(Σ wᵢzᵢ + b)<small>離線模式不提交 request。</small></div>}</div><span className="step-indicator"/></div>)}</div>
    {complete?<div className="case-result" role="status"><span className="eyebrow">PUBLIC REFERENCE RESULT</span><div><strong>{number(sample.score,6)}</strong><Badge tone="amber">明文 affine 參考</Badge></div>
      <h3>{model.task==='regression'?'標準化進展研究分數':sample.boundary?'研究門檻附近的數值探針':sample.score>model.threshold?'高於模型研究門檻':'低於模型研究門檻'}</h3>
      <p>{model.task==='regression'?'這是迴歸分數，不是罹病機率或診斷結果。':'這是公開模型的參考 logit，不是臨床風險或已校準機率。'}</p>
      <small>動畫進度不是晶片時間；此案例沒有新 latency、吞吐或 CKKS 誤差量測。</small></div>:<div className="result-placeholder"><Fingerprint size={22}/><span>播放流程後查看參考結果</span></div>}
  </div>;
}

function Application({ bundle, model, motion }: { bundle: Bundle; model: Model; motion: boolean }) {
  const cases=bundle.cases.filter(c=>c.modelId===model.id);const [caseId,setCaseId]=useState(cases[0].id);
  const sample=cases.find(c=>c.id===caseId)!;
  const [details,setDetails]=useState<'model'|'math'>('model');
  return <div className="view-enter"><div className="page-intro"><p className="eyebrow">PUBLIC CASE EXPERIENCE</p><h1>從一筆資料，理解整個流程。</h1><p>選取可重算的公開合成案例。這裡展示應用邏輯，不上傳私密資料，也不呼叫硬體。</p></div>
    <div className="application-layout"><div className="case-column"><Panel eyebrow={`${model.featureCount} FEATURES · ${model.task==='regression'?'REGRESSION':'LOGISTIC RESEARCH'}`} title={model.title}>
      <label className="field-label" htmlFor="public-case">公開案例</label><select id="public-case" value={caseId} onChange={e=>setCaseId(e.target.value)}>{cases.map(c=><option key={c.id} value={c.id}>{c.label}</option>)}</select>
      <div className="feature-grid">{model.features.map((f,i)=><div className="feature-item" key={f}><span title={f}>{featureNames[f]||f}</span><strong>{model.id.includes('symptom')&&i>0?sample.raw[i]===1?'1 · 是':'0 · 否':number(sample.raw[i],3)}</strong></div>)}</div>
      <div className="public-data-note"><FlaskConical size={17}/><p>公開合成向量，非個人資料。數值僅對應原始欄位定義；不作醫療合理性或部署有效性保證。</p></div>
    </Panel><Panel title="模型與數學"><div className="segmented small" role="tablist" aria-label="案例詳情"><button role="tab" aria-selected={details==='model'} onClick={()=>setDetails('model')}>模型卡</button><button role="tab" aria-selected={details==='math'} onClick={()=>setDetails('math')}>數學過程</button></div>
      {details==='model'?<div className="model-card"><dl><div><dt>用途</dt><dd>{model.task==='regression'?'進展指標研究迴歸':'公開資料分類研究'}</dd></div><div><dt>資料來源</dt><dd>{model.sourceName}</dd></div><div><dt>特徵／槽位</dt><dd>{model.featureCount} ／ 2,048</dd></div><div><dt>量測角色</dt><dd>{model.primary?'主要模型':'探索性模型'}</dd></div></dl><p>沒有臨床或工業部署核准；資料商用權利仍需個別審查。</p><code className="hash" title={model.sourceSha256}>{model.sourceSha256.slice(0,16)}…</code></div>:
      <div className="math-detail"><div className="formula">zᵢ = (xᵢ − μᵢ) / σᵢ<br/>y = Σ wᵢzᵢ + b</div><p>色條是公開明文參考的特徵貢獻 wᵢzᵢ，不是從私密密文中讀取的解釋。</p><div className="contributions">{model.features.map((f,i)=><div key={f}><span>{featureNames[f]||f}</span><div><i style={{width:`${Math.min(100,Math.abs(sample.contributions[i])*30)}%`,background:sample.contributions[i]>=0?'#23856b':'#bf875a'}}/></div><code>{number(sample.contributions[i],3)}</code></div>)}</div></div>}
    </Panel></div><Panel eyebrow="PLAYBACK · NOT A LIVE REQUEST" title="邊緣到密文運算" action={<Badge tone="amber">離線流程</Badge>}><WorkflowPlayer key={sample.id} sample={sample} model={model} motion={motion}/></Panel></div>
  </div>;
}

function Architecture({ bundle }: { bundle: Bundle }) {
  return <><Panel eyebrow="PS / PL CO-DESIGN" title="同一套硬體，多種 affine 應用"><div className="architecture-flow">
    <div><span className="circle-icon"><Monitor size={22}/></span><strong>可信擁有者</strong><p>公開示例輸入<br/>真實私密輸入另行接入</p></div><ArrowRight/>
    <div><span className="circle-icon"><Cpu size={22}/></span><strong>ARM PS</strong><p>正規化、編碼<br/>金鑰與解密</p></div><ArrowRight/>
    <div className="pl-box"><span className="circle-icon"><Boxes size={22}/></span><strong>FPGA PL</strong><p>3 RNS × 4 lanes<br/>NTT／ENC</p></div><ArrowRight/>
    <div><span className="circle-icon"><Cloud size={22}/></span><strong>密文 evaluator</strong><p>公開權重乘加<br/>不持有私鑰</p></div></div>
    <p className="note">此頁「效能比較」的 evaluator 在同一塊板上（不含網路）；evaluator 移到 PC 並經 mTLS 的兩機結果，見「兩機實測」。</p>
    <div className="spec-grid">{[['環維度','N = 4096'],['複數槽位','2048'],['RNS 模數','3 個質數'],['名目 PL 時脈','200 MHz'],['ENC 取樣','ChaCha20 / CDT63'],['輸出層級','公開 scalar affine']].map(([k,v])=><div key={k}><span>{k}</span><strong>{v}</strong></div>)}</div>
  </Panel><div className="two-column"><Panel eyebrow="ROUTED BASELINE · NOT NEW HLS" title="硬體資源使用"><div className="resource-bars">{bundle.hardware.resources.map(r=><div key={r.name}><header><strong>{r.name}</strong><span>{number(r.percent,2)}%</span></header><div><i style={{width:`${r.percent}%`}}/></div><small>{number(r.used,Number.isInteger(r.used)?0:1)} / {number(r.available)}</small></div>)}</div><p className="note">基準：{bundle.hardware.baseline}。URAM 與時序也是擴充限制；不能只看 LUT 餘量增加 lanes。</p></Panel>
    <Panel eyebrow="RESEARCH ACCEPTANCE" title="完成與缺口，分開呈現"><div className="gates">{bundle.gates.map(g=><div key={g.id}><div><h3>{g.title}</h3><Badge tone={g.state==='accepted'?'teal':g.state==='partial'?'amber':'neutral'}>{stateLabels[g.state]}</Badge></div><p>{g.detail}</p></div>)}</div></Panel></div></>;
}

function Performance({ bundle, model }: { bundle: Bundle; model: Model }) {
  const m=metricFor(bundle,model.id);const stages=m.stage_median_of_session_medians_ms['1'].candidate;
  const stageNames: Record<string,string>={normalize_ns:'正規化',encrypt_ns:'加密',cloud_ns:'同板 affine',decrypt_ns:'解密',sink_ns:'結果檢查',cleanup_ns:'清除'};
  const colors=['#b9cdc1','#178365','#84bca6','#3c797d','#d3a86f','#e0ddcd'];
  return <><Scope/><MetricGrid m={m}/><div className="two-column"><Panel title="單筆延遲 · W1" eyebrow="P50 / P95" action={<span className="tiny muted">越低越好</span>}><Suspense fallback={<div className="chart chart-loading">載入歷史圖表…</div>}><ComparisonChart m={m}/></Suspense><div className="chart-legend"><span><i className="legend-dot cpu"/>CPU3</span><span><i className="legend-dot"/>ARM＋PL</span></div>
      <div className="interval-detail"><span>P50 加速 95% CI</span><strong>{ci(m.W1_latency_speedup)}×</strong><span>P95 加速 95% CI</span><strong>{ci(m.P95_speedup['1'])}×</strong></div></Panel>
    <Panel title="滿槽持續吞吐 · W2" eyebrow="60 SECOND ADMISSION + DRAIN" action={<span className="tiny muted">越高越好</span>}><Suspense fallback={<div className="chart chart-loading">載入歷史圖表…</div>}><ComparisonChart m={m} throughput/></Suspense><div className="chart-legend"><span><i className="legend-dot cpu"/>CPU3</span><span><i className="legend-dot"/>ARM＋PL</span></div><div className="interval-detail"><span>吞吐加速 95% CI</span><strong>{ci(m.W2_sustained_speedup)}×</strong><span>max in-flight</span><strong>CPU {m.W2_max_in_flight.cpu} / PL {m.W2_max_in_flight.pl}</strong></div></Panel></div>
    <Panel eyebrow="W1 · STAGE MEDIAN SUMMARY" title="時間花在哪裡？"><div className="stage-timing">{Object.entries(stageNames).map(([k,label],i)=><div key={k}><span><i style={{background:colors[i]}}/>{label}</span><strong>{number(stages[k as keyof typeof stages],3)}<small>ms</small></strong></div>)}</div>
      <div className="stacked-bar" aria-label="W1階段耗時摘要">{Object.entries(stageNames).map(([k,label],i)=><div key={k} title={`${label} ${stages[k as keyof typeof stages]} ms`} style={{flex:Math.max(.01,stages[k as keyof typeof stages]),background:colors[i]}}/>)}</div>
      <p className="note">各階段中位數的摘要不必相加等於 wall 中位數。這不是 DMA／NTT 核心的獨立量測；W2 管線的重疊時間也不能直接相加。</p></Panel>
    <Panel title="三個模型，用同樣的尺比較" eyebrow="SAME BOARD · SAME APP V5"><div className="table-scroll"><table><thead><tr><th>模型</th><th>CPU3 P50</th><th>PL P50</th><th>W1 加速</th><th>W2 PL 筆/s</th><th>角色</th></tr></thead><tbody>{bundle.models.map(x=>{const v=metricFor(bundle,x.id);return <tr key={x.id}><th>{x.short}</th><td>{fmt(v.W1_cpu_P50_ms)} ms</td><td>{fmt(v.W1_candidate_P50_ms)} ms</td><td>{number(v.W1_latency_speedup.point,3)}×</td><td>{number(v.W2_candidate_records_s)}</td><td>{x.primary?'主要':'探索性'}</td></tr>;})}</tbody></table></div>
      <p className="note">CPU 也使用 app_v5 優化。倍數是 mean session ratios，不能用顯示的四捨五入 P50 再相除取代。單一 boot 的 marginal CI 不代表跨開機或多重比較的聯合保證。</p></Panel></>;
}

function PrecisionEnergy({ bundle }: { bundle: Bundle }) {
  return <><div className="callout amber"><CircleHelp size={21}/><div><strong>精度例外也保留，才是可信的比較。</strong><p>AI4I 1 筆、症狀模型 25 筆超過 5e-4；皆在本輪 logistic 驗收門檻 2e-3 內。這些是實測觀察，不是一般誤差保證或每金鑰解析證書。</p></div></div>
    <Panel eyebrow="ALL SUSTAINED REQUESTS CHECKED" title="最大誤差與超限次數"><div className="table-scroll"><table><thead><tr><th>模型</th><th>CPU 最大誤差</th><th>PL 最大誤差</th><th>超過 5e-4 次數</th><th>當輪容忍值</th></tr></thead><tbody>{bundle.models.map(x=>{const m=metricFor(bundle,x.id);return <tr key={x.id}><th>{x.short}</th><td>{m.W2_window_max_error.cpu.toExponential(3)}</td><td>{m.W2_window_max_error.pl.toExponential(3)}</td><td>{bundle.campaign.precisionExceedances[x.id]}</td><td>{x.task==='regression'?'5e-4':'2e-3'}</td></tr>;})}</tbody></table></div><p className="note">整數環 exact 與 CKKS 近似誤差是不同關卡；分類門檻附近應保留 uncertain。沒有第三方安全或臨床認證。</p></Panel>
    <div className="two-column"><Panel eyebrow="INA260 U14 · 30 S WINDOWS" title="每焦耳完成多少筆？"><div className="energy-cards">{bundle.energy.models.map(x=><div key={x.modelId}><span>{bundle.models.find(m=>m.id===x.modelId)!.short}</span><strong>{number(x.recordsPerJRatio,2)}<small>×</small></strong><p>PL / CPU 總功耗每焦耳筆數</p><dl><div><dt>CPU 軌功率</dt><dd>{number(x.cpu.mean_W,3)} W</dd></div><div><dt>PL 軌功率</dt><dd>{number(x.pl.mean_W,3)} W</dd></div></dl></div>)}</div><p className="note">只涵蓋 K26 SOM 供電軌，不是插座／整板功耗。每格單次 30 秒窗口，沒有 CI，不能當正式多次能效比較。</p></Panel>
      <Panel eyebrow="RECORDED SESSION SUMMARY" title="溫度紀錄"><div className="sensor-head"><Thermometer size={20}/><strong>{number(bundle.sensors.maxTempC,2)} °C</strong><span>歷史最高值</span></div><Suspense fallback={<div className="chart chart-loading">載入歷史圖表…</div>}><SensorChart sessions={bundle.sensors.sessions}/></Suspense><p className="note">不是即時儀表。當輪 CPU frequency snapshot 為 {number(bundle.sensors.cpuFrequencyKHz/1000,3)} MHz；不推定目前板子狀態。</p></Panel></div></>;
}

function Sources({ bundle }: { bundle: Bundle }) {
  return <><Panel eyebrow="EVIDENCE, NOT A LEADERBOARD" title="我們的參考基準點"><div className="reference-summary"><CheckItem>直接加速比：相同板子、相同 app_v5、相同模型與工作量的 CPU3。</CheckItem><CheckItem>OpenFHE／SEAL 尚需最新同板等價比較；前端不補造倍數。</CheckItem><CheckItem>其他論文用來比較分工、資料重用與面積，不做跨平台簡單排名。</CheckItem></div><div className="paper-grid">{bundle.papers.map(p=><article key={p.id}><span>{p.id} · {p.year}</span><h3>{p.title}</h3><small>{p.venue}</small><p>{p.limit}</p><a href={p.url} target="_blank" rel="noreferrer">DOI 原始出處<ExternalLink size={13}/></a></article>)}</div></Panel>
    <Panel eyebrow="PUBLIC MANIFEST" title="資料來源身分"><div className="source-list">{bundle.sources.map(s=><div key={s.id}><div><FileCheck2 size={17}/><strong>{s.label}</strong><span>{s.kind}</span></div><code>{s.sha256}</code></div>)}</div><p className="note">SHA 用於身分與完整性核對，不等於硬體 attestation、數位簽章或第三方認證。本展示不公開個人電腦路徑。</p></Panel></>;
}

function Engineering({ bundle, model, initialTab = 'performance' }: { bundle: Bundle; model: Model; initialTab?: string }) {
  const [tab,setTab]=useState(initialTab);
  return <div className="view-enter"><div className="page-intro"><p className="eyebrow">ENGINEERING, WITH CONTEXT</p><h1>看見效能，也看見邊界。</h1><p>同一個版本、同一個工作量。每份結果都保留來源與未完成事項。</p></div>
    <div className="engineering-tabs" role="tablist" aria-label="工程分析分類">{[['performance','效能比較',Gauge],['precision','精度與能源',Activity],['architecture','架構與關卡',Workflow],['sources','原件與參考',BookOpen]].map(([id,label,Icon])=>{const I=Icon as typeof Gauge;return <button key={String(id)} role="tab" aria-selected={tab===id} onClick={()=>setTab(String(id))}><I size={16}/>{String(label)}</button>;})}</div>
    <div role="tabpanel">{tab==='performance'?<Performance bundle={bundle} model={model}/>:tab==='precision'?<PrecisionEnergy bundle={bundle}/>:tab==='architecture'?<Architecture bundle={bundle}/>:<Sources bundle={bundle}/>}</div>
  </div>;
}

function PrintReport({ bundle }: { bundle: Bundle }) {
  return <section className="print-report"><header><h1>EdgeFHE｜邊緣隱私分析研究展示</h1><p>app_v5 / campaign v3 ＋ 兩機 LAN · 2026-10-24 · 公開摘要</p></header>
    <h2>系統與範圍</h2><p>KV260：ARM C++＋FPGA；N4096、3 RNS分量、2048 slots。正式數據為同板、單boot、十process sessions，不含網路／TLS。研究原型，非產品安全或醫療認證。</p>
    <h2>同板正式結果</h2><table><thead><tr><th>模型</th><th>CPU / PL P50 ms</th><th>W1比值 [95% CI]</th><th>W2 PL 筆/s</th><th>W2比值 [95% CI]</th></tr></thead><tbody>{bundle.models.map(x=>{const m=metricFor(bundle,x.id);return <tr key={x.id}><td>{x.short}{!x.primary?'（探索）':''}</td><td>{fmt(m.W1_cpu_P50_ms)} / {fmt(m.W1_candidate_P50_ms)}</td><td>{number(m.W1_latency_speedup.point,3)} [{ci(m.W1_latency_speedup)}]</td><td>{number(m.W2_candidate_records_s)}</td><td>{number(m.W2_sustained_speedup.point,3)} [{ci(m.W2_sustained_speedup)}]</td></tr>;})}</tbody></table>
    <p>比值是mean session ratios；CI為單boot marginal session bootstrap。W2是60秒admission並含drain，不是單筆latency倒數。主要模型是四／十特徵。</p>
    <h2>兩機 LAN 實測（含網路與 TLS）</h2><table><thead><tr><th>網路／模型</th><th>延遲 [95% CI]</th><th>吞吐 [95% CI]</th><th>P95</th></tr></thead><tbody>{bundle.lan.runs.flatMap(r=>r.models.map(m=><tr key={r.id+m.model}><td>{r.title}／{bundle.models.find(x=>x.id===m.model)!.short}</td><td>{number(m.latency.point,3)} [{ci(m.latency)}]</td><td>{number(m.records.point,3)} [{ci(m.records)}]</td><td>{number(m.P95.point,3)}</td></tr>))}</tbody></table>
    <p>KV260（ARM＋PL）↔ PC（Windows 原生雲端），mTLS，十 session。有線六小時浸泡 360/360 窗口通過，{number(bundle.lan.soak.activeHours,2)} 小時、最大誤差 {bundle.lan.soak.maxError.toExponential(2)}。Wi‑Fi 輪浸泡未完成。單板單 boot、單一 PC。</p>
    <h2>精度與功耗限制</h2><p>持續窗口超5e-4次數：AI4I 1、進展0、症狀25；logistic皆在當輪2e-3容忍值內。實測結果不是普遍解析精度保證。INA260 U14只量SOM rail，兩能效視窗比值{number(bundle.energy.models[0].recordsPerJRatio,2)}×／{number(bundle.energy.models[1].recordsPerJRatio,2)}×，單次30秒、沒有CI，非整板功耗。</p>
    <h2>目前與下一步</h2><ul>{bundle.gates.map(g=><li key={g.id}><strong>{g.title}：{stateLabels[g.state]}。</strong>{g.detail}</li>)}</ul>
    <h2>公開案例與證據</h2><p>離線案例只播放流程並顯示native C++明文affine參考；沒有當次CKKS或FPGA工作。原始資料與私鑰留在未來系統的owner信任邊界。展示包不含私鑰／私密CSV／ledger。</p><p className="print-hash">原件archive SHA256：{bundle.campaign.archiveSha256}<br/>審查摘要SHA256：{bundle.sources.find(s=>s.id==='campaign-v3-acceptance')!.sha256}</p>
    <h2>參考定位</h2><p>R2：同板CPU3。R1：OpenFHE等價設定仍需補證。Aloha-HE／RISE／HEAX做架構比較，平台與工作量不同，不做倍數排名。</p>
    {bundle.papers.filter(p=>['EF01','EF02','EF36','EF41'].includes(p.id)).map(p=><p className="print-reference" key={p.id}>{p.id} {p.title} ({p.year}). DOI: {p.doi}</p>)}
  </section>;
}

function Showcase({ bundle }: { bundle: Bundle }) {
  const [view,setView]=useState<View>(()=>window.location.hash==='#live'?'live':'tour');const [modelId,setModelId]=useState('diabetes-progression-ridge-v1');
  const [motion,setMotion]=useState(()=>!window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(()=>{
    const preference=window.matchMedia('(prefers-reduced-motion: reduce)');
    const respectPreference=()=>{if(preference.matches)setMotion(false);};
    respectPreference();preference.addEventListener('change',respectPreference);
    return()=>preference.removeEventListener('change',respectPreference);
  },[]);
  const [boardLive,setBoardLive]=useState(false);
  useEffect(()=>{let active=true;fetchLiveStatus().then(s=>{if(active)setBoardLive(!!s?.connected);});return()=>{active=false;};},[view]);
  const [modal,setModal]=useState<'sources'|'about'|null>(null);const [message,setMessage]=useState('');const [guide,setGuide]=useState(-1);
  const file=useRef<HTMLInputElement>(null);const model=bundle.models.find(m=>m.id===modelId)!;
  const guides=[['tour','先用一句話說明：資料在邊緣加密，服務端處理密文。'],['application','選擇一個公開案例，播放六個步驟。'],['system','兩台實體機器：三層架構、有線與 Wi‑Fi 對照、六小時浸泡。'],['engineering','比較同版本CPU3與PL；注意時間與CI的定義。'],['engineering','切到精度與能源，說明例外與量測範圍。'],['engineering','最後用架構與關卡，說明已完成與下一步。']];
  function go(v: View) { setView(v); window.scrollTo({top:0,behavior:motion?'smooth':'instant'}); }
  function guideTo(i: number) { setGuide(i);go(guides[i][0] as View); }
  const tabForGuide=guide===4?'precision':guide===5?'architecture':'performance';
  async function importFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f=e.target.files?.[0];if(!f)return;
    try {if(f.size>MAX_BUNDLE_BYTES)throw new Error('資料包超過2 MiB上限。');await decodePinnedBundle(new Uint8Array(await f.arrayBuffer()));setMessage('資料包完整雜湊與格式相符；本版核准來源已確認。');}
    catch(error){setMessage(error instanceof Error?error.message:'資料包無法核對');}finally{e.target.value='';}
  }
  const fullScreen=async()=>{try{await document.documentElement.requestFullscreen();}catch{setMessage('瀏覽器未允許全螢幕；仍可正常展示。');}};
  return <div className="app" data-motion={motion?'on':'off'}><a className="skip-link" href="#main">跳到主要內容</a>
    <aside className="sidebar"><a href="#" className="brand" onClick={e=>{e.preventDefault();go('tour');}}><img src={`${import.meta.env.BASE_URL}mark.svg`} alt=""/><div><strong>EdgeFHE<span>SHOWCASE</span></strong><small>隱私分析，從邊緣開始</small></div></a>
      <div className="nav-label">EXPLORE</div><nav aria-label="主要導覽">{[['tour','成果導覽',LayoutDashboard],['live','即時體驗',Radio],['application','應用體驗',Sparkles],['system','兩機實測',Network],['engineering','工程分析',Gauge]].map(([id,label,Icon])=>{const I=Icon as typeof Gauge;return <button key={String(id)} aria-label={String(label)} className={view===id?'selected':''} aria-current={view===id?'page':undefined} onClick={()=>go(id as View)}><I size={19}/><span>{String(label)}</span><ChevronRight size={15}/></button>;})}</nav>
      <div className="sidebar-project"><span className="eyebrow">THE RESEARCH STACK</span><div><Cpu size={16}/><strong>Kria KV260</strong></div><p>ARM Cortex-A53 + FPGA<br/>CKKS / RNS / NTT</p><div className="stack-tags"><span>N4096</span><span>3 RNS</span><span>12 lanes</span></div></div>
      <div className="sidebar-bottom"><div><span className="offline-dot" style={boardLive?{background:'#4fd19c'}:undefined}/>{boardLive?'即時展示模式':'離線展示模式'}</div><p>公開案例＋已審查原件<br/>不含即時硬體操作</p><button onClick={()=>setModal('about')}><CircleHelp size={16}/>關於這份展示</button></div></aside>
    <div className="workspace"><header className="topbar"><div className="breadcrumb">EdgeFHE<span>/</span><strong>{view==='tour'?'成果導覽':view==='live'?'即時體驗':view==='application'?'應用體驗':view==='system'?'兩機實測':'工程分析'}</strong></div><div className="top-tools"><span className="connection-label">{boardLive?'已連接開發板（即時）':'目前未連接開發板'}</span>{boardLive?<Badge tone="teal">LIVE</Badge>:<Badge>OFFLINE</Badge>}<button className="icon-button" aria-label="列印公開摘要" onClick={()=>window.print()}><Printer size={18}/></button><button className="icon-button" aria-label="全螢幕展示" onClick={fullScreen}><Fullscreen size={18}/></button><button className={`motion-toggle ${motion?'enabled':''}`} aria-label={`動畫${motion?'開':'關'}`} aria-pressed={motion} onClick={()=>setMotion(!motion)} title="切換教學動畫"><Activity size={16}/><span>動畫{motion?'開':'關'}</span></button></div></header>
      <div className="contextbar"><span><span className="source-check"/><strong>{view==='live'?'即時：這一頁的數字來自當場請求':view==='system'?'兩機 LAN 實測（有線＋Wi‑Fi）':'app_v5 / campaign v3'}</strong><span>已審查原件</span></span><label htmlFor="model-select">展示模型<select id="model-select" value={modelId} onChange={e=>{setModelId(e.target.value);setGuide(-1);}}>{bundle.models.map(m=><option key={m.id} value={m.id}>{m.short} · {m.featureCount} 特徵</option>)}</select></label></div>
      <main id="main"><div className="content">{view==='tour'?<Tour bundle={bundle} model={model} go={go} sources={()=>setModal('sources')}/>:view==='application'?<Application key={model.id} bundle={bundle} model={model} motion={motion}/>:view==='system'?<SystemView bundle={bundle} model={model}/>:view==='live'?<LiveView key={model.id} bundle={bundle} model={model}/>:<Engineering key={`${model.id}-${guide}`} bundle={bundle} model={model} initialTab={tabForGuide}/>}
        <footer className="page-footer"><span>EDGEFHE RESEARCH SHOWCASE <b>v2.0</b></span><span>數據有來源，限制有說明。</span><button onClick={()=>{setGuide(0);go('tour');}}><Play size={13}/>五分鐘導覽</button></footer></div></main></div>
    {guide>=0&&<div className="guide-bar"><span className="guide-count">0{guide+1} / 06</span><div><strong>五分鐘導覽</strong><p>{guides[guide][1]}</p></div><button className="icon-button" disabled={guide===0} aria-label="上一段導覽" onClick={()=>guideTo(guide-1)}><ChevronLeft size={19}/></button><button className="button primary" onClick={()=>guide===5?setGuide(-1):guideTo(guide+1)}>{guide===5?'結束導覽':'下一段'}<ChevronRight size={16}/></button><button className="text-button" onClick={()=>setGuide(-1)}>關閉</button></div>}
    {message&&<div className="toast" role="alert"><span>{message}</span><button className="text-button" onClick={()=>setMessage('')}>關閉</button></div>}
    {modal&&<Modal title={modal==='sources'?'來源、版本與量測邊界':'關於 EdgeFHE 展示'} close={()=>setModal(null)}>
      {modal==='sources'?<><Badge tone="teal">歷史原件已審查</Badge><h3>{bundle.campaign.title}</h3><p>{bundle.campaign.scope}</p><p className="note">{bundle.campaign.confidenceScope}</p><div className="source-list">{bundle.sources.map(s=><div key={s.id}><strong>{s.label}</strong><code>{s.sha256}</code></div>)}</div><p className="note">此處是原件來源摘要，不代表目前硬體就緒，也不是第三方安全認證。</p></>:
      <><h3>一個入口，三種看見專題的方式。</h3><p>成果導覽給教授與評審；應用體驗以公開案例說明流程；工程分析保留數據、精度例外、基準與驗收缺口。</p>
        <CheckItem>無外部字型、CDN 或分析追蹤。</CheckItem><CheckItem>不提交 FPGA、DMA、載入或 reset 操作。</CheckItem><CheckItem>公開案例是 C++ 明文參考，不冒充即時加密。</CheckItem>
        <div className="callout amber"><CircleHelp size={18}/><p>真機 adapter 尚未啟用。待原生兩機服務與身分綁定驗收後，才接入同一個介面。</p></div>
        <div className="modal-actions"><button className="button secondary" onClick={()=>file.current?.click()}><Upload size={16}/>核對公開資料包</button><button className="button secondary" onClick={()=>downloadJSON({schema:bundle.schema,version:bundle.version,campaign:bundle.campaign,sources:bundle.sources},'EdgeFHE-public-summary.json')}><Download size={16}/>匯出公開摘要</button></div>
        <p className="note">核對只接受本版固定SHA資料包，不會上傳檔案。需要更新資料時先經本機轉換器與審查，不直接用新檔替換正式結論。</p></>}
    </Modal>}
    <input ref={file} type="file" accept="application/json,.json" hidden aria-label="選擇公開資料包" onChange={importFile}/>
    <PrintReport bundle={bundle}/>
  </div>;
}

export default function App() {
  const [bundle,setBundle]=useState<Bundle|null>(null);const [error,setError]=useState('');
  useEffect(()=>{let active=true;loadBundle().then(b=>{if(active)setBundle(b);}).catch(e=>{if(active)setError(String(e));});return()=>{active=false;};},[]);
  if(error)return <div className="boot-state" role="alert"><ShieldCheck size={38}/><h1>展示資料尚未通過核對</h1><p>{error}</p><p>不使用替代數字填補缺少的原件。</p><button className="button primary" onClick={()=>window.location.reload()}>重新讀取本機資料</button></div>;
  if(!bundle)return <div className="boot-state"><Empty>正在核對本機公開資料與來源雜湊…</Empty></div>;
  return <Showcase bundle={bundle}/>;
}
