import { BUNDLE_SHA256 } from './generated/bundleDigest';
import type { Bundle, Interval, Lan, LiveResult, LiveStatus, PublicCase, ShowcaseAdapter } from './types';

export const MAX_BUNDLE_BYTES = 2 * 1024 * 1024;
export const MODEL_IDS = ['ai4i-failure-logreg-v1', 'diabetes-progression-ridge-v1', 'diabetes-symptom-logreg-v1'];
export const LAN_MODEL_IDS = MODEL_IDS.slice(0, 2);
// the two-machine protocol measured only the two primary models; others fall back to ridge with an explicit notice
export const lanModelFor = (id: string) => LAN_MODEL_IDS.includes(id) ? id : 'diabetes-progression-ridge-v1';
function need(value: unknown, text: string): asserts value {
  if (!value) throw new Error(`資料核對失敗：${text}`);
}
const finite = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
const positive = (n: unknown) => finite(n) && n > 0;
const hex = (s: unknown) => typeof s === 'string' && /^[a-f0-9]{64}$/.test(s);
function interval(x: Interval) {
  need(x && positive(x.point) && Array.isArray(x.CI95) && x.CI95.length === 2 &&
    x.CI95.every(positive) && x.CI95[0] <= x.point && x.point <= x.CI95[1], '信賴區間');
}
function validateLan(l: Lan, sourceIds: Set<string>) {
  need(l && l.provenance === 'checked-evidence' && l.networkE2E === true && l.wholeProjectAcceptance === false, '兩機實測範圍');
  need(Array.isArray(l.runs) && l.runs.length === 2 && l.runs[0].id === 'wired' && l.runs[1].id === 'wifi', '兩機實測組別');
  l.runs.forEach(r => {
    need(sourceIds.has(r.sourceId) && r.sessions === 10 && positive(r.connections) && positive(r.deviceCalls) && positive(r.serverCloudMedianMs), '兩機實測計數');
    need(r.models.length === 2 && r.models.every((m, i) => m.model === LAN_MODEL_IDS[i]), '兩機實測模型');
    r.models.forEach(m => {
      [m.latency, m.records, m.P95].forEach(interval);
      need(m.latency.CI95[0] > 1 && m.records.CI95[0] > 1 && m.P95.CI95[0] >= 1, '兩機主要關卡');
      need((['cpu', 'pl'] as const).every(be => Object.values(m.decompositionMs[be]).every(positive) && positive(m.windowRecordsPerS[be])), '兩機時間分解');
    });
  });
  need(l.runs[0].soakComplete === true && l.runs[0].soakWindows === 360 && l.runs[1].soakComplete === false, '浸泡完成狀態');
  const s = l.soak;
  need(sourceIds.has(s.sourceId) && s.windows === 360 && s.series.length === 360 && s.series.every((x, i) => x.index === i &&
    LAN_MODEL_IDS.includes(x.model) && positive(x.recordsPerS) && positive(x.activeHour)), '浸泡窗口');
  need(s.activeHours >= 6 && positive(s.requests) && s.records === s.requests * 2048 && positive(s.deviceCalls) && finite(s.maxError) && s.maxError < 5e-4, '浸泡總量');
  need(LAN_MODEL_IDS.every(id => s.perModel[id] && s.perModel[id].windows === 180 && positive(s.perModel[id].firstHour) && positive(s.perModel[id].lastHour)), '浸泡模型摘要');
}
export function validateBundle(value: unknown): Bundle {
  need(value && typeof value === 'object' && !Array.isArray(value), '資料包格式');
  const b = value as Bundle;
  const expected = ['schema','version','generatedOn','sources','models','cases','campaign','hardware','sensors','energy','papers','gates','lan'].sort();
  need(JSON.stringify(Object.keys(b).sort()) === JSON.stringify(expected), '資料包欄位');
  need(b.schema === 'edgefhe.showcase.v2' && b.version === '2.0.0', '資料版本');
  need(Array.isArray(b.models) && b.models.length === 3 && b.models.every((m, i) => m.id === MODEL_IDS[i] &&
    hex(m.sourceSha256) && m.featureCount === m.features.length && m.features.length === [4,10,16][i]), '模型身分');
  need(Array.isArray(b.sources) && b.sources.length >= 12 && b.sources.every(s => hex(s.sha256)), '來源雜湊');
  need(new Set(b.sources.map(s => s.id)).size === b.sources.length, '重複來源');
  need(Array.isArray(b.cases) && b.cases.length === 12 && new Set(b.cases.map(c => c.id)).size === 12, '公開案例數量');
  b.cases.forEach(c => {
    const m = b.models.find(m => m.id === c.modelId);
    need(m && c.provenance === 'reference-demo' && c.referenceKind === 'native-cpp-public-scalar-affine', '案例來源');
    need([c.raw,c.normalized,c.contributions].every(a => Array.isArray(a) && a.length === m.featureCount && a.every(finite)), '案例維度');
    need(c.normalized.every(v => Math.abs(v) <= m.inputAbsMax) && finite(c.score) && finite(c.bias), '案例範圍');
    const score = c.contributions.reduce((a,v) => a+v, c.bias);
    need(Math.abs(score-c.score) < 1e-10, '參考分數一致性');
  });
  const c = b.campaign;
  need(c && c.provenance === 'checked-evidence' && c.networkE2E === false && c.wholeProjectAcceptance === false &&
    c.sessions === 10 && c.pairs === 30 && c.warmups === 3 && c.windowSeconds === 60 &&
    c.acceptedStatus === 'NATIVE_CPP_V3_ARCHIVE_SOURCE_ELF_IRQ_GATES_PROTOCOL_INDEPENDENT_STATISTICS_SENSOR_PASS', '實測範圍');
  need(c.deviceCalls === 550850 && c.sessionCalls === 534540 && hex(c.archiveSha256), '實測計數');
  need(c.models.length === 3 && c.models.every((m,i) => m.model === MODEL_IDS[i]), '實測模型');
  c.models.forEach(m => {
    need([m.W1_cpu_P50_ms,m.W1_candidate_P50_ms,m.W2_cpu_records_s,m.W2_candidate_records_s].every(positive), '效能值');
    interval(m.W1_latency_speedup); interval(m.W2_sustained_speedup);
    interval(m.P95_speedup['1']); interval(m.P95_speedup['2048']);
    need(Object.values(m.W2_window_max_error).every(v => finite(v) && v>=0), '誤差值');
    Object.values(m.stage_median_of_session_medians_ms).forEach(row => Object.values(row).forEach(t =>
      need(Object.values(t).every(v => finite(v) && v>=0), '階段時間')));
  });
  need(MODEL_IDS.every((id,i) => c.precisionExceedances[id] === [1,0,25][i]), '精度例外次數');
  need(b.hardware.n === 4096 && b.hardware.slots === 2048 && b.hardware.resources.length === 5 &&
    b.hardware.resources.every(r => finite(r.percent) && r.percent >= 0 && r.percent <= 100), '硬體基準');
  need(b.sensors.sessions.length === 10 && b.sensors.provenance === 'checked-evidence' &&
    b.sensors.sessions.every(r => [r.powerW,r.temperatureC,r.samples].every(positive)), '感測摘要');
  need(b.energy.hasCI === false && b.energy.windowSeconds === 30 && b.energy.models.length === 2 &&
    b.energy.models.every(r => positive(r.recordsPerJRatio)), '能源範圍');
  need(b.gates.length === 8 && b.gates.every(g => ['accepted','partial','pending'].includes(g.state)), '驗收狀態');
  need(!b.gates.some(g => g.id === 'release' && g.state === 'accepted'), '不得宣稱整體完成');
  validateLan(b.lan, new Set(b.sources.map(s => s.id)));
  need(b.papers.length === 9 && b.papers.every(p => p.url === `https://doi.org/${p.doi}`), '論文定位');
  return b;
}
export async function decodePinnedBundle(raw: Uint8Array): Promise<Bundle> {
  need(raw.byteLength > 0 && raw.byteLength <= MAX_BUNDLE_BYTES, '檔案大小上限 2 MiB');
  const hash = await crypto.subtle.digest('SHA-256', new Uint8Array(raw));
  const digest = Array.from(new Uint8Array(hash), x => x.toString(16).padStart(2, '0')).join('');
  need(digest === BUNDLE_SHA256, '來源身分不符；只接受本版固定的公開資料包');
  return validateBundle(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)));
}
export async function loadBundle(): Promise<Bundle> {
  const response = await fetch(`${import.meta.env.BASE_URL}data/showcase.json`, { cache: 'no-store' });
  need(response.ok, '找不到本機展示資料；請透過本機啟動器開啟');
  return decodePinnedBundle(new Uint8Array(await response.arrayBuffer()));
}
export class OfflineAdapter implements ShowcaseAdapter {
  constructor(private bundle: Bundle) {}
  connection() { return 'offline' as const; }
  async models() { return this.bundle.models; }
  async evidence() { return this.bundle; }
  async referenceCase(id: string): Promise<PublicCase> {
    const c = this.bundle.cases.find(c => c.id === id); need(c, '不存在的公開案例'); return c;
  }
  async runInference(): Promise<never> { throw new Error('真機服務未接入；離線模式不提交推論或板端操作。'); }
  subscribe() { return () => undefined; }
}
export function number(n: number, digits = 0) {
  return new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
}
export const ci = (x: Interval) => `${number(x.CI95[0],3)}–${number(x.CI95[1],3)}`;
export function downloadJSON(value: unknown, filename: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\n'], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Live replies come from the local console; anything malformed is rejected instead of being drawn as a result.
export function validateLiveResult(value: unknown): LiveResult {
  const r = value as LiveResult;
  need(r && r.type === 'result' && r.provenance === 'live' && typeof r.request_id === 'string' && Number.isInteger(r.index), '即時結果格式');
  const keys = Object.keys(r.results || {});
  need(keys.length >= 1 && keys.every(k => k === 'cpu' || k === 'pl'), '即時結果後端');
  need(Array.isArray(r.normalized) && r.normalized.every(finite) && finite(r.reference?.score), '即時參考值');
  Object.values(r.results).forEach(x => {
    need(x && [x.score, x.abs_error, x.prepare_ms, x.network_ms, x.decrypt_ms, x.request_ms, x.cold_context_and_TLS_ms].every(finite) &&
      hex(x.request_sha256) && hex(x.response_sha256) && positive(x.upload_bytes) && positive(x.response_bytes) && Number.isInteger(x.dma) && x.dma >= 0, '即時結果數值');
  });
  need(!r.results.cpu || r.results.cpu.dma === 0, 'CPU 不應有 DMA');
  Object.values(r.results).forEach(x => need(x.samples === undefined || (Array.isArray(x.samples) && x.samples.length === x.repeat &&
    x.samples.every(s => [s.request_ms, s.prepare_ms, s.network_ms, s.decrypt_ms].every(finite) && hex(s.request_sha256))), '重複量測樣本'));
  return r;
}
const liveCodes: Record<string, string> = {
  INPUT_OUT_OF_RANGE: '超出模型可接受範圍（標準化後超過 ±8 個標準差），板子拒絕處理，沒有執行任何加密。',
  INPUT_UCI_BINARY: '只能填 0 或 1。', INPUT_UCI_AGE: '需要是 16 到 90 的整數。', INPUT_NOT_FINITE: '不是有效數字。',
  INPUT_DIMENSIONS: '欄位數量不符。', MODEL_UNKNOWN: '開發板不認得這個模型。', CONSOLE_BUSY: '上一筆還在處理，請稍候再送。',
  GATEWAY_NOT_CONNECTED: '主控台尚未連上開發板，請按「連線開發板」。',
  GATEWAY_CHANNEL_LOST_NOT_RETRIED: '與開發板的連線中斷；這一筆沒有自動重送，請確認板子狀態後再試。',
  SESSION_LIMIT: '本次展示的請求數已達上限，請在板子上結束並重新啟動閘道。',
};
export function liveErrorText(data: Record<string, unknown>, labels: string[], features: string[]) {
  const code = String(data.code ?? 'UNKNOWN'); const detail = typeof data.detail === 'string' ? data.detail : '';
  const at = features.indexOf(detail); const field = at >= 0 ? `「${labels[at]}」` : '';
  return `${field}${liveCodes[code] ?? '請求未完成'}（${code}）`;
}

// The local console answers this; the offline static server returns 503, which means "no live hardware".
export async function fetchLiveStatus(): Promise<LiveStatus | null> {
  try {
    const r = await fetch('/api/live/v1/status', { cache: 'no-store' });
    return r.ok ? await r.json() as LiveStatus : null;
  } catch { return null; }
}
