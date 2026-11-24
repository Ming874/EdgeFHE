// EdgeFHE owner screen: pure logic shared by the browser (app.js) and the node tests (tests/lib.test.mjs).
// No DOM access. Envelopes and event data follow demo/edge/EVENTS.md (sections 2 and 3): the gateway emits
// form.accepted twice (public, then owner), client.spawned, progress.irq / progress.io from the 25 ms sampler,
// client.exited (with the client JSON as `report`), irq.delta, result (owner) or a fatal error, and wiped.
// Exact stage durations come from the client JSON and the gateway timestamps; live markers come from the board's
// kernel counters. The screen shows a risk level and the observed rate of the test data; it never reads or shows
// the sigmoid of the class-balanced fit that the result event also carries.

export const STAGE_COUNT = 13;
export const SAMPLE_MS = 25;
export const DEFAULT_SIZES = Object.freeze({
  request_frame_bytes: 393472, response_frame_bytes: 98560, public_key_bytes: 98304,
});
const HEADER_BYTES = 256;            // wire.hpp:5
const RING_N = 4096;                 // CKKS ring dimension
const TLS_RECORD_MAX = 16384;        // tls.hpp:120 writes at most 16,384 B per record
const TLS13_EXPANSION = 17;          // inner content type (1 B) + AEAD tag (16 B)
const TLS_RECORD_HEADER = 5;
const UPLOAD_STARTED_BYTES = 32768;  // more than any handshake flight of the client
const MAX_ENC_K = 4;                 // ciphertexts per ENC call (owner_v5_core.hpp:8)

export const BACKEND_LABELS = Object.freeze({ pl: 'FPGA（PL）', spec: 'PL 位元精確軟體模型', cpu: 'ARM' });

// Data-state badges: every badge has a text label and an icon shape, never colour alone.
export const BADGES = Object.freeze({
  plain: { label: '明文・僅本機', icon: 'unlock' },
  chip: { label: '晶片內明文（PS→PL DMA）', icon: 'chip' },
  cipher: { label: '密文', icon: 'lock' },
  tls: { label: '密文＋TLS 1.3', icon: 'shield' },
  public: { label: '公開中繼資料', icon: 'tag' },
  owner: { label: '明文結果・僅擁有者', icon: 'person' },
  key: { label: '私鑰・僅本機', icon: 'key' },
  pubkey: { label: '公鑰・可公開', icon: 'pubkey' },
});

export const SOURCES = Object.freeze({
  ui: '本畫面', client: 'client JSON', kernel: '板上核心計數', gateway: '閘道', cloud: '雲端畫面',
});

// In the PC rehearsal the kernel counters belong to the rehearsal container, not to the board.
export function sourceLabel(source, rehearsal = false) {
  return rehearsal && source === 'kernel' ? '演練容器核心計數' : SOURCES[source] || source;
}

// The labels that name a measurement: board figures, or PC rehearsal values (status.rehearsal).
export function timeLabels(rehearsal = false) {
  const tail = '每 25 ms 取樣，自加密程式啟動起算）；「資料量」為協定規格值或計算值。';
  return rehearsal
    ? { header: 'PC 演練時間', fact: 'PC 演練時間', legend: `此圖示標示演練容器核心計數的即時標記（PC 演練值，${tail}` }
    : { header: '實測時間', fact: '實測時間', legend: `此圖示標示板上核心計數的即時標記（${tail}` };
}

// The one fixed line under every error or STOP message: the gateway never retries and never signals the client.
export const NO_RETRY_LINE = '本系統不會自動重試，也不會中斷加密程式。';
// Shown instead of board figures when the gateway runs the PC rehearsal (status.rehearsal).
export const REHEARSAL_LABEL = 'PC 演練（PL 位元精確軟體模型）・非板上實測';
// The plain model name for the audience; the model identifier stays in the technical details.
export const MODEL_NAME = 'CDC 糖尿病模型';

// Short backend names for the top-bar chip; the full names stay on the form and in the chip's tooltip.
export const BACKEND_SHORT = Object.freeze({ pl: 'FPGA（PL）', spec: 'PL 軟體模型', cpu: 'ARM' });

// Joins copy fragments with one space wherever a Han character meets a Latin letter or digit ('保存於', 'KV260',
// '的記憶體' -> '保存於 KV260 的記憶體'), the spacing the rest of the copy uses ('於 KV260 板上').
export function joinMixed(...parts) {
  const han = /[㐀-鿿]/;
  const latin = /[A-Za-z0-9]/;
  let out = '';
  for (const part of parts) {
    const s = String(part);
    if (!s) continue;
    const a = out.slice(-1);
    const b = s[0];
    out += ((han.test(a) && latin.test(b)) || (latin.test(a) && han.test(b)) ? ' ' : '') + s;
  }
  return out;
}

// Where the gateway keeps a retained private record: the board, or the container of the PC rehearsal.
export function privateHost(rehearsal) {
  return rehearsal ? '演練容器' : 'KV260';
}

// '保存於 KV260 的記憶體暫存區（tmpfs），直到公開揭露或重新開機為止' (or 演練容器 in the PC rehearsal).
export function keptText(rehearsal = false) {
  return joinMixed('保存於', privateHost(rehearsal), '的記憶體暫存區（tmpfs），直到公開揭露或重新開機為止');
}

// The stage 13 marker of a run whose private record is kept: '私有紀錄暫存於 KV260'.
export function keptMarker(rehearsal = false) {
  return joinMixed('私有紀錄暫存於', privateHost(rehearsal));
}

// What happens to the private files of a form (the wiped event's retained_private, or the status before a run).
export function wipeText(retained, pl, rehearsal = false) {
  const files = pl ? 'in.csv 與 score.txt，並刪除呼叫許可' : 'in.csv 與 score.txt';
  return retained
    ? `暫存檔已以零覆寫並刪除；此次的私有紀錄（回答與分數）${keptText(rehearsal)}。`
    : `本機閘道已以零覆寫並刪除 ${files}。`;
}

// Stage groups that share one measured duration (client.cpp:306-318, EVENTS.md section 3).
export const TIME_GROUPS = Object.freeze([
  { id: 'submit', stages: [1, 2], source: 'ui' },
  { id: 'setup', stages: [3], source: 'gateway' },
  { id: 'cold', stages: [4, 5], source: 'client' },
  { id: 'prepare', stages: [6, 7, 8], source: 'client' },
  { id: 'roundtrip', stages: [9, 10, 11], source: 'client' },
  { id: 'decrypt', stages: [12], source: 'client' },
  { id: 'finish', stages: [13], source: 'gateway' },
]);

export function groupOfStage(n) {
  return TIME_GROUPS.find((g) => g.stages.includes(n)) || null;
}

export function groupLabel(group) {
  const s = group.stages;
  return s.length > 1 ? `第 ${s[0]}–${s[s.length - 1]} 階段合計` : `第 ${s[0]} 階段`;
}

// ------------------------------------------------------------------------------------------------ formatting
export function fmtInt(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—';
  const sign = n < 0 ? '-' : '';
  return sign + String(Math.trunc(Math.abs(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

export function fmtBytes(n) {
  return typeof n === 'number' && Number.isFinite(n) ? `${fmtInt(n)} B` : '—';
}

function sig3(v) {
  if (v >= 100) return String(Math.round(v));
  if (v >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

// Three significant digits in ns, µs, ms or s.
export function fmtDuration(ns) {
  if (typeof ns !== 'number' || !Number.isFinite(ns) || ns < 0) return '—';
  if (ns < 1000) return `${Math.round(ns)} ns`;
  const units = [[1e3, 'µs'], [1e6, 'ms'], [1e9, 's']];
  for (let i = 0; i < units.length; i++) {
    const [scale, unit] = units[i];
    const text = sig3(ns / scale);
    if (i < units.length - 1 && Number(text) >= 1000) continue;
    return `${text} ${unit}`;
  }
  return `${sig3(ns / 1e9)} s`;
}

const SUPERSCRIPT = { '-': '⁻', 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹' };

export function superscript(n) {
  return String(n).split('').map((c) => SUPERSCRIPT[c] || c).join('');
}

// Scientific notation for small errors: 0.002 -> '2×10⁻³', 3.14e-6 -> '3.1×10⁻⁶'.
export function fmtSci(x, digits = 2) {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  if (x === 0) return '0';
  let e = Math.floor(Math.log10(Math.abs(x)));
  let m = Number((x / 10 ** e).toPrecision(digits));
  if (Math.abs(m) >= 10) { m /= 10; e += 1; }
  if (Math.abs(m) < 1) { m *= 10; e -= 1; }
  return `${Number(m.toPrecision(digits))}×10${superscript(e)}`;
}

export function fmtPercent(rate, decimals = 1) {
  return typeof rate === 'number' && Number.isFinite(rate) ? `${(rate * 100).toFixed(decimals)}%` : '—';
}

export function fmtMHz(hz) {
  return typeof hz === 'number' && Number.isFinite(hz) && hz > 0 ? `${(hz / 1e6).toFixed(6)} MHz` : '—';
}

export function fmtSigned(x, decimals = 6) {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  const text = Math.abs(x).toFixed(decimals);
  return (x < 0 ? '−' : '+') + text;
}

export function hexPrefix(hex, n = 16) {
  return typeof hex === 'string' && /^[0-9a-f]{16,}$/.test(hex) ? hex.slice(0, n) : '—';
}

// {'61': 2, '62': 2} -> '+2／+2' in interrupt-number order.
export function fmtIrqDelta(delta) {
  if (!delta || typeof delta !== 'object') return '—';
  const keys = Object.keys(delta).sort((a, b) => Number(a) - Number(b) || (a < b ? -1 : 1));
  if (!keys.length) return '—';
  return keys.map((k) => (delta[k] >= 0 ? '+' : '') + fmtInt(delta[k])).join('／');
}

export function fmtClock(date) {
  const p = (v) => String(v).padStart(2, '0');
  return `${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`;
}

// ---------------------------------------------------------------------------------------------- byte counts
export function tlsRecords(frameBytes) {
  const lengths = [HEADER_BYTES + TLS13_EXPANSION];
  for (let left = frameBytes - HEADER_BYTES; left > 0; left -= TLS_RECORD_MAX) {
    lengths.push(Math.min(left, TLS_RECORD_MAX) + TLS13_EXPANSION);
  }
  return lengths;
}

export function tlsWireBytes(frameBytes) {
  return tlsRecords(frameBytes).reduce((sum, n) => sum + n + TLS_RECORD_HEADER, 0);
}

// [273, 16401 x 24] -> '1 × 273 B＋24 × 16,401 B'
export function recordsText(lengths) {
  const runs = [];
  for (const n of lengths) {
    const last = runs[runs.length - 1];
    if (last && last.n === n) last.count += 1; else runs.push({ n, count: 1 });
  }
  return runs.map((r) => `${r.count} × ${fmtInt(r.n)} B`).join('＋');
}

export function csvText(values, form) {
  const fields = (form && form.fields) || [];
  const row = fields.map((f) => {
    const v = values ? values[f.name] : undefined;
    if (typeof v !== 'number') return '';
    return f.kind === 'number' ? v.toFixed(f.decimals || 0) : String(Math.trunc(v));
  });
  const header = (form && form.model && form.model.csv_header) || fields.map((f) => f.name).join(',');
  return `${header}\n${row.join(',')}\n`;
}

export function frameSizes(status, run) {
  const sizes = { ...DEFAULT_SIZES, ...((status && status.sizes) || {}) };
  const ev = run && run.result && run.result.evidence;
  if (ev && ev.request_frame_bytes) sizes.request_frame_bytes = ev.request_frame_bytes;
  if (ev && ev.response_frame_bytes) sizes.response_frame_bytes = ev.response_frame_bytes;
  return sizes;
}

// --------------------------------------------------------------------------------------- stage definitions
// The 13 stages for one backend. opts: {sizes, features, csvBytes, wipedFiles, retainPrivate, rehearsal, noScore}.
// Each stage has a title, a short strip label, a one-sentence plain explanation for the audience (the caption under
// the table) and the full technical explanation (the stage screenshot). `bytes` is a protocol or computed size;
// `bytesText` marks a cell that is a count or a description rather than a byte number. noScore: the run ended without
// a model score (a STOP or another fatal error before the result), so stage 13 only wipes the files.
export function stageDefs(backend, opts = {}) {
  const sizes = { ...DEFAULT_SIZES, ...(opts.sizes || {}) };
  const features = opts.features || 4;
  const csvBytes = typeof opts.csvBytes === 'number' ? opts.csvBytes : null;
  const wiped = Array.isArray(opts.wipedFiles) ? opts.wipedFiles : null;
  const retained = opts.retainPrivate === true;
  const noScore = opts.noScore === true;
  const pl = backend === 'pl';
  const spec = backend === 'spec';
  const ct = sizes.public_key_bytes || DEFAULT_SIZES.public_key_bytes;
  const req = sizes.request_frame_bytes || HEADER_BYTES + features * ct;
  const res = sizes.response_frame_bytes || HEADER_BYTES + ct;
  const upRecords = tlsRecords(req);
  const downRecords = tlsRecords(res);
  const encCalls = Math.ceil(features / MAX_ENC_K);
  const encodeBytes = features * RING_N * 8;
  const cts = features * ct;
  const encWhere = pl ? 'FPGA' : spec ? 'PL 位元精確軟體模型' : 'ARM';
  const wipeFiles = pl ? 'in.csv 與 score.txt，並刪除呼叫許可' : 'in.csv 與 score.txt';
  const rehearsal = opts.rehearsal === true;
  const keep = retained ? `；此次的私有紀錄${keptText(rehearsal)}` : '';
  return [
    {
      n: 1, title: '表單輸入', short: '表單', place: 'ARM', where: '擁有者畫面', badges: ['plain'],
      bytes: `${features} 個數值`, bytesText: true, bytesNote: '自評健康狀況、高血壓、BMI 與年齡層',
      plain: '操作人員於擁有者畫面選擇或填寫四個虛構的回答。',
      explain: '操作人員於 KV260 本機畫面，或以序列主控台連接 KV260 的擁有者筆電，選擇或填寫四個虛構的回答。本畫面只'
        + '透過本機迴路介面（127.0.0.1）與閘道通訊（使用擁有者筆電時，再經序列主控台轉送至 KV260），表單內容不經過任何網路。',
    },
    {
      n: 2, title: '驗證與承諾', short: '承諾', place: 'ARM', where: '閘道', badges: ['plain', 'public'],
      bytes: '32 B', bytesNote: '承諾值為 32 B 的 SHA-256；32 B 的鹽值只存於本機',
      plain: '本機閘道檢查回答是否位於模型範圍內，並只公開一個無法反推回答的承諾值。',
      explain: '本機閘道檢查四個欄位是否位於模型可處理的範圍，再以 32 位元組的隨機鹽值計算 SHA-256 承諾值。'
        + '公開紀錄只保存承諾值，不保存答案。',
    },
    {
      n: 3, title: pl ? 'CSV 與呼叫許可' : 'CSV 暫存檔', short: pl ? 'CSV／許可' : 'CSV', place: 'ARM', where: '閘道',
      badges: ['plain'], bytes: csvBytes === null ? 'CSV 一列' : `CSV ${fmtBytes(csvBytes)}`, bytesText: true,
      bytesNote: pl ? 'CSV 為標頭加一列四個欄位；FPGA 呼叫許可只適用於此次參數' : 'CSV 為標頭加一列四個欄位',
      plain: pl ? '本機閘道將回答寫入僅限 root 存取的暫存檔，並產生只適用於此次的 FPGA 呼叫許可。'
        : '本機閘道將回答寫入僅限 root 存取的暫存檔，然後啟動加密程式。',
      explain: '本機閘道將一列 CSV 寫入僅限 root 存取的記憶體暫存目錄（tmpfs，目錄 0700、檔案 0600），'
        + (pl ? '並產生只適用於此次參數的 FPGA 呼叫許可，然後啟動加密程式。' : '然後啟動加密程式；此後端不需要 FPGA 呼叫許可。'),
    },
    {
      n: 4, title: pl ? '金鑰產生與公鑰載入 PL' : spec ? '金鑰產生與公鑰載入' : '金鑰產生', short: '金鑰',
      place: pl ? 'ARM→PL' : 'ARM', where: '加密程式', badges: ['key', 'pubkey'],
      bytes: fmtBytes(ct), bytesPrefix: '公鑰', bytesNote: pl ? `公鑰 ${fmtBytes(ct)} 經板上 DMA 載入 PL；私鑰不離開 ARM 記憶體`
        : `公鑰 ${fmtBytes(ct)}；私鑰不離開 ARM 記憶體`,
      plain: pl ? 'KV260 產生此次專用的金鑰：私鑰只留在 ARM 記憶體，公鑰載入 FPGA。'
        : spec ? 'KV260 產生此次專用的金鑰：私鑰只留在 ARM 記憶體，公鑰交給 PL 位元精確軟體模型。'
          : 'KV260 產生此次專用的金鑰：私鑰只留在 ARM 記憶體。',
      explain: pl
        ? `加密程式於 ARM 記憶體中產生此次專用的 CKKS 金鑰。私鑰不離開 ARM 記憶體；公鑰（${fmtBytes(ct)}）經板上 DMA `
          + '載入 FPGA，完成時 FPGA 發出一次 DMA 中斷（LOAD）。'
        : spec
          ? '加密程式於 ARM 記憶體中產生此次專用的 CKKS 金鑰，並將公鑰交給 PL 位元精確軟體模型；私鑰不離開 ARM 記憶體。'
          : '加密程式於 ARM 記憶體中產生此次專用的 CKKS 金鑰；私鑰不離開 ARM 記憶體。',
    },
    {
      n: 5, title: 'TLS 1.3 交握', short: '交握', place: '網路', where: '板端↔雲端', badges: ['public'],
      bytes: '約 3 KB', bytesText: true, bytesNote: '憑證、簽章與金鑰交換',
      plain: 'KV260 與雲端電腦互相驗證憑證，建立 TLS 1.3 加密連線。',
      explain: '加密程式與雲端電腦建立 TLS 1.3 連線。雙方互相驗證憑證（雙向認證），並各自比對預先釘選的憑證 '
        + 'SHA-256；任一項不符時，加密程式立即停止。',
    },
    {
      n: 6, title: '標準化', short: '標準化', place: 'ARM', where: '加密程式', badges: ['plain'],
      bytes: fmtBytes(features * 8), bytesNote: `${features} 個 double：z ＝（x − μ）／σ`,
      plain: 'KV260 將四個回答換算為模型使用的標準化數值。',
      explain: '加密程式於 ARM 上依模型的平均值 μ 與標準差 σ 計算 z ＝（x − μ）／σ；任一 |z| 超過 8 時，'
        + '加密程式拒絕此筆輸入。',
    },
    {
      n: 7, title: 'CKKS 編碼', short: '編碼', place: pl ? 'ARM→PL' : 'ARM', where: '加密程式', badges: [pl ? 'chip' : 'plain'],
      bytes: fmtBytes(encodeBytes), bytesNote: `${features} × ${fmtInt(RING_N)} 個 int64，另加 32 B 種子；Δ ＝ 2³⁰`,
      plain: pl ? 'KV260 將標準化數值編碼為可加密的多項式，經板上 DMA 送入 FPGA。'
        : 'KV260 將標準化數值編碼為可加密的多項式。',
      explain: `加密程式於 ARM 上以縮放因子 Δ ＝ 2³⁰ 將 ${features} 個標準化值編碼為明文多項式（${features} × `
        + `${fmtInt(RING_N)} 個 int64，共 ${fmtBytes(encodeBytes)}）`
        + (pl ? '，連同 32 B 的種子經板上 DMA 送入 FPGA。明文多項式只存在於 KV260 板上（ARM 記憶體、DMA 緩衝區與 FPGA），'
          + '不經過網路。' : '；明文多項式只存在於 KV260 的 ARM 記憶體，不經過網路。'),
    },
    {
      n: 8, title: pl ? `PL 公鑰加密（k＝${features}）` : spec ? '公鑰加密（軟體模型）' : '公鑰加密（ARM）', short: '加密',
      place: pl ? 'PL' : 'ARM', where: encWhere, badges: ['cipher'],
      bytes: fmtBytes(cts), bytesNote: `${features} 個密文，每個 ${fmtBytes(ct)}（2 個多項式 × 3 個 RNS 分量 × 4096 個字組）`,
      plain: pl ? `FPGA 以公鑰將多項式加密為 ${features} 個密文。`
        : spec ? `PL 位元精確軟體模型於 ARM 上以公鑰將多項式加密為 ${features} 個密文。`
          : `ARM 以公鑰將多項式加密為 ${features} 個密文。`,
      explain: pl
        ? `FPGA 以公鑰在 ${encCalls} 次呼叫中加密 ${features} 個明文多項式，輸出 ${features} 個密文（共 ${fmtBytes(cts)}），`
          + '完成時發出一次 DMA 中斷（ENC）。'
        : spec
          ? `PL 位元精確軟體模型於 ARM 上以公鑰加密 ${features} 個明文多項式，輸出與 FPGA 位元相同的 ${features} 個密文`
            + `（共 ${fmtBytes(cts)}）。`
          : `ARM 以公鑰加密 ${features} 個明文多項式，輸出 ${features} 個密文（共 ${fmtBytes(cts)}）。`,
    },
    {
      n: 9, title: '上傳密文（請求訊框）', short: '上傳', place: '網路', where: '板端→雲端', badges: ['tls'],
      bytes: fmtBytes(req), bytesWire: `線上 ${fmtBytes(tlsWireBytes(req))}`,
      bytesNote: `${HEADER_BYTES} B 公開標頭＋${fmtBytes(cts)} 密文；TLS 紀錄 ${recordsText(upRecords)}；線上位元組為依協定計算的值`,
      plain: `KV260 經 TLS 1.3 連線將 ${features} 個密文送往雲端電腦。`,
      explain: `加密程式以一個 EFHEW001 訊框送出 ${HEADER_BYTES} B 的公開標頭與 ${features} 個密文。TLS 1.3 將其分為 `
        + `${recordsText(upRecords)} 的紀錄，依協定計算，線上共 ${fmtBytes(tlsWireBytes(req))}。`,
    },
    {
      n: 10, title: '雲端加權加總（仿射運算）', short: '雲端運算', place: '雲端', where: '雲端電腦', badges: ['cipher'],
      bytes: `${features} 進 1 出`, bytesText: true, bytesNote: `${features} 個輸入密文，1 個輸出密文`,
      plain: '雲端電腦直接對密文進行加權加總；雲端沒有任何 CKKS 金鑰，無法解密。',
      explain: '雲端電腦以公開的模型權重 W 與 B，對每個 RNS 分量計算 Σ W_f · ct_f ＋ B，輸出 1 個密文。雲端電腦沒有任何 '
        + 'CKKS 金鑰，無法解密，也無法得知答案或分數；雲端電腦的 TLS 伺服器金鑰只保護傳輸，不能解開 CKKS 密文。'
        + '雲端運算時間記錄於雲端電腦的 server.log。',
    },
    {
      n: 11, title: '下載結果密文（回應訊框）', short: '下載', place: '網路', where: '雲端→板端', badges: ['tls'],
      bytes: fmtBytes(res), bytesWire: `線上 ${fmtBytes(tlsWireBytes(res))}`,
      bytesNote: `${HEADER_BYTES} B 標頭＋${fmtBytes(ct)} 密文；TLS 紀錄 ${recordsText(downRecords)}；線上位元組為依協定計算的值`,
      plain: '雲端電腦將 1 個結果密文傳回 KV260。',
      explain: `雲端電腦回傳 ${HEADER_BYTES} B 的標頭與 1 個密文（${fmtBytes(ct)}）。TLS 1.3 將其分為 `
        + `${recordsText(downRecords)} 的紀錄，依協定計算，線上共 ${fmtBytes(tlsWireBytes(res))}；加密程式檢查回應與請求的綁定。`,
    },
    {
      n: 12, title: '解密', short: '解密', place: 'ARM', where: '加密程式', badges: ['owner'],
      bytes: '8 B', bytesNote: '一個 double 的模型分數：c0 ＋ c1 · s',
      plain: 'KV260 以私鑰解密，得到模型分數。',
      explain: '加密程式於 ARM 上以私鑰計算 c0 ＋ c1 · s 並解碼，得到一個 8 B 的模型分數，寫入僅限 root 存取的暫存檔；'
        + '結束時，加密程式清除記憶體中的私鑰。',
    },
    {
      n: 13, title: '驗證、分級與清除', short: '分級／清除', place: 'ARM', where: '閘道', badges: ['owner'],
      bytes: `清除 ${wiped ? wiped.length : pl ? 3 : 2} 個檔案`, bytesText: true,
      bytesNote: (wiped ? `已以零覆寫並刪除：${wiped.join('、') || '無'}` : `以零覆寫並刪除 ${wipeFiles}`) + keep,
      plain: noScore
        ? (retained ? joinMixed('本機閘道未取得分數，因此未分級，只清除暫存檔；', keptMarker(rehearsal), '，直到揭露或重新開機。')
          : '本機閘道未取得分數，因此未分級，只以零覆寫並刪除暫存檔。')
        : retained ? joinMixed('本機閘道核對分數、決定風險等級並清除暫存檔；', keptMarker(rehearsal), '，直到揭露或重新開機。')
          : '本機閘道核對分數、決定風險等級，並清除暫存檔。',
      explain: noScore
        ? '本機閘道未取得模型分數，因此不重算參考分數，也不決定風險等級，只以零覆寫並刪除 CSV 與分數檔'
          + `${pl ? '，並刪除呼叫許可' : ''}${keep}。`
        : '本機閘道以明文模型重算參考分數，確認 CKKS 結果與參考分數相差不超過 2 × 10⁻³ 後，依等級邊界決定風險'
          + `等級，再以零覆寫並刪除 CSV 與分數檔${pl ? '，並刪除呼叫許可' : ''}${keep}。`,
    },
  ];
}

// The caption under the stage table: one stage, or a measured group of stages in the replay.
export function stageCaption(defs, stages) {
  const list = (stages || []).map((n) => defs[n - 1]).filter(Boolean);
  if (!list.length) return null;
  const first = list[0].n;
  const last = list[list.length - 1].n;
  const head = list.length > 1 ? `第 ${first}–${last} 階段` : `第 ${first} 階段・${list[0].title}`;
  return { head, text: list.map((d) => d.plain).join('') };
}

// ------------------------------------------------------------------------------------------- event reducer
export function createStore() {
  return { runs: new Map(), order: [], lastSeq: 0 };
}

function newRun(id) {
  return {
    id, kind: null, backend: null, model: null, model_sha256: null, commitment: null,
    accepted: false, owner: null, refused: null, spawned: null, exited: null, irq: null, result: null,
    wiped: null, fatal: null, errors: [], progressIrq: [], progressIo: [],
    t: {}, arrival: {}, firstSeq: null, lastSeq: null,
  };
}

// Applies one envelope; returns the run it touched (or null for an unusable envelope or an old seq).
export function applyEvent(store, env, localMs = null) {
  if (!env || typeof env !== 'object' || typeof env.seq !== 'number' || typeof env.run !== 'string') return null;
  if (env.seq <= store.lastSeq) return null;
  store.lastSeq = env.seq;
  let run = store.runs.get(env.run);
  if (!run) {
    run = newRun(env.run);
    store.runs.set(env.run, run);
    store.order.push(env.run);
    run.firstSeq = env.seq;
    run.arrival.first = localMs;
  }
  run.lastSeq = env.seq;
  const d = env.data && typeof env.data === 'object' ? env.data : {};
  const t = env.t_mono_ns;
  if (run.t.first === undefined) run.t.first = t;
  run.t.last = t;
  switch (env.type) {
    case 'form.accepted':
      if (env.class === 'public') {
        run.kind = d.kind || 'form';
        run.backend = d.backend || run.backend;
        run.model = d.model || run.model;
        run.model_sha256 = d.model_sha256 || run.model_sha256;
        run.commitment = d.commitment || run.commitment;
        run.accepted = true;
        if (run.t.accepted === undefined) run.t.accepted = t;
        if (run.arrival.accepted === undefined) run.arrival.accepted = localMs;
      } else {
        run.owner = { features: d.features, values: d.values, x: d.x, z: d.z, contributions: d.contributions, bias: d.bias };
        run.commitment = run.commitment || d.commitment || null;
        run.kind = run.kind || 'form';
        run.accepted = true;
        if (run.t.accepted === undefined) run.t.accepted = t;
        if (run.arrival.accepted === undefined) run.arrival.accepted = localMs;
      }
      break;
    case 'form.refused':
      run.kind = 'form';
      run.refused = { code: d.code, message: d.message, field: d.field, note: d.note };
      run.t.refused = t;
      break;
    case 'client.spawned':
      run.kind = run.kind || d.kind || null;
      run.backend = run.backend || d.backend || null;
      run.spawned = d;
      run.t.spawned = t;
      run.arrival.spawned = localMs;
      break;
    case 'progress.irq':
      run.progressIrq.push(d);
      break;
    case 'progress.io':
      run.progressIo.push(d);
      break;
    case 'client.exited':
      run.exited = d;
      run.t.exited = t;
      break;
    case 'irq.delta':
      run.irq = d;
      break;
    case 'result':
      run.result = d;
      run.t.result = t;
      break;
    case 'error':
      run.errors.push(d);
      if (d.fatal) { run.fatal = d; run.t.fatal = t; }
      break;
    case 'wiped':
      run.wiped = d;
      run.t.wiped = t;
      break;
    default:
      break;
  }
  return run;
}

export function isFormRun(run) {
  return !!run && (run.kind === 'form' || (run.kind === null && (run.accepted || !!run.refused)));
}

export function runFinished(run) {
  return !!run && (!!run.wiped || !!run.refused || (!!run.fatal && !run.spawned));
}

export function latestFormRun(store, { finishedOnly = false } = {}) {
  for (let i = store.order.length - 1; i >= 0; i--) {
    const run = store.runs.get(store.order[i]);
    if (isFormRun(run) && (!finishedOnly || runFinished(run))) return run;
  }
  return null;
}

function reportOf(run) {
  const r = run && run.exited && run.exited.report;
  return r && typeof r === 'object' ? r : null;
}

function trace0(run) {
  const r = reportOf(run);
  return r && Array.isArray(r.trace) && r.trace.length ? r.trace[0] : null;
}

// ------------------------------------------------------------------------------------------- live markers
// Markers from the progress sampler (board kernel counters, sampled every 25 ms): times since client.spawned.
export function liveMarks(run, sizes = DEFAULT_SIZES) {
  const m = {
    load: null, enc: null, firstWrite: null, uploadStarted: null, uploadDone: null, downloadStarted: null,
    downloadDone: null, wchar: null, rchar: null, downloaded: null, sampled: false, irqLines: {},
  };
  if (!run) return m;
  for (const e of run.progressIrq) {
    m.sampled = true;
    const delta = typeof e.delta === 'number' ? e.delta : typeof e.index === 'number' ? e.index : 0;
    const line = String(e.irq);
    m.irqLines[line] = Math.max(m.irqLines[line] || 0, delta);
    if (delta >= 1 && m.load === null) m.load = e.t_ns;
    if (delta >= 2 && m.enc === null) m.enc = e.t_ns;
  }
  let baseR = 0;
  for (const e of run.progressIo) {
    m.sampled = true;
    const w = e.wchar;
    const r = e.rchar;
    m.wchar = w;
    m.rchar = r;
    if (w > 0 && m.firstWrite === null) m.firstWrite = e.t_ns;
    if (w < UPLOAD_STARTED_BYTES) baseR = r;
    if (w >= UPLOAD_STARTED_BYTES && m.uploadStarted === null) m.uploadStarted = e.t_ns;
    if (w >= sizes.request_frame_bytes && m.uploadDone === null) m.uploadDone = e.t_ns;
    if (m.uploadStarted !== null) {
      const down = Math.max(0, r - baseR);
      m.downloaded = down;
      if (down >= TLS_RECORD_MAX && m.downloadStarted === null) m.downloadStarted = e.t_ns;
      if (down >= sizes.response_frame_bytes && m.downloadDone === null) m.downloadDone = e.t_ns;
    }
  }
  return m;
}

// ----------------------------------------------------------------------------------------- stage states
const FAIL_STAGE = [
  [/^NORMALIZATION_REFUSED$/, 6],
  [/^(OWNER_ENCRYPT|OWNER_ENCODE)/, 8],
  [/^OWNER_DECRYPT/, 12],
  [/^(OWNER_|PL_CAPS|PL_DEVICE|PL_LOAD)/, 4],
  [/^(PL_PERMIT_|PL_REQUIRES_|PL_ROOT_|CPU_SPEC_TAKES_|RUN_SETUP|CLIENT_SPAWN|WORKDIR_UNSAFE)/, 3],
  [/^(TLS_PEER_|TLS_HANDSHAKE|TLS_CA|TLS_LOCAL|LITERAL_IPV4|SOCKET_CONNECT)/, 5],
  [/^(WIRE_|RESPONSE_)/, 11],
  [/^(VERIFICATION_FAILED|PL_CLOCK_RANGE|CPU_SPEC_PL_FRAME_ACCOUNTING|IRQ_|CLIENT_REPORT_MISMATCH|SCORE_FILE|CLIENT_OUTPUT|MODEL_UNKNOWN)/, 13],
];

// The stage a fatal code stops at; `reached` is the number of stages already done.
export function failStage(code, reached) {
  if (typeof code !== 'string') return null;
  for (const [re, stage] of FAIL_STAGE) if (re.test(code)) return stage;
  if (/^(TLS_|SOCKET_)/.test(code)) return reached >= 8 ? Math.min(Math.max(reached + 1, 9), 11) : 5;
  return Math.min(Math.max(reached + 1, 3), 13);
}

// One of 'idle', 'wait', 'active', 'done', 'fail', 'skip' per stage (index 0 = stage 1).
export function stageStates(run, sizes = DEFAULT_SIZES) {
  const states = new Array(STAGE_COUNT).fill(run ? 'wait' : 'idle');
  if (!run) return states;
  if (run.refused) {
    states[0] = 'done';
    states[1] = 'fail';
    for (let i = 2; i < STAGE_COUNT; i++) states[i] = 'skip';
    return states;
  }
  const m = liveMarks(run, sizes);
  const report = reportOf(run);
  const clean = !!(run.exited && run.exited.returncode === 0 && report);
  let reached = 0;
  if (run.accepted) reached = 2;
  if (run.spawned) reached = 3;
  if (m.load !== null || m.firstWrite !== null) reached = Math.max(reached, 4);
  if (m.enc !== null || m.uploadStarted !== null) reached = Math.max(reached, 8);
  if (m.uploadDone !== null) reached = Math.max(reached, 9);
  if (m.downloadStarted !== null) reached = Math.max(reached, 10);
  if (m.downloadDone !== null) reached = Math.max(reached, 11);
  if (clean) reached = Math.max(reached, 12);
  if (run.wiped && run.result && run.result.passed && !run.fatal) reached = 13;
  if (run.fatal) {
    const f = Math.max(failStage(run.fatal.code, reached) || reached + 1, 1);
    for (let i = 0; i < STAGE_COUNT; i++) states[i] = i < f - 1 ? 'done' : i === f - 1 ? 'fail' : 'skip';
    // The gateway still wipes the private files after a STOP: stage 13 is done as far as the wipe goes.
    if (run.wiped && f < STAGE_COUNT) states[STAGE_COUNT - 1] = 'done';
    return states;
  }
  for (let i = 0; i < reached; i++) states[i] = 'done';
  if (reached < STAGE_COUNT) states[reached] = 'active';
  return states;
}

// ------------------------------------------------------------------------------------------------ timings
function positive(v) {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;
}

// Measured durations (ns) per time group plus totals. local: {submitToCommitNs} measured by this screen.
export function stageTimes(run, local = null) {
  const out = {};
  for (const g of TIME_GROUPS) out[g.id] = { ns: null, source: g.source, group: g };
  if (!run) return out;
  const t = run.t;
  const report = reportOf(run);
  const tr = trace0(run);
  if (local && positive(local.submitToCommitNs) !== null) {
    out.submit.ns = local.submitToCommitNs;
    if (local.recorded) out.submit.recorded = true;
  }
  if (typeof t.accepted === 'number' && typeof t.spawned === 'number') out.setup.ns = positive(t.spawned - t.accepted);
  if (report) out.cold.ns = positive(report.cold_context_and_TLS_ns);
  if (tr) {
    out.prepare.ns = positive(tr.prepare_ns);
    out.roundtrip.ns = positive(tr.TLS_roundtrip_and_validate_ns);
    out.decrypt.ns = positive(tr.decrypt_ns);
  }
  if (typeof t.exited === 'number' && typeof t.wiped === 'number') out.finish.ns = positive(t.wiped - t.exited);
  const wall = run.exited ? positive(run.exited.wall_ns) : null;
  const input = report ? positive(report.input_read_ns) : null;
  const elapsed = report ? positive(report.elapsed_ns) : null;
  out.extra = {
    wall_ns: wall,
    input_read_ns: input,
    elapsed_ns: elapsed,
    unitemized_ns: wall !== null && input !== null && out.cold.ns !== null && elapsed !== null
      ? positive(wall - input - out.cold.ns - elapsed) : null,
    gateway_total_ns: typeof t.accepted === 'number' && typeof t.wiped === 'number' ? positive(t.wiped - t.accepted) : null,
  };
  return out;
}

// ------------------------------------------------------------------------------------------------- replay
export const NICE_FACTORS = Object.freeze([1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000,
  100000, 200000, 500000, 1000000]);
export const REPLAY_RANGES = Object.freeze({
  all: { first: 1, last: 13, targetMs: 20000 },
  detail: { first: 6, last: 12, targetMs: 12000 },
});

// A replay of the measured durations, slowed by one factor N for the whole range; unmeasured groups are left out.
export function replayPlan(times, range = 'all') {
  const spec = REPLAY_RANGES[range] || REPLAY_RANGES.all;
  const segments = [];
  let realNs = 0;
  for (const g of TIME_GROUPS) {
    if (g.stages[0] < spec.first || g.stages[g.stages.length - 1] > spec.last) continue;
    const ns = times && times[g.id] ? times[g.id].ns : null;
    if (!(typeof ns === 'number' && ns > 0)) continue;
    segments.push({ group: g.id, stages: g.stages.slice(), realNs: ns, startMs: 0, durMs: 0 });
    realNs += ns;
  }
  if (!segments.length) return null;
  const maxFactor = (spec.targetMs * 1e6) / realNs;
  let factor = 1;
  for (const f of NICE_FACTORS) if (f <= maxFactor) factor = f;
  let at = 0;
  for (const s of segments) {
    s.startMs = at;
    s.durMs = (s.realNs * factor) / 1e6;
    at += s.durMs;
  }
  // The full replay leaves out the client's unitemized start-up and exit, which no stage owns.
  const extra = times && times.extra ? times.extra : {};
  const excludedNs = range === 'all' && typeof extra.unitemized_ns === 'number' && extra.unitemized_ns > 0
    ? extra.unitemized_ns : null;
  return {
    range, factor, segments, totalMs: at, realNs, excludedNs, banner: `依本次實測時間重播（×1/${fmtInt(factor)}）`,
    excludedText: excludedNs === null ? '' : `（不含程式啟動與結束 ${fmtDuration(excludedNs)}）`,
  };
}

// The segment active at replay time ms (null after the end).
export function replayAt(plan, ms) {
  if (!plan) return null;
  for (const s of plan.segments) if (ms >= s.startMs && ms < s.startMs + s.durMs) return s;
  return null;
}

// --------------------------------------------------------------------------------------------- level view
function levelItems(form) {
  return (form && form.levels && Array.isArray(form.levels.items)) ? form.levels.items : [];
}

export function levelView(run, form) {
  const levels = (form && form.levels) || {};
  const band = typeof levels.band_abs_logit === 'number' ? levels.band_abs_logit : 0.002;
  if (!run) return { state: 'idle' };
  if (run.refused) {
    return { state: 'refused', code: run.refused.code, message: run.refused.message, note: run.refused.note };
  }
  const r = run.result;
  if (r) {
    const errorText = `CKKS 與明文模型相差 ${fmtSci(r.abs_error)}（容許 ${fmtSci(r.max_abs_error)}）`;
    if (!r.passed) return { state: 'unverified', message: r.message, errorText };
    if (Array.isArray(r.boundary)) {
      return {
        state: 'boundary', title: r.boundary_text || `介於 L${r.boundary[0]}／L${r.boundary[1]}，不判定`,
        boundary: r.boundary.slice(), errorText,
        explain: `模型分數與等級邊界的距離不超過 ${fmtSci(band)}，本系統不判定等級。`,
      };
    }
    const item = levelItems(form).find((i) => i.level === r.level) || null;
    const caption = r.observed_caption || levels.observed_caption || '';
    let observed = '';
    if (typeof r.observed_rate === 'number') {
      observed = `${caption}：${fmtPercent(r.observed_rate)}`;
      if (item && typeof item.observed_rows === 'number' && typeof item.observed_positives === 'number') {
        observed += `（${fmtInt(item.observed_rows)} 筆中 ${fmtInt(item.observed_positives)} 筆）`;
      }
    }
    return {
      state: 'level', level: r.level, code: r.level_code || (item && item.code), name: r.level_name || (item && item.name),
      label: r.level_label || (item && item.label), observed, errorText,
    };
  }
  if (run.fatal) {
    return {
      state: 'error', code: run.fatal.code, message: run.fatal.message, stop: !!run.fatal.stop, line: NO_RETRY_LINE,
    };
  }
  return { state: 'running' };
}

export function levelScale(form) {
  return levelItems(form).map((i) => ({ level: i.level, code: i.code, name: i.name, label: i.label }));
}

// The level scale only where a level applies or may still apply.
export function showsScale(state) {
  return !['refused', 'error', 'unverified'].includes(state);
}

// '自評健康 好、高血壓 是、BMI 31.2、年齡 60–64 歲' for the answers of one run.
export function answersText(values, form) {
  if (!values || typeof values !== 'object') return '';
  const fields = (form && form.fields) || [];
  return fields.map((f) => {
    const v = values[f.name];
    let text;
    if (v === null || v === undefined || v === '') text = '—';
    else if (f.kind === 'choice') {
      const o = [...(f.options || []), ...(f.refused_options || [])].find((x) => x.value === v);
      text = o ? o.label : String(v);
    } else text = typeof v === 'number' ? v.toFixed(f.decimals || 0) : String(v);
    return `${f.short || f.label || f.name} ${text}`;
  }).join('、');
}

// ------------------------------------------------------------------------------------------ evidence view
export function evidenceView(run, status) {
  const out = {
    commitment: '—', request: '—', response: '—', deviceCalls: '—', irq: '—', irqPassed: null, plHz: '—',
    frames: '—', complete: false,
  };
  if (!run) return out;
  const sizes = frameSizes(status, run);
  const ev = run.result && run.result.evidence ? run.result.evidence : null;
  const report = reportOf(run);
  const tr = trace0(run);
  out.commitment = hexPrefix((ev && ev.commitment) || run.commitment);
  out.request = hexPrefix((ev && ev.request_sha256) || (tr && tr.request_sha256));
  out.response = hexPrefix((ev && ev.response_payload_sha256) || (tr && tr.response_payload_sha256));
  const calls = ev ? ev.device_calls : report ? report.device_calls : null;
  out.deviceCalls = typeof calls === 'number' ? fmtInt(calls) : '—';
  const delta = (ev && ev.irq_delta) || (run.irq && run.irq.delta);
  if (run.backend === 'pl') {
    out.irq = fmtIrqDelta(delta);
    out.irqPassed = run.irq ? run.irq.passed : null;
  } else if (run.irq || ev) {
    out.irq = '不適用';
  }
  const hz = ev ? ev.PL_hz : report ? report.PL_hz : null;
  out.plHz = run.backend === 'pl' ? fmtMHz(hz) : (run.irq || ev) ? '不適用' : '—';
  out.complete = out.request !== '—' && out.response !== '—';
  // Frame sizes only for a run whose request and response crossed the network.
  out.frames = out.complete ? `${fmtBytes(sizes.request_frame_bytes)}／${fmtBytes(sizes.response_frame_bytes)}` : '—';
  return out;
}

// ------------------------------------------------------------------------------------------ form checking
// Mirrors demo/edge/efgw/model.py Inference.refusal for the values the screen is about to send.
export function checkForm(values, form) {
  const fields = (form && form.fields) || [];
  const refusals = (form && form.refusals) || {};
  const result = (code, field) => ({ code, field, message: refusals[code] || code });
  const names = fields.map((f) => f.name).sort();
  const keys = values && typeof values === 'object' ? Object.keys(values).sort() : [];
  if (names.join(',') !== keys.join(',')) return result(form ? form.refuse_fields : 'FORM_FIELDS', null);
  for (const f of fields) {
    const v = values[f.name];
    if (f.kind === 'choice') {
      if (!Number.isInteger(v)) return result(f.refuse_invalid, f.name);
      const refused = (f.refused_options || []).find((o) => o.value === v);
      if (refused) return result(refused.code, f.name);
      if (!(f.options || []).some((o) => o.value === v)) return result(f.refuse_invalid, f.name);
    } else {
      if (typeof v !== 'number' || !Number.isFinite(v)) return result(f.refuse_invalid, f.name);
      if (Number(v.toFixed(f.decimals || 0)) !== v) return result(f.refuse_invalid, f.name);
      if (v < f.minimum) return result(f.refuse_below, f.name);
      if (v > f.maximum) return result(f.refuse_above, f.name);
    }
  }
  return null;
}

// The BMI text as the screen sends it: a number when the text is one, else the text (the gateway refuses it).
export function parseBmiText(text) {
  const s = String(text == null ? '' : text).trim().replace(/，/g, '.').replace(/．/g, '.');
  if (/^[0-9]+(\.[0-9]+)?$/.test(s)) return Number(s);
  return s;
}

// BMI from height (cm) and weight (kg), rounded half up to one decimal; null when the inputs are not plausible.
export function bmiFromMetric(heightCm, weightKg) {
  const h = Number(heightCm);
  const w = Number(weightKg);
  if (!Number.isFinite(h) || !Number.isFinite(w) || h < 50 || h > 250 || w < 10 || w > 400) return null;
  const bmi = w / ((h / 100) ** 2);
  return Math.round(bmi * 10 + 1e-9) / 10;
}

export function presetFor(values, presets) {
  if (!values || !Array.isArray(presets)) return null;
  for (const p of presets) {
    const f = p.form || {};
    const keys = Object.keys(f);
    if (keys.length && keys.every((k) => values[k] === f[k]) && Object.keys(values).length === keys.length) return p;
  }
  return null;
}

export const PRESET_TAGS = Object.freeze({ main: '主要', boundary: '邊界', refused: '拒絕', near_bound: '上限' });

// ------------------------------------------------------------------------------------------ screenshot mode
// ?shot=all, ?shot=stage=N, ?shot=stage&stage=N, ?shot=N or ?stage=N; ?run=<id> picks a run.
export function parseShot(search) {
  const p = new URLSearchParams(search || '');
  const runParam = p.get('run');
  const run = runParam && /^r[0-9a-f]{12}$/.test(runParam) ? runParam : null;
  const shot = p.get('shot');
  let stage = p.get('stage');
  if (shot === null && stage === null) return { mode: null, stage: null, run };
  if (shot !== null) {
    const m = /^(?:stage[=:\-]?)?(\d{1,2})$/.exec(shot);
    if (m) stage = m[1];
  }
  if (stage !== null && /^\d{1,2}$/.test(stage)) {
    const n = Number(stage);
    if (n >= 1 && n <= STAGE_COUNT) return { mode: 'stage', stage: n, run };
  }
  return { mode: 'all', stage: null, run };
}

// --------------------------------------------------------------------------------------------- history row
// local: what this page itself sent for the run ({values, backend}), used when the events do not carry them
// (a refused form has no owner values and no backend).
export function runSummary(run, form, local = null) {
  const presets = (form && form.presets) || [];
  const values = run && run.owner && run.owner.values ? run.owner.values : local && local.values ? local.values : null;
  const preset = presetFor(values, presets);
  const backend = run && run.backend ? run.backend : local && local.backend ? local.backend : null;
  const view = levelView(run, form);
  let outcome = '進行中';
  if (view.state === 'level') outcome = `${view.name || ''}（${view.label || ''}）`;
  else if (view.state === 'boundary') outcome = view.title;
  else if (view.state === 'refused') outcome = `本機拒絕（${view.code}）`;
  else if (view.state === 'unverified') outcome = '推論驗證失敗';
  else if (view.state === 'error') outcome = `未完成（${view.code}）`;
  const tr = trace0(run);
  return {
    run: run ? run.id : null,
    preset: preset ? preset.id : null,
    known: !!values,
    backend: backend ? (BACKEND_LABELS[backend] || backend) : '—',
    outcome,
    request: hexPrefix(tr && tr.request_sha256, 12),
    state: view.state,
  };
}
