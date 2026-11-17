import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, CircleCheck, Cloud, Cpu, Fingerprint, Laptop, LockKeyhole, RefreshCw, Send, TriangleAlert, Zap } from 'lucide-react';

import { fetchLiveStatus, liveErrorText, number, validateLiveResult } from './data';
import { featureNames } from './labels';
import { Badge, Panel } from './ui';
import type { Backend, Bundle, LiveBackendResult, LiveResult, LiveStatus, Model } from './types';

const API = '/api/live/v1';
const backendName: Record<Backend, string> = { cpu: '純 CPU（ARM）', pl: 'ARM＋FPGA（PL）' };
const kib = (n: number) => `${number(n / 1024)} KiB`;
const short = (h: string) => `${h.slice(0, 12)}…`;

// Wall-clock ticker for the "processing" label only; measured stage times come from the board.
function startTicker(set: (ms: number) => void) {
  const t0 = performance.now(); const id = window.setInterval(() => set(performance.now() - t0), 100);
  return () => window.clearInterval(id);
}

async function post(path: string, body: unknown) {
  const r = await fetch(API + path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-EdgeFHE-Live': '1' }, body: JSON.stringify(body), cache: 'no-store' });
  return { status: r.status, data: await r.json().catch(() => ({})) as Record<string, unknown> };
}

function Interpretation({ model, value }: { model: Model; value: number }) {
  if (model.task === 'regression') {
    const scaled = model.targetMean !== null && model.targetStd !== null ? model.targetMean + value * model.targetStd : null;
    return <p>標準化進展分數{scaled !== null && <>，換算回資料集的進展指標約 <strong>{number(scaled, 1)}</strong></>}。這是研究用迴歸分數，不是診斷。</p>;
  }
  const p = 1 / (1 + Math.exp(-value));
  return <p>logit {number(value, 4)}，換算機率約 <strong>{number(p * 100, 1)}%</strong>，{value > Math.log(model.threshold / (1 - model.threshold)) ? '高於' : '低於'}研究門檻。公開研究模型，不是臨床或工業認證。</p>;
}

function Timeline({ r, scale }: { r: LiveBackendResult; scale: number }) {
  const parts = [['prepare_ms', '板上準備：正規化＋編碼＋加密', '#178365'], ['network_ms', '網路＋TLS＋PC 密文運算', '#d3a86f'], ['decrypt_ms', '板上解密', '#3c797d']] as const;
  return <div className="live-timeline"><div className="live-bar" style={{ width: `${Math.max(8, 100 * r.request_ms / scale)}%` }}>
    {parts.map(([k, label, color]) => <i key={k} title={`${label} ${number(r[k], 2)} ms`} style={{ flex: Math.max(.02, r[k]), background: color }} />)}</div>
    <dl>{parts.map(([k, label, color]) => <div key={k}><dt><i style={{ background: color }} />{label}</dt><dd>{number(r[k], 2)} ms</dd></div>)}</dl></div>;
}

function Evidence({ r, result }: { r: LiveBackendResult; result: LiveResult }) {
  return <ul className="live-evidence">
    <li><LockKeyhole size={14} />上傳密文 <strong>{kib(r.upload_bytes)}</strong>（{r.ciphertexts} 個密文）· SHA-256 <code title={r.request_sha256}>{short(r.request_sha256)}</code></li>
    <li><Cloud size={14} />{r.cloud_log ? <>PC 伺服器紀錄找到同一個雜湊（第 {r.cloud_log.sequence} 筆），密文運算 <strong>{number(r.cloud_log.cloud_us, 0)} µs</strong>，回傳雜湊{r.cloud_log.response_digest_matches ? '一致' : '不一致'}</> : <>PC 伺服器紀錄尚未對到這個雜湊（主控台未提供伺服器紀錄）</>}</li>
    <li><Fingerprint size={14} />回傳密文 <strong>{kib(r.response_bytes)}</strong> · SHA-256 <code title={r.response_sha256}>{short(r.response_sha256)}</code></li>
    {r.dma > 0 ? <li><Zap size={14} />FPGA DMA <strong>+{r.dma}</strong> 次{(r.repeat ?? 1) > 1 ? `（${r.repeat} 次執行合計）` : ''}（PL {number(r.PL_hz / 1e6, 1)} MHz；整筆 IRQ {Object.entries(result.irq_before).map(([k, v]) => `${k}: ${number(v)} → ${number(result.irq_after[k])}`).join('、')}）</li>
      : <li><Cpu size={14} />{r.fixture && r.device_calls > 0 ? '本機測試：以軟體規格模擬 PL，沒有實際 DMA' : '純 CPU 路徑，沒有使用 FPGA'}</li>}
    <li><RefreshCw size={14} />每筆請求都在板上重新產生金鑰並建立 TLS：{number(r.cold_context_and_TLS_ms, 1)} ms（不計入上方單筆時間）</li>
  </ul>;
}

export function LiveView({ bundle, model: initial }: { bundle: Bundle; model: Model }) {
  const [status, setStatus] = useState<LiveStatus | null>(null); const [offline, setOffline] = useState(false);
  const [modelId, setModelId] = useState(initial.id); const model = bundle.models.find(m => m.id === modelId)!;
  const cases = bundle.cases.filter(c => c.modelId === modelId);
  const [values, setValues] = useState<string[]>(() => cases[0].raw.map(v => String(Number(v.toFixed(3)))));
  const [compare, setCompare] = useState(true); const [repeat, setRepeat] = useState(3); const [busy, setBusy] = useState(false); const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<LiveResult | null>(null); const [error, setError] = useState(''); const [history, setHistory] = useState<LiveResult[]>([]);
  const stopTicker = useRef<() => void>(() => undefined);

  const refresh = useCallback(async () => {
    const s = await fetchLiveStatus(); setStatus(s); setOffline(s === null);
  }, []);
  useEffect(() => {
    let active = true;
    fetchLiveStatus().then(s => { if (active) { setStatus(s); setOffline(s === null); } });
    return () => { active = false; stopTicker.current(); };
  }, []);

  function pickModel(id: string) {
    setModelId(id); setResult(null); setError('');
    setValues(bundle.cases.find(c => c.modelId === id)!.raw.map(v => String(Number(v.toFixed(3)))));
  }
  async function connect() { setError(''); try { const r = await post('/connect', {}); setStatus(r.data as unknown as LiveStatus); } catch { setOffline(true); } }
  async function submit() {
    const features = values.map(v => Number(v));
    const bad = features.findIndex(v => values[features.indexOf(v)] === '' || !Number.isFinite(v));
    if (bad >= 0) { setError(`「${featureNames[model.features[bad]] || model.features[bad]}」不是有效數字。`); return; }
    setBusy(true); setError(''); setElapsed(0); stopTicker.current = startTicker(setElapsed);
    try {
      const r = await post('/infer', { model: modelId, features, backends: compare ? ['cpu', 'pl'] : ['pl'], repeat });
      if (r.status === 200) { const ok = validateLiveResult(r.data); setResult(ok); setHistory(h => [ok, ...h].slice(0, 12)); }
      else { setError(liveErrorText(r.data, model.features.map(f => featureNames[f] || f), model.features)); setResult(null); }
    } catch (e) { setError(e instanceof Error ? e.message : '主控台沒有回應'); }
    finally { stopTicker.current(); setBusy(false); void refresh(); }
  }

  const g = status?.gateway; const ready = !!status?.connected && !!g;
  const pl = result?.results.pl; const cpu = result?.results.cpu;
  const scale = Math.max(pl?.request_ms ?? 0, cpu?.request_ms ?? 0, 1);
  return <div className="view-enter">
    <div className="page-intro"><p className="eyebrow">LIVE · YOUR INPUT · REAL HARDWARE</p><h1>輸入一筆資料，看它在 KV260 上加密、在 PC 上算密文。</h1>
      <p>按下送出後，這筆資料會送到開發板：板子正規化、用 FPGA 加密，PC 只收到密文並做模型運算，結果傳回板子解密。下面的時間與雜湊都是這一筆的實際量測。</p></div>
    <div className="live-status" aria-label="即時連線狀態">
      <div className="ok"><Laptop size={18} /><div><strong>瀏覽器 → 本機主控台</strong><span>{offline ? '未啟動（離線展示模式）' : '127.0.0.1 · 已連線'}</span></div></div><ArrowRight size={16} />
      <div className={ready ? 'ok' : 'off'}><Cpu size={18} /><div><strong>KV260 邊緣閘道</strong><span>{ready ? `boot ${g!.boot_tag} · ${g!.session} · fastpath ${g!.fastpath}${g!.fixture ? ' · 本機模擬' : ''}` : status?.error ? '未連線：' + status.error : '未連線'}</span></div></div><ArrowRight size={16} />
      <div className={status?.server_log ? 'ok' : 'off'}><Cloud size={18} /><div><strong>PC 密文服務</strong><span>{status?.server_log ? '伺服器紀錄可供比對' : '未提供伺服器紀錄'}</span></div></div>
      <button className="button secondary" onClick={() => void (offline ? refresh() : connect())} disabled={busy}><RefreshCw size={15} />{offline ? '重新檢查' : ready ? '重新連線' : '連線開發板'}</button>
    </div>
    {offline && <div className="callout amber"><TriangleAlert size={20} /><div><strong>目前是離線展示，沒有連到開發板。</strong><p>即時體驗需要：開發板上執行邊緣閘道、電腦上執行 START_LIVE_DEMO（雲端服務＋本機主控台）。離線時這一頁不會產生任何假結果。</p></div></div>}
    <div className="live-layout">
      <Panel eyebrow={`${model.featureCount} FEATURES · YOUR RECORD`} title="輸入資料">
        <label className="field-label" htmlFor="live-model">模型</label>
        <select id="live-model" value={modelId} onChange={e => pickModel(e.target.value)} disabled={busy}>{bundle.models.map(m => <option key={m.id} value={m.id}>{m.title}（{m.featureCount} 特徵）</option>)}</select>
        <div className="live-presets"><span>帶入公開案例：</span>{cases.map(c => <button key={c.id} className="text-button" disabled={busy} onClick={() => { setValues(c.raw.map(v => String(Number(v.toFixed(3))))); setResult(null); }}>{c.label}</button>)}</div>
        <div className="live-fields">{model.features.map((f, i) => <label key={f}><span title={f}>{featureNames[f] || f}</span>
          <input inputMode="decimal" aria-label={featureNames[f] || f} value={values[i] ?? ''} disabled={busy} onChange={e => setValues(v => v.map((x, j) => j === i ? e.target.value : x))} /></label>)}</div>
        <label className="live-check"><input type="checkbox" checked={compare} disabled={busy} onChange={e => setCompare(e.target.checked)} />同一筆資料也用純 CPU 跑一次，當場比較</label>
        <label className="live-repeat" htmlFor="live-repeat">重複量測<select id="live-repeat" value={repeat} disabled={busy} onChange={e => setRepeat(Number(e.target.value))}>
          <option value={1}>1 次（最快）</option><option value={3}>3 次取中位數（建議）</option><option value={5}>5 次取中位數</option></select></label>
        <button className="button primary live-submit" disabled={!ready || busy} onClick={() => void submit()}>{busy ? <><span className="spinner" />處理中 {number(elapsed / 1000, 1)} s</> : <><Send size={16} />加密並送出</>}</button>
        <p className="note">資料只經過：本機主控台 → mTLS → 開發板。PC 雲端服務只收到密文。這一頁和主控台紀錄都不保存輸入值與分數。</p>
        {error && <div className="callout amber live-error" role="alert"><TriangleAlert size={18} /><p>{error}</p></div>}
      </Panel>
      <Panel eyebrow="DECRYPTED ON THE BOARD · MEASURED" title="結果與過程" action={result && <Badge tone={pl?.fixture ? 'amber' : 'teal'}>{pl?.fixture ? '本機模擬（非 FPGA）' : 'LIVE · 實機'}</Badge>}>
        {!result ? <div className="result-placeholder"><Fingerprint size={22} /><span>{busy ? '開發板正在處理這一筆…' : '送出後，這裡顯示解密結果、每段實測時間與密文雜湊'}</span></div> : <div className="live-result" role="status">
          <div className="live-score"><span className="eyebrow">KV260 解密結果 · {model.short}</span><strong>{number((pl ?? cpu)!.score, 6)}</strong>
            <Interpretation model={model} value={(pl ?? cpu)!.score} />
            <p className="live-check-line"><CircleCheck size={15} />與板上明文參考 {number(result.reference.score, 6)} 相差 {(pl ?? cpu)!.abs_error.toExponential(2)}（容許 {model.task === 'regression' ? '5e-4' : '2e-3'}）</p></div>
          {(['pl', 'cpu'] as const).map(b => { const r = result.results[b]; const xs = r?.samples?.map(s => s.request_ms) ?? []; return r && <div className="live-backend" key={b}><h3>{backendName[b]} · {xs.length > 1 ? `${xs.length} 次中位數 ${number(r.request_ms, 1)} ms（範圍 ${number(Math.min(...xs), 1)}–${number(Math.max(...xs), 1)} ms）` : `單筆 ${number(r.request_ms, 1)} ms`}</h3><Timeline r={r} scale={scale} /><Evidence r={r} result={result} /></div>; })}
          {pl && cpu && <div className="live-compare"><strong>{number(cpu.request_ms / pl.request_ms, 2)}×</strong><span>這一筆含網路的單筆時間，PL 比純 CPU 快的倍數{(pl.repeat ?? 1) > 1 ? `（各 ${pl.repeat} 次、交替先後順序後取中位數）` : '（單次量測，容易受網路抖動影響）'}；這是現場觀察，不是統計結論，正式統計見「兩機實測」</span></div>}
          <p className="note">瀏覽器到結果的總時間 {number(result.console_round_trip_ms, 0)} ms，包含主控台轉送、板上建立程序與金鑰；第 {result.index} 筆 · 本次 FPGA DMA 累計 {number(result.session_dma)} 次。</p>
        </div>}
      </Panel>
    </div>
    {history.length > 0 && <Panel eyebrow="THIS SESSION" title="這次展示的紀錄"><div className="table-scroll"><table className="lan-table"><thead><tr><th>#</th><th>模型</th><th>PL 結果</th><th>PL 單筆</th><th>CPU 單筆</th><th>DMA</th></tr></thead><tbody>
      {history.map(h => <tr key={h.request_id}><th>{h.index}</th><td>{bundle.models.find(m => m.id === h.model)!.short}</td><td>{h.results.pl ? number(h.results.pl.score, 5) : '—'}</td><td>{h.results.pl ? `${number(h.results.pl.request_ms, 1)} ms` : '—'}</td><td>{h.results.cpu ? `${number(h.results.cpu.request_ms, 1)} ms` : '—'}</td><td>{h.results.pl ? `+${h.results.pl.dma}` : '0'}</td></tr>)}
    </tbody></table></div><p className="note">紀錄只存在這個瀏覽器分頁的記憶體，重新整理就會清除。</p></Panel>}
  </div>;
}
