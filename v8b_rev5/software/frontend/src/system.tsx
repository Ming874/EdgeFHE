import { lazy, Suspense } from 'react';
import { ArrowRight, CircleHelp, Cloud, Cpu, Laptop, LockKeyhole, Network, ShieldCheck, Timer, Wifi } from 'lucide-react';

import { ci, LAN_MODEL_IDS, lanModelFor, number } from './data';
import { Badge, CheckItem, Metric, Panel } from './ui';
import type { Backend, Bundle, LanModel, LanRun, Model } from './types';

const SoakChart=lazy(()=>import('./charts').then(m=>({default:m.SoakChart})));
const parts=[['prepare','板上準備（正規化＋編碼＋加密）','#178365'],['net','網路＋TLS 往返＋回應驗證','#d3a86f'],['decrypt','解密與解碼','#3c797d']] as const;
const backendLabel: Record<Backend,string>={cpu:'CPU3（純 ARM）',pl:'ARM＋PL'};
const runModel=(r: LanRun,id: string)=>r.models.find(m=>m.model===id)!;

export function LanScope() {
  return <div className="scope-strip"><Badge tone="teal">兩機實測</Badge><span>有線 LAN · mTLS · 10 sessions · 單板單 boot</span><strong>含網路與 TLS</strong></div>;
}

function Tiers({ bundle }: { bundle: Bundle }) {
  const t=bundle.lan.topology;
  return <Panel eyebrow="FRONT · EDGE · CLOUD" title="三層架構：誰看得到什麼？" className="tiers-panel">
    <div className="tiers">
      <div><span className="circle-icon"><Laptop size={21}/></span><p className="eyebrow">前端</p><strong>操作與展示</strong><p>{t.front}</p><Badge>本頁目前離線</Badge></div>
      <div className="tier-link"><LockKeyhole size={15}/><span>擁有者範圍</span><ArrowRight size={16}/></div>
      <div className="tier-edge"><span className="circle-icon"><Cpu size={21}/></span><p className="eyebrow">中端 · KV260</p><strong>邊緣資料擁有者</strong><p>{t.edge}</p><Badge tone="teal">明文與私鑰只在這裡</Badge></div>
      <div className="tier-link"><ShieldCheck size={15}/><span>{t.transport}</span><ArrowRight size={16}/></div>
      <div><span className="circle-icon"><Cloud size={21}/></span><p className="eyebrow">後端 · PC</p><strong>密文評估服務</strong><p>{t.cloud}</p><Badge>只收密文</Badge></div>
    </div>
    <p className="note">中端與後端已在兩台實體機器上正式量測（{t.lan}）。前端即時模式（瀏覽器直接送單筆給板子）仍在開發，這一頁播放的是已審查原件，不是當次運算。</p>
  </Panel>;
}

function Decomposition({ m, title }: { m: LanModel; title: string }) {
  const max=Math.max(m.decompositionMs.cpu.total,m.decompositionMs.pl.total);
  return <div className="decomp"><h3>{title}</h3>{(['cpu','pl'] as const).map(be=>{const d=m.decompositionMs[be];return <div className="decomp-row" key={be}>
    <span>{backendLabel[be]}</span><div className="decomp-bar" style={{width:`${100*d.total/max}%`}} aria-label={`${backendLabel[be]} 中位數 ${number(d.total,2)} ms`}>
      {parts.map(([k,label,color])=><i key={k} title={`${label} ${number(d[k],2)} ms`} style={{flex:d[k],background:color}}/>)}</div>
    <strong>{number(d.total,1)}<small>ms</small></strong></div>;})}</div>;
}

export function SystemView({ bundle, model }: { bundle: Bundle; model: Model }) {
  const id=lanModelFor(model.id);const [wired,wifi]=bundle.lan.runs;const w=runModel(wired,id);const f=runModel(wifi,id);
  const short=(x: string)=>bundle.models.find(m=>m.id===x)!.short;const soak=bundle.lan.soak;
  return <div className="view-enter"><div className="page-intro"><p className="eyebrow">TWO MACHINES · REAL NETWORK</p><h1>邊緣加密、雲端算密文，真的跑在兩台機器上。</h1><p>KV260 負責資料擁有者的工作，PC 只收到密文。下面每個數字都來自經獨立審查的原件，包含網路與 TLS 的時間。</p></div>
    {id!==model.id&&<div className="callout amber"><CircleHelp size={20}/><div><strong>{model.short}沒有納入兩機正式比較。</strong><p>兩機協定只量了兩個主要模型，以下改顯示{short(id)}；不把同板結果搬到網路情境。</p></div></div>}
    <LanScope/>
    <div className="metrics-grid"><Metric label={`單筆延遲加速 · ${short(id)}`} value={number(w.latency.point,3)} unit="×" foot={`95% CI ${ci(w.latency)} · 有線`} accent/>
      <Metric label="持續吞吐加速" value={number(w.records.point,3)} unit="×" foot={`95% CI ${ci(w.records)} · 60 秒含 drain`}/>
      <Metric label="PL 單筆 E2E 中位數" value={number(w.decompositionMs.pl.total,1)} unit="ms" foot={`CPU3 ${number(w.decompositionMs.cpu.total,1)} ms · 含網路`}/>
      <Metric label="PL 持續吞吐" value={number(w.windowRecordsPerS.pl)} unit="筆/s" foot={`CPU3 ${number(w.windowRecordsPerS.cpu)} 筆/s · 10 session 平均`}/></div>
    <Tiers bundle={bundle}/>
    <div className="two-column"><Panel eyebrow="WIRED VS WI-FI · SAME PACKAGE" title="換成有線之後，加速才看得出來" action={<span className="tiny muted">CPU ÷ PL，越高越好</span>}>
      <div className="table-scroll"><table className="lan-table"><thead><tr><th>網路</th><th>模型</th><th>延遲</th><th>吞吐</th><th>P95</th><th>網路中位數</th></tr></thead><tbody>
        {bundle.lan.runs.flatMap(r=>r.models.map(m=><tr key={r.id+m.model}><th>{r.id==='wired'?<Network size={13}/>:<Wifi size={13}/>} {r.title}</th><td>{short(m.model)}</td><td>{number(m.latency.point,3)}×</td><td>{number(m.records.point,3)}×</td><td>{number(m.P95.point,3)}×</td><td>{number(m.decompositionMs.pl.net,1)} ms</td></tr>))}
      </tbody></table></div>
      <p className="note">兩輪程式包逐位元相同，只差網路。Wi‑Fi 的網路往返 {number(f.decompositionMs.pl.net,1)} ms 吃掉大部分時間；有線降到 {number(w.decompositionMs.pl.net,1)} ms，PL 省下的加密時間才顯現。{wifi.note}</p></Panel>
      <Panel eyebrow="MEDIAN OF ONE REQUEST · ROWS = 1" title="一筆請求的時間花在哪裡？"><Decomposition m={w} title="有線 LAN"/><Decomposition m={f} title="Wi-Fi LAN"/>
        <div className="chart-legend">{parts.map(([k,label,color])=><span key={k}><i className="legend-dot" style={{background:color}}/>{label}</span>)}</div>
        <p className="note">各段為獨立中位數，不一定相加等於總和中位數。雲端只做公開權重乘加，伺服器端計算中位數 {number(wired.serverCloudMedianMs,3)} ms。</p></Panel></div>
    <Panel eyebrow="SIX HOURS · 360 WINDOWS · WIRED" title="連續六小時，吞吐沒有往下掉" action={<Badge tone="teal">{soak.windows}/{soak.windows} 窗口通過</Badge>}>
      <Suspense fallback={<div className="chart chart-loading">載入浸泡圖表…</div>}><SoakChart series={soak.series} names={Object.fromEntries(LAN_MODEL_IDS.map(x=>[x,short(x)]))}/></Suspense>
      <div className="chart-legend">{LAN_MODEL_IDS.map((x,i)=><span key={x}><i className="legend-dot" style={{background:['#178365','#d3a86f'][i]}}/>{short(x)}（每窗口 60 秒持續吞吐）</span>)}</div>
      <div className="soak-grid">{LAN_MODEL_IDS.map(x=>{const s=soak.perModel[x];return <div key={x}><span>{short(x)} · {s.windows} 窗口</span><strong>{number(s.meanRecordsPerS)}<small>筆/s</small></strong>
        <dl><div><dt>第一小時 → 最後一小時</dt><dd>{number(s.firstHour)} → {number(s.lastHour)}</dd></div><div><dt>單一請求 P50 / P99</dt><dd>{number(s.p50Ms,1)} / {number(s.p99Ms,1)} ms</dd></div></dl></div>;})}
        <div><span>六小時總量</span><strong>{number(soak.activeHours,2)}<small>小時</small></strong><dl><div><dt>請求／紀錄</dt><dd>{number(soak.requests)} / {number(soak.records/1e8,1)} 億</dd></div><div><dt>DMA／最大誤差</dt><dd>{number(soak.deviceCalls)} / {soak.maxError.toExponential(2)}</dd></div></dl></div></div>
      <p className="note">每個窗口 60 秒、2,048 筆一批、管線 2 槽，每窗口重新建立金鑰與 TLS。吞吐包含 drain；冷啟動與窗口間隔不計入有效時間。</p></Panel>
    <Panel eyebrow="INDEPENDENTLY RE-CHECKED" title="不是只看板子自己報的數字"><div className="integrity-grid">
      <CheckItem>{number(wired.connections)} 條連線：板子送出的請求與 PC 記錄的回應，雜湊逐筆相同（{number(wired.serverResponses ?? 0)} 筆）。</CheckItem>
      <CheckItem>每個子程式的 FPGA 中斷增量都等於它回報的 DMA 次數；全程 {number(wired.deviceCalls)} 次。</CheckItem>
      <CheckItem>統計由第二套程式從原始紀錄重算，與板端結果差距在 10⁻⁹ 以內。</CheckItem>
      <CheckItem>PC 端心跳每 5 秒一筆，最長間隔不到 6 秒；伺服器結束碼 0。</CheckItem>
    </div><p className="note"><Timer size={12}/> 範圍：一塊板、一次開機、一台 PC、路由器同網段。信賴區間是單一開機的 session bootstrap 邊際區間，不是跨開機保證。</p></Panel>
  </div>;
}
