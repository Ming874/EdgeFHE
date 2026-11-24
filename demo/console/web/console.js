'use strict';
/* EdgeFHE cloud console client (cloud view). Vanilla JavaScript, no external resources. Every text is set with
   textContent; the data come from GET /api/v1/state and the Server-Sent Events of GET /api/v1/events. State is kept
   in memory only. */
(function () {
  const API = '/api/v1';
  const S = {
    seq: 0, mode: '', run: {}, config: {}, status: {}, sessions: new Map(), order: [], probe: [], idleMw: null,
    packets: [], serverLog: [], model: null, titles: {}, statusText: {}, seal: {}, verify: {}, board: {},
    selected: null, follow: true, detail: null, notice: null, connected: false, loaded: false,
  };
  const QUICK = ['G2', 'G3', 'G4', 'G5', 'G6', 'G8'];
  const ALL_GATES = ['G0', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7', 'G8', 'G9', 'G10', 'G11'];
  const ICON = { PASS: '✓', FAIL: '✗', 'FAIL-PENDING': '…', INCOMPLETE: '…', BLOCKED: '⏸', SKIP: '–' };
  const SUITES = { '0x1301': 'TLS_AES_128_GCM_SHA256', '0x1302': 'TLS_AES_256_GCM_SHA384',
    '0x1303': 'TLS_CHACHA20_POLY1305_SHA256' };
  const FEATURE_ZH = { GenHlth: '自評健康狀況', HighBP: '高血壓', BMI: 'BMI', Age: '年齡層' };
  const CLASS_ZH = { form: '表單連線', handshake_only: '僅交握', other: '其他連線' };
  const CLOSE_ZH = { FIN: 'FIN', RST: 'RST（重設）', close_notify: 'close_notify 後 2 秒', inactive: '逾時無封包',
    capture_end: '擷取結束', port_reused: '連接埠重新使用' };
  const PROBE_WINDOW_S = 120;                       // live view: the last 120 s up to the newest sample
  const VIEW_MARGIN_S = 30;                         // replay and sealed view: 30 s before and after the sessions
  const AXIS_STEPS_S = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400];
  const $ = (id) => document.getElementById(id);
  const nf = new Intl.NumberFormat('en-US');

  // ------------------------------------------------------------------ DOM helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const SVGNS = 'http://www.w3.org/2000/svg';
  function sv(tag, attrs, text) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v !== null && v !== undefined) el.setAttribute(k, String(v));
    }
    if (text !== undefined) el.textContent = text;
    return el;
  }

  // ------------------------------------------------------------------ formatting
  const fmtInt = (n) => (n === null || n === undefined || !isFinite(n)) ? '—' : nf.format(n);
  const fmtB = (n) => fmtInt(n) + ' B';
  function fmtDur(s) {
    if (s === null || s === undefined || !isFinite(s)) return '—';
    const ms = s * 1000;
    if (Math.abs(ms) < 1) return ms.toFixed(3) + ' ms';
    if (Math.abs(ms) < 100) return ms.toFixed(1) + ' ms';
    if (Math.abs(ms) < 10000) return Math.round(ms) + ' ms';
    return s.toFixed(1) + ' s';
  }
  const hex16 = (x) => (typeof x === 'string' && x.length >= 16) ? x.slice(0, 16) : '—';
  function groupDigits(s) {                          // '-666458326697820288' -> '−666,458,326,697,820,288'
    const neg = s.startsWith('-');
    const d = neg ? s.slice(1) : s;
    return (neg ? '−' : '') + d.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function clock(t) {
    if (!t) return '—';
    const d = new Date(t * 1000);
    const p = (n, w) => String(n).padStart(w, '0');
    return p(d.getHours(), 2) + ':' + p(d.getMinutes(), 2) + ':' + p(d.getSeconds(), 2) + '.' + p(d.getMilliseconds(), 3);
  }
  function hms(t) {
    if (!t || !isFinite(t)) return '—';
    const d = new Date(t * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  const suiteName = (code) => SUITES[code] || code || '—';
  function gateBadge(st) {
    const cls = 'gate g-' + String(st || 'none').toLowerCase();
    return h('span', { class: cls }, (ICON[st] || '·') + ' ' + (S.statusText[st] || st || '未執行'));
  }
  function cmp(v, waitText) {
    if (v === true) return h('span', { class: 'cmp ok' }, '✓ 相同');    // the same as the hash from the capture
    if (v === false) return h('span', { class: 'cmp bad' }, '✗ 不同');
    return h('span', { class: 'cmp wait' }, waitText || '… 待比對');
  }
  function badge(kind, text) { return h('span', { class: 'badge b-' + kind }, text); }
  function src(text) { return h('span', { class: 'src' }, text); }

  // ------------------------------------------------------------------ state
  function applyState(st) {
    S.seq = st.seq || 0;
    S.rehearsal = !!st.rehearsal;
    S.port = st.port;                                // the port of the sessions shown (replay: from server.log)
    S.mode = st.mode; S.run = st.run || {}; S.config = st.config || {}; S.status = st.status || {};
    S.sessions = new Map(); S.order = [];
    for (const s of st.sessions || []) { S.sessions.set(s.id, s); S.order.push(s.id); }
    S.probe = (st.probe && st.probe.samples) || [];
    S.idleMw = st.probe ? st.probe.idle_mw : null;
    S.packets = st.packets || []; S.serverLog = st.server_log || [];
    S.model = st.model; S.titles = st.gate_titles || {}; S.statusText = st.status_text || {};
    S.seal = st.seal || {}; S.verify = st.verify || {}; S.board = st.board || {};
    S.loaded = true;
  }

  function handle(env) {
    if (env.seq) S.seq = Math.max(S.seq, env.seq);
    const d = env.data || {};
    switch (env.type) {
      case 'session':
        if (!S.sessions.has(d.id)) S.order.push(d.id);
        S.sessions.set(d.id, d);
        break;
      case 'status':
        for (const [k, v] of Object.entries(d)) S.status[k] = v;
        break;
      case 'probe': {                               // live samples (the last 180 s are kept)
        for (const row of d.samples || []) S.probe.push(row);
        const cut = (S.probe.length ? S.probe[S.probe.length - 1][0] : 0) - 180;
        while (S.probe.length && S.probe[0][0] < cut) S.probe.shift();
        S.idleMw = d.idle_mw;
        break;
      }
      case 'packet':
        S.packets.push(d);
        if (S.packets.length > 300) S.packets.splice(0, S.packets.length - 300);
        break;
      case 'server_log':
        S.serverLog.push(d);
        if (S.serverLog.length > 300) S.serverLog.splice(0, S.serverLog.length - 300);
        break;
      case 'seal': {
        const was = S.seal.state;
        S.seal = d;
        // after the seal the energy chart covers every session: reload the state, which then carries the probe
        // samples of the whole session span instead of the last 180 s
        if (d.state === 'sealed' && was !== 'sealed') { load(); return; }
        break;
      }
      case 'verify': S.verify = d; break;
      case 'board': S.board = d; break;
      case 'notice': showNotice(d.level, d.text); break;
      case 'resync': load(); return;
      default: break;
    }
    schedule();
  }

  let timer = null;
  function schedule() {
    if (timer) return;
    timer = setTimeout(() => { timer = null; render(); }, 150);
  }

  function showNotice(level, text) {
    const el = $('notice');
    el.textContent = text;
    el.className = 'notice' + (level === 'error' ? ' error' : '');
    el.hidden = !text;
  }

  async function load() {
    try {
      const r = await fetch(API + '/state', { cache: 'no-store' });
      applyState(await r.json());
      render();
      connect();
    } catch (e) {
      showNotice('error', '本畫面無法取得主控台狀態；請確認 evidence_console.py 仍在執行。');
      setTimeout(load, 3000);
    }
  }

  let es = null;
  function connect() {
    if (es) es.close();
    es = new EventSource(API + '/events?since=' + S.seq);
    es.onopen = () => { S.connected = true; schedule(); };
    es.onmessage = (ev) => {
      try { handle(JSON.parse(ev.data)); } catch (e) { /* a malformed event is skipped */ }
    };
    es.onerror = () => { S.connected = false; schedule(); };
  }

  async function post(path, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      const r = await fetch(API + path, {
        method: 'POST', cache: 'no-store', body: '{}',
        headers: { 'Content-Type': 'application/json', 'X-EdgeFHE-Console': '1' },
      });
      let body = {};
      try { body = await r.json(); } catch (e) { body = {}; }
      if (r.status >= 400) showNotice('error', body.message || ('主控台回應 HTTP ' + r.status));
      else showNotice('', '');
    } catch (e) {
      showNotice('error', '本畫面無法連線到主控台。');
    }
  }

  function current() {
    if (!S.order.length) return null;
    if (S.follow || !S.selected || !S.sessions.has(S.selected)) S.selected = S.order[S.order.length - 1];
    if (S.detailFor !== S.selected) { S.detail = null; S.detailFor = S.selected; }
    return S.sessions.get(S.selected);
  }

  function sessState(s) {
    const d = s.decode || {};
    if (s.state === 'open') return ['進行中', 'open'];
    switch (d.state) {
      case 'waiting': case 'queued': case 'pending': return [d.after_seal ? '等待封存後解碼' : '等待解碼', 'wait'];
      case 'running': return [(d.after_seal ? '封存後解碼中' : '解碼中') + '（第 ' + (d.attempts || 1) + ' 次）', 'wait'];
      case 'done': return [d.ok ? '已解碼' : '已解碼・有未通過項目', d.ok ? 'ok' : 'bad'];
      case 'deferred': return ['封存後解碼', 'wait'];
      case 'notls': return ['無 TLS 紀錄', 'wait'];
      case 'nokeys': return ['未啟用金鑰紀錄', 'wait'];
      default: return ['—', 'wait'];
    }
  }

  // ------------------------------------------------------------------ top bar and session list
  function chip(k, v, cls) { return h('li', { class: cls || null }, h('span', { class: 'k' }, k), v); }

  function renderChips() {
    const st = S.status || {};
    const cap = st.capture || {}, srv = st.server || {}, feed = st.feed || {}, pr = st.probe || {};
    const list = $('chips');
    list.replaceChildren();
    list.append(chip('模式', S.mode === 'replay' ? '重播（離線）' : '即時'));
    if (S.rehearsal) list.append(chip('資料來源', 'PC 演練（時間為 PC 演練值，非 KV260 實測）', 'warn'));
    const capText = { capturing: '擷取中', stopped: '已停止', file: '擷取檔', failed: '已中斷', idle: '尚未開始' }[cap.state] ||
      cap.state || '—';
    list.append(chip('擷取 dumpcap', capText + '・封包 ' + fmtInt(cap.packets) + '・遺失 ' +
      (cap.dropped === null || cap.dropped === undefined ? '—' : fmtInt(cap.dropped)),
    cap.state === 'failed' || cap.dropped > 0 ? 'bad' : null));
    const srvText = { starting: '啟動中', ready: '就緒', serving: '處理連線中', idle: '閒置（就緒）', stopped: '已停止',
      exited: '已結束', external: '由操作人員啟動', file: 'server.log' }[srv.state] || srv.state || '—';
    list.append(chip('雲端伺服器', srvText + (srv.keylog_warning ? '・金鑰紀錄（示範專用）已啟用' : '') +
      (srv.idle_deadlines ? '・閒置等待逾時 ' + fmtInt(srv.idle_deadlines) + ' 次（非錯誤）' : ''),
      srv.state === 'exited' && S.mode === 'live' && S.seal.state !== 'sealed' ? 'bad' : null));
    const feedText = { running: '執行中', stopped: '已停止', failed: '已中斷', ended: '已結束', file: '擷取檔' }[feed.state] ||
      feed.state || '—';
    list.append(chip('即時封包（無金鑰）', feedText + '・封包 ' + fmtInt(feed.packets), feed.state === 'failed' ? 'bad' : null));
    let prText = '未設定';
    if (pr.state === 'streaming') prText = (pr.port || '') + '・' + (pr.rate_hz || 0).toFixed(1) + ' Hz';
    else if (pr.state === 'file') prText = '紀錄檔 ' + (pr.port || '');
    else if (pr.state && pr.state !== 'off') prText = ({ open: '已開啟', silent: '沒有資料', unavailable: '無法開啟',
      lost: '連線中斷', stopped: '已停止', idle: '尚未開始' }[pr.state] || pr.state) + (pr.port ? '（' + pr.port + '）' : '');
    list.append(chip('能耗探針', prText, ['silent', 'unavailable', 'lost'].includes(pr.state) ? 'warn' : null));
    list.append(chip('事件串流', S.connected ? '已連線' : '重新連線中', S.connected ? null : 'warn'));
    $('foot-run').textContent = '證據資料夾：' + (S.run.dir || '—') + '　雲端連接埠：' + (S.port || S.config.port || '—') +
      '　擷取過濾條件：' + (S.config.capture_filter || '—');
  }

  function renderSessions() {
    const ol = $('session-list');
    ol.replaceChildren();
    for (const id of S.order) {
      const s = S.sessions.get(id);
      const [text, cls] = sessState(s);
      const sig = (s.signature || {}).classification;
      const b = h('button', { type: 'button', 'aria-current': id === S.selected ? 'true' : 'false',
        onclick: () => { S.selected = id; S.follow = false; render(); } },
      id + ' ' + (CLASS_ZH[sig] || ''), h('span', { class: 'st ' + cls }, text));
      ol.append(h('li', null, b));
    }
    const f = $('follow');
    f.setAttribute('aria-pressed', S.follow ? 'true' : 'false');
    const feed = (S.status || {}).feed || {};
    $('other-traffic').textContent = '其他封包（不屬於雲端連接埠）：' + fmtInt(feed.other_packets) +
      (feed.foreign ? '・來源不是設定的 KV260 位址：' + fmtInt(feed.foreign) : '');
  }

  // ------------------------------------------------------------------ 線上封包（無金鑰）
  function recTip(r, t0) { return r.label + '・' + fmtB(r.len) + '・+' + fmtDur(r.t - t0); }

  function renderWire(s) {
    const box = $('wire-body');
    box.replaceChildren();
    if (!s) {
      box.append(h('p', { class: 'empty' }, '目前尚無連線。KV260 送出表單時，本面板即時顯示每一筆 TLS 紀錄的長度與推論內容。'));
      return;
    }
    const t0 = s.t_syn || s.t_first;
    const recs = s.records || [];
    for (const dir of ['up', 'down']) {
      const tiles = h('div', { class: 'tiles ' + dir, role: 'list',
        'aria-label': dir === 'up' ? '上傳紀錄' : '下載紀錄' });
      for (const r of recs.filter((x) => x.dir === dir)) {
        const tip = recTip(r, t0);
        const end = dir === 'up' && r.group === 'limb' && r.k % 6 === 5;
        const tile = h('span', { class: 'rec g-' + r.group + (r.approx ? ' approx' : ' exact') + (end ? ' ct-end' : ''),
          role: 'listitem', tabindex: '0', title: tip, 'aria-label': tip });
        tile.addEventListener('mouseenter', () => { S.detail = tip; renderDetail(); });
        tile.addEventListener('focus', () => { S.detail = tip; renderDetail(); });
        tiles.append(tile);
      }
      box.append(h('div', { class: 'strip-row' },
        h('div', { class: 'strip-label' }, dir === 'up' ? '上傳（KV260 → 雲端）' : '下載（雲端 → KV260）'), tiles));
    }
    box.append(h('p', { class: 'detail', id: 'wire-detail', 'aria-live': 'off' }));
    const sig = s.signature || {};
    const c = s.counts || { up: {}, down: {} };
    const exact = s.records_source === 'capture';
    box.append(h('p', { class: 'sig' },
      '上傳：' + (sig.up_text && sig.up_text.length ? sig.up_text.join('、') : '—') + ' B，其中 ' + fmtInt(c.up.limbs) +
      ' 筆 RNS 分量（4 個密文 × c0、c1 × 3 個分量）', ' ', badge('cipher', '密文＋TLS 1.3')));
    box.append(h('p', { class: 'sig' },
      '下載：' + (sig.down_text && sig.down_text.length ? sig.down_text.join('、') : '—') + ' B，其中 ' + fmtInt(c.down.limbs) +
      ' 筆 RNS 分量（1 個結果密文）', ' ', badge('cipher', '密文＋TLS 1.3')));
    box.append(h('p', { class: 'sig' }, '紀錄大小特徵：' + (CLASS_ZH[sig.classification] || '—') +
      (exact ? '（已由解密確認，標示不再帶「≈」）' : '（由長度推論）')));
    box.append(h('div', { class: 'legend' },
      h('span', null, h('span', { class: 'rec g-hs exact' }), '交握'),
      h('span', null, h('span', { class: 'rec g-hdr exact' }), 'EFHE 標頭 256 B（公開中繼資料）'),
      h('span', null, h('span', { class: 'rec g-limb exact' }), 'RNS 分量 16,384 B（密文）'),
      h('span', null, h('span', { class: 'rec g-close exact' }), 'close_notify'),
      h('span', null, '虛線框＝由長度推論（≈）；實線框＝已解密確認')));
    const tbl = h('table', { class: 'packets' },
      h('thead', null, h('tr', null, h('th', null, '時間'), h('th', null, '方向'), h('th', { class: 'num' }, 'TCP'),
        h('th', null, '旗標'), h('th', null, 'TLS 紀錄'))));
    const body = h('tbody');
    const pk = S.packets.filter((p) => p.s === s.id).slice(-8).reverse();
    for (const p of pk) {
      body.append(h('tr', null, h('td', null, '+' + fmtDur(p.t - t0)), h('td', null, p.dir === 'up' ? '↑ 上傳' : '↓ 下載'),
        h('td', { class: 'num' }, fmtInt(p.tcp_len)), h('td', null, p.flags || ''),
        h('td', null, (p.recs || []).map((r) => fmtInt(r[0]) + (r[2] ? ' ≈' : ' ') + r[1]).join('；'))));
    }
    tbl.append(body);
    box.append(tbl);
    const last = recs.length ? recs[recs.length - 1] : null;
    if (!S.detail && last) S.detail = recTip(last, t0);
    renderDetail();
  }

  function renderDetail() {
    const el = $('wire-detail');
    if (el) el.textContent = S.detail || '';
  }

  // ------------------------------------------------------------------ 解開 TLS 之後
  // The model of a request frame in words, as on the owner screen ('CDC 糖尿病'): model_id is the SHA-256 of the
  // model file, so the demo model is recognised by that hash; any other model keeps the catalogue name.
  function modelName(frame) {
    const m = S.model || {};
    if (frame.model_id && m.sha256 && frame.model_id === m.sha256) return 'CDC 糖尿病模型';
    if (!frame.model || frame.model === 'unknown') return '模型目錄中沒有的模型';
    return frame.model;
  }

  function decodeLine(s) {
    const d = s.decode || {};
    if (s.state === 'open') return '連線進行中；本主控台在連線結束後解開此連線。';
    switch (d.state) {
      case 'waiting': case 'queued': case 'pending':
        return d.after_seal ? '等待封存後解碼：本主控台將解開已封存的擷取檔副本。' : '等待解碼：連線已結束，本主控台正在複製擷取檔。';
      case 'running': return (d.after_seal ? '封存後解碼中' : '解碼中') + '（第 ' + (d.attempts || 1) + ' 次嘗試）。';
      case 'done':
        if (d.after_seal) return '本主控台已於封存後解開此連線（解碼的對象是已封存的完整擷取檔副本）。';
        return '已解碼' + (d.after_close_s !== null && d.after_close_s !== undefined ? '（連線結束後 ' +
          d.after_close_s.toFixed(1) + ' 秒）' : '') + '。';
      case 'deferred': return '封存後解碼：本主控台在時限內未能解開此連線（' + (d.reason || '原因不明') +
        '）。封存後的完整驗證不受影響。';
      case 'nokeys': return '本次執行未啟用示範用金鑰紀錄，因此不解開 TLS。';
      case 'notls': return '此連線沒有任何 TLS 紀錄（例如伺服器尚未接聽時被拒絕的連線），因此沒有可解開的內容。';
      default: return '—';
    }
  }

  function renderTls(s) {
    const box = $('tls-body');
    box.replaceChildren();
    if (!s) { box.append(h('p', { class: 'empty' }, '目前尚無連線。')); return; }
    box.append(h('p', null, decodeLine(s)));
    const res = (s.decode || {}).result;
    if (!res) return;
    const req = (res.frames || []).find((f) => f.kind === 'request');
    const resp = (res.frames || []).find((f) => f.kind === 'response');
    if (req) {
      box.append(h('h3', null, '請求訊框 ', badge('public', '標頭：公開中繼資料'), ' ', badge('cipher', '酬載：密文')));
      box.append(h('dl', { class: 'kv' },
        h('dt', null, '訊框'), h('dd', null, 'EFHEW001・種類 1（請求）・序號 ' + req.sequence),
        h('dt', null, '模型'), h('dd', { title: 'model_id：模型檔的 SHA-256' }, modelName(req) + '（SHA-256 ',
          h('code', null, hex16(req.model_id)), '…）'),
        h('dt', null, '維度'), h('dd', null, '特徵 ' + req.features + '・列 ' + req.rows),
        h('dt', null, '酬載'), h('dd', null, fmtB(req.bytes - 256) + '＝' + req.features + ' 個 CKKS 密文 × c0、c1 × 3 個 RNS 分量 × 4096 個 uint32'),
        h('dt', null, '公鑰雜湊'), h('dd', null, h('code', null, hex16(req.key_id)), '…（KV260 為本筆表單新產生的金鑰；此處只有公鑰的雜湊值）'),
        h('dt', null, '世代'), h('dd', null, h('code', null, req.generation || '—'))));
    }
    if (resp) {
      box.append(h('h3', null, '回應訊框 ', badge('cipher', '酬載：密文')));
      box.append(h('dl', { class: 'kv' },
        h('dt', null, '訊框'), h('dd', null, 'EFHEW001・種類 2（回應）・序號 ' + resp.sequence),
        h('dt', null, '酬載'), h('dd', null, fmtB(resp.bytes - 256) + '＝1 個結果密文'),
        h('dt', null, '綁定'), h('dd', null, '回應標頭與請求標頭相同，只改種類與長度')));
    }
    const tls = res.tls || {};
    const g2 = ((res.gates || {}).G2 || {}).facts || {};
    box.append(h('h3', null, 'TLS 通道'));
    box.append(h('dl', { class: 'kv' },
      h('dt', null, '協定'), h('dd', null, tls.version === '0x0304' ? 'TLS 1.3（0x0304）' : (tls.version || '—')),
      h('dt', null, '加密套件'), h('dd', null, suiteName(tls.suite) + '（' + (tls.suite || '—') + '）'),
      h('dt', null, '伺服器名稱'), h('dd', null, tls.sni || '—'),
      h('dt', null, '雙向驗證'), h('dd', null, '伺服器憑證 ', h('code', null, hex16(g2.server_leaf_sha256)), '…、用戶端憑證 ',
        h('code', null, hex16(g2.client_leaf_sha256)), '… ', gateBadge(((res.gates || {}).G2 || {}).status)),
      h('dt', null, '工作階段票證'), h('dd', null, fmtInt(tls.tickets) + ' 張（伺服器不使用）')));
    const g6 = ((res.gates || {}).G6 || {}).facts || {};
    if (g6.histogram && g6.histogram.length) {
      box.append(h('h3', null, '請求密文的餘數分布'));
      const max = Math.max(...g6.histogram, 1);
      const hist = h('div', { class: 'hist', role: 'img',
        'aria-label': '請求密文所有餘數除以 q 之後的 32 區間直方圖；各區間高度相近代表分布均勻' });
      for (const v of g6.histogram) hist.append(h('span', { 'data-v': v }));
      box.append(hist);
      Array.from(hist.children).forEach((el, i) => { el.style.height = Math.max(3, Math.round(g6.histogram[i] / max * 100)) + '%'; });
      box.append(h('p', null, fmtInt(g6.uniform_limbs) + '／' + fmtInt(g6.limbs) +
        ' 個 RNS 分量通過均勻性檢定（卡方、KS、平均值與小餘數），非典型餘數 ' + fmtInt(g6.noncanonical_words) + ' 個。 ',
      gateBadge(((res.gates || {}).G6 || {}).status)));
    }
    box.append(h('p', { class: 'note' }, '解開 TLS 之後仍是 CKKS 密文：雲端沒有任何 CKKS 金鑰，無法解密，也無法得知表單答案或風險等級。'));
  }

  // ------------------------------------------------------------------ 三方雜湊
  // One row per source and one column per hash, so that the table fits one third of a 1920 px screen; each
  // comparison mark sits below its hash.
  function renderHash(s) {
    const box = $('hash-body');
    box.replaceChildren();
    if (!s) { box.append(h('p', { class: 'empty' }, '目前尚無連線。')); return; }
    const hs = s.hashes || {};
    const cap = hs.capture || {}, log = hs.server_log || {}, bd = hs.board || {};
    const cs = hs.capture_server || {}, cb = hs.capture_board || {};
    const sealed = S.seal.state === 'sealed';
    const boardMark = (same) => {
      if (bd.found === false) return h('span', { class: 'cmp bad' }, '✗ 板端公開紀錄無此筆請求');
      return cmp(same, sealed ? '… 待取回後比對' : '… 封存後比對');
    };
    const cell = (v, mark) => h('td', null, h('code', { class: 'hash' }, hex16(v)), mark || null);
    // the source name, with its file or method on a second line (a one-line label would break inside a word)
    const source = (name, detail) => h('th', null, name, h('span', { class: 'src-detail' }, detail));
    box.append(h('div', { class: 'scroll-x' }, h('table', { class: 'hash3' },
      h('colgroup', null, h('col', { class: 'c-src' }), h('col', { class: 'c-hash' }), h('col', { class: 'c-hash' })),
      h('thead', null, h('tr', null, h('th', null, '來源'), h('th', null, '請求雜湊'), h('th', null, '回應雜湊'))),
      h('tbody', null,
        h('tr', null, source('擷取檔', '解開 TLS 後計算'), cell(cap.request), cell(cap.response)),
        h('tr', null, source('雲端紀錄', 'server.log'),
          cell(log.request, cmp(cs.request, log.request ? null : '… 尚未出現')),
          cell(log.response, cmp(cs.response, log.response ? null : '… 尚未出現'))),
        h('tr', null, source('板端公開紀錄', sealed ? 'board_public.jsonl' : '封存後取回'),
          cell(bd.request, boardMark(cb.request)), cell(bd.response, boardMark(cb.response)))))));
    box.append(h('p', { class: 'note' }, '請求雜湊為 request_sha256，回應雜湊為 response_payload_sha256；表中列出前 16 個' +
      '十六進位字元。「✓ 相同」表示與擷取檔算出的雜湊相同。' + (sealed ? '' : '板端公開紀錄於封存後取回並比對；封存之前，' +
      '請與擁有者畫面上的雜湊比對。')));
    const big = cap.request || log.request;
    if (big) {
      box.append(h('p', { class: 'note' }, '請與擁有者畫面上的雜湊前 16 個十六進位字元比對：'));
      box.append(h('p', { class: 'big-hash' }, 'request ' + hex16(big)));
      const r2 = cap.response || log.response;
      if (r2) box.append(h('p', { class: 'big-hash' }, 'response ' + hex16(r2)));
    }
    const how = log.match === 'hash' ? '本主控台以 request_sha256 對應 server.log 的紀錄。' :
      (log.match === 'time' ? '本主控台暫以時間對應 server.log 的紀錄（≈），解碼後改以雜湊確認。' : '');
    if (how) box.append(h('p', { class: 'note' }, how));
    if (s.connection_stop) {
      box.append(h('p', { class: 'note' }, '雲端伺服器已結束此連線：' + s.connection_stop.code));
    }
  }

  // ------------------------------------------------------------------ 雲端運算
  function renderCloud(s) {
    const box = $('cloud-body');
    box.replaceChildren();
    const m = S.model || {};
    box.append(h('p', null, '雲端電腦以公開的模型權重直接對密文計算（在每個 RNS 分量上逐係數模 q 計算；B 只加在 c0 的常數項）：'));
    box.append(h('p', { class: 'formula', 'aria-label': '結果密文等於各特徵 f 的 W f 乘以密文 f 之總和，再加上 B' },
      '結果密文 = Σ', h('sub', null, 'f'), ' W', h('sub', null, 'f'), ' · 密文', h('sub', null, 'f'), ' + B'));
    box.append(h('p', null, '雲端沒有任何 CKKS 金鑰，無法解密，也無法得知表單答案或風險等級。'));
    const tb = h('tbody');
    (m.features || []).forEach((f, i) => {
      const zh = FEATURE_ZH[f] || f;
      tb.append(h('tr', null, h('td', null, zh === f ? f : zh + '（' + f + '）'),
        h('td', { class: 'num mono' }, fmtInt((m.weights_int || [])[i]))));
    });
    // bias_int_text: the exact integer (about -6.7e17 does not fit a JavaScript number)
    tb.append(h('tr', null, h('td', null, '偏差 B（× 2^60）'), h('td', { class: 'num mono' },
      typeof m.bias_int_text === 'string' && /^-?\d+$/.test(m.bias_int_text) ? groupDigits(m.bias_int_text) : '—')));
    box.append(h('table', null, h('thead', null, h('tr', null, h('th', null, '公開權重 W（× 2^30）'),
      h('th', { class: 'num' }, '整數值'))), tb));
    if (!s) { renderServerLog(box); return; }
    const sl = s.server_log || {};
    const res = (s.decode || {}).result || {};
    const g5 = (res.gates || {}).G5 || {};
    box.append(h('dl', { class: 'kv' },
      h('dt', null, '雲端運算時間'), h('dd', null, sl.cloud_ns !== undefined && sl.cloud_ns !== null ?
        fmtDur(sl.cloud_ns / 1e9) + ' ' : '— ', src('server.log 的 cloud_ns')),
      h('dt', null, '逐位元重算'), h('dd', null, g5.status ? gateBadge(g5.status) : '—', ' ',
        g5.facts && g5.facts.bit_exact ? '本主控台以擷取到的密文與公開的 W、B 重算回應，' + fmtInt(g5.facts.words) +
          ' 個 32 位元字組完全相同。' : (g5.status === 'FAIL' ? '重算結果與雲端回應不同。' : '解碼後重算。'))));
    renderServerLog(box);
  }

  // server.log lines as the cloud server wrote them (newest first)
  function logText(e, stops) {
    const d = e.data || {};
    const srv = (S.status || {}).server || {};
    const timeoutMs = Number((srv.launcher || {}).timeout_ms) || 30000;
    switch (e.kind) {
      case 'response':
        return '已寫出回應：序號 ' + d.sequence + '・request ' + hex16(d.request_sha256) + '…・response ' +
          hex16(d.response_payload_sha256) + '…・雲端計算 ' +
          (d.cloud_ns !== undefined && d.cloud_ns !== null ? fmtDur(d.cloud_ns / 1e9) : '—');
      case 'connection_stop': {
        const st = stops.get(e.n) || {};
        if (st.idle) {
          return '閒置：伺服器在 ' + Math.round(timeoutMs / 1000) + ' 秒內沒有收到新連線，重新等待（' + d.code + '，不是錯誤）';
        }
        return '伺服器結束連線' + (st.session ? ' ' + st.session : '') + '：' + (d.code || '');
      }
      case 'ready': return '伺服器就緒：連接埠 ' + d.port;
      case 'keylog_warning': return '示範專用的 TLS 金鑰紀錄已啟用（' + (d.path || 'tls_keys.log') + '）';
      case 'launcher_start':
        return '啟動程式：連線上限 ' + (d.connections || '—') + '・逾時 ' + (d.timeout_ms ? d.timeout_ms + ' ms' : '—') +
          '・金鑰紀錄 ' + (d.keylog === 'on' ? '啟用' : '未啟用');
      case 'launcher_exit': return '伺服器已結束（rc=' + (d.rc || '—') + '）';
      case 'stopped':
        return '伺服器停止：已計算 ' + fmtInt(d.evaluated) + ' 筆、寫出 ' + fmtInt(d.responses_written) + ' 筆、拒絕 ' +
          fmtInt(d.rejected_connections) + ' 條連線';
      default: return e.text || '';
    }
  }

  function renderServerLog(box) {
    const lines = S.serverLog.slice(-8).reverse();
    box.append(h('h3', null, '雲端紀錄 server.log ', src('雲端伺服器的輸出，時間取自雲端時鐘')));
    if (!lines.length) {
      box.append(h('p', { class: 'empty' }, '雲端伺服器尚未寫出任何紀錄。'));
      return;
    }
    const stops = new Map((((S.status || {}).server || {}).stops || []).map((x) => [x.n, x]));
    const ul = h('ul', { class: 'loglines' });
    for (const e of lines) {
      const st = e.kind === 'connection_stop' ? stops.get(e.n) || {} : {};
      ul.append(h('li', { class: st.idle ? 'idle' : null }, h('span', { class: 'mono t' }, clock(e.epoch)), ' ',
        logText(e, stops)));
    }
    box.append(ul);
    box.append(h('p', { class: 'note' }, '雲端紀錄只有雜湊值、運算時間與連線狀態；雲端伺服器沒有表單答案、任何 CKKS 金鑰或風險等級，因此無從寫入。'));
  }

  // ------------------------------------------------------------------ 時序
  function renderTiming(s) {
    const box = $('time-body');
    box.replaceChildren();
    if (!s) { box.append(h('p', { class: 'empty' }, '目前尚無連線。')); return; }
    const p = s.phases || {};
    const end = p.fin || p.close || s.t_last;
    const sl = s.server_log || {};
    const phases = [
      ['hs', '交握（TCP 與 TLS 1.3）', p.syn || s.t_first, p.client_finished],
      ['board', S.rehearsal ? '板端編碼與加密（PC 演練值；雲端只看到等待）' : '板端編碼與 PL 加密（雲端只看到等待）',
        p.client_finished, p.request_first],
      ['up', '上傳請求 393,472 B', p.request_first, p.request_last],
      ['cloud', '雲端運算與回應準備', p.request_last, p.response_first],
      ['down', '下載回應 98,560 B', p.response_first, p.response_last],
      ['close', '關閉連線', p.response_last, end],
    ].filter((x) => x[2] && x[3] && x[3] >= x[2]);
    const t0 = p.syn || s.t_first;
    const total = end && t0 ? end - t0 : null;
    box.append(h('p', null, '連線 ' + s.id + '：' + clock(t0) + ' 開始，總長 ' + fmtDur(total) + '・結束原因：' +
      (CLOSE_ZH[s.close_reason] || (s.state === 'open' ? '進行中' : '—')), ' ', src('擷取時間戳記')));
    if (phases.length && total > 0) {
      const W = 640, H = 40;
      const g = sv('svg', { class: 'chart', viewBox: '0 0 ' + W + ' ' + H, role: 'img',
        'aria-label': '連線各階段在網路上的時間比例' });
      for (const [cls, , a, b] of phases) {
        const x = (a - t0) / total * W;
        const w = Math.max((b - a) / total * W, 1.5);
        g.append(sv('rect', { class: 'ph ph-' + cls, x: x.toFixed(1), y: 4, width: w.toFixed(1), height: H - 8 }));
      }
      box.append(g);
    }
    const tb = h('tbody');
    for (const [cls, name, a, b] of phases) {
      tb.append(h('tr', null, h('td', null, h('span', { class: 'swatch sw-' + cls }), name),
        h('td', { class: 'num' }, fmtDur(b - a))));
    }
    if (sl.cloud_ns !== undefined && sl.cloud_ns !== null) {
      tb.append(h('tr', null, h('td', null, h('span', { class: 'swatch sw-cloud' }), '其中雲端計算 Σ W·ct + B ', src('server.log')),
        h('td', { class: 'num' }, fmtDur(sl.cloud_ns / 1e9))));
    }
    box.append(h('table', null, h('thead', null, h('tr', null, h('th', null, '階段'), h('th', { class: 'num' }, '時間'))), tb));
    box.append(h('p', { class: 'note' }, '時間取自雲端電腦的擷取時間戳記；板端的金鑰產生、PL 載入、編碼與加密之確切時間只在擁有者畫面上顯示。'));
  }

  // ------------------------------------------------------------------ 能耗
  // The chart range follows the run, not the wall clock: while a live run is not sealed, the last 120 s up to the
  // newest sample; in a replay and after the seal, every session with 30 s on each side, or the selected session
  // (after the operator picks one) with 30 s on each side.
  function energyView(s) {
    const first = S.probe[0][0], last = S.probe[S.probe.length - 1][0];
    if (S.mode === 'live' && S.seal.state !== 'sealed') {
      return { lo: last - PROBE_WINDOW_S, hi: last, live: true, text: '最近 120 秒（即時）' };
    }
    if (!S.follow && s && (s.t_syn || s.t_first)) {
      const a = s.t_syn || s.t_first, b = s.t_end || s.t_last || a;
      return { lo: a - VIEW_MARGIN_S, hi: b + VIEW_MARGIN_S, live: false, text: '連線 ' + s.id + ' 及其前後 30 秒' };
    }
    const spans = S.order.map((id) => S.sessions.get(id)).map((x) => [x.t_syn || x.t_first, x.t_end || x.t_last])
      .filter((p) => p[0] && p[1]);
    if (spans.length) {
      return { lo: Math.min(...spans.map((p) => p[0])) - VIEW_MARGIN_S,
        hi: Math.max(...spans.map((p) => p[1])) + VIEW_MARGIN_S, live: false,
        text: '全部連線，自第一條連線開始前 30 秒至最後一條連線結束後 30 秒' };
    }
    return { lo: first, hi: Math.max(last, first + 1), live: false, text: '整段探針紀錄' };
  }

  function renderEnergy(s) {
    const box = $('energy-body');
    box.replaceChildren();
    const pr = (S.status || {}).probe || {};
    if (!S.probe.length) {
      box.append(h('p', { class: 'empty' }, pr.state === 'off' || !pr.state ? '本次執行未設定能耗探針。' :
        '能耗探針目前沒有資料（' + (pr.message || pr.state) + '）。'));
      return;
    }
    const view = energyView(s);
    const lo = view.lo, hi = view.hi, span = Math.max(hi - lo, 1);
    const pts = S.probe.filter((r) => r[0] >= lo && r[0] <= hi);
    const ps = pts.map((r) => r[1]).concat(S.idleMw ? [S.idleMw] : []);
    let pmin = ps.length ? Math.min(...ps) : 0, pmax = ps.length ? Math.max(...ps) : 1000;
    const pad = Math.max((pmax - pmin) * 0.1, 50);
    pmin -= pad; pmax += pad;
    // one SVG unit = one CSS pixel, so that the 18 px chart labels are drawn at 18 px
    const W = Math.max(320, Math.floor(box.clientWidth || 640)), H = 250, L = 84, R = 10, B = 34, T = 26;
    const X = (t) => L + (t - lo) / span * (W - L - R);
    const Y = (p) => T + (1 - (p - pmin) / (pmax - pmin)) * (H - T - B);
    const g = sv('svg', { class: 'chart', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': 'KV260 12 V 輸入端功率，' + view.text + '（' + hms(lo) + ' 至 ' + hms(hi) +
        '），連線以 SYN 與 FIN 標示' });
    g.append(sv('line', { class: 'axis', x1: L, y1: H - B, x2: W - R, y2: H - B }));
    g.append(sv('line', { class: 'axis', x1: L, y1: T, x2: L, y2: H - B }));
    // clock-time ticks (host clock of the cloud PC, the same clock as the capture time stamps)
    const tick = AXIS_STEPS_S.find((x) => span / x <= Math.max(2, Math.floor((W - L - R) / 120))) ||
      AXIS_STEPS_S[AXIS_STEPS_S.length - 1];
    for (let t = Math.ceil(lo / tick) * tick; t <= hi; t += tick) {
      const x = X(t);
      g.append(sv('line', { class: 'tick', x1: x.toFixed(1), y1: H - B, x2: x.toFixed(1), y2: H - B + 5 }));
      if (x - L > 30 && W - R - x > 30) g.append(sv('text', { x: x.toFixed(1), y: H - 8, 'text-anchor': 'middle' }, hms(t)));
    }
    // session labels above the plot: the selected session always, the others only where they do not crowd a label
    const selSess = S.sessions.get(S.selected);
    const selX = selSess && (selSess.t_syn || selSess.t_first) ? X(Math.max(selSess.t_syn || selSess.t_first, lo)) : null;
    let lastLabel = -Infinity;
    for (const id of S.order) {
      const x = S.sessions.get(id);
      const a = (x.t_syn || x.t_first);
      const open = !x.t_end;
      const b = x.t_end || (S.probe.length ? S.probe[S.probe.length - 1][0] : a);   // open: up to the newest sample
      if (!a || b < lo || a > hi) continue;
      const sel = x.id === S.selected;
      const win = (x.energy && x.energy.window) || [a - 2, b + 0.5];
      const wa = Math.max(win[0], lo), wb = Math.min(win[1], hi);
      g.append(sv('rect', { class: 'win' + (sel ? ' sel' : ''), x: X(wa).toFixed(1), y: T,
        width: Math.max(X(wb) - X(wa), 1).toFixed(1), height: H - T - B }));
      if (a >= lo) g.append(sv('line', { class: 'syn' + (sel ? ' sel' : ''), x1: X(a).toFixed(1), y1: T, x2: X(a).toFixed(1), y2: H - B }));
      if (!open && b <= hi) g.append(sv('line', { class: 'fin', x1: X(b).toFixed(1), y1: T, x2: X(b).toFixed(1), y2: H - B }));
      const lx = X(Math.max(a, lo));
      if (sel || (lx - lastLabel >= 44 && (selX === null || Math.abs(lx - selX) >= 44))) {
        g.append(sv('text', { x: (lx + 3).toFixed(1), y: T - 6, class: sel ? 'sel' : null }, id));
        if (!sel) lastLabel = lx;
      }
    }
    if (S.idleMw) g.append(sv('line', { class: 'idle', x1: L, y1: Y(S.idleMw).toFixed(1), x2: W - R, y2: Y(S.idleMw).toFixed(1) }));
    const step = Math.max(1, Math.floor(pts.length / 900));
    if (pts.length) {
      g.append(sv('polyline', { class: 'power', points: pts.filter((_, i) => i % step === 0).map((r) =>
        X(r[0]).toFixed(1) + ',' + Y(r[1]).toFixed(1)).join(' ') }));
    }
    g.append(sv('text', { x: 4, y: T + 14 }, (pmax / 1000).toFixed(2) + ' W'));
    g.append(sv('text', { x: 4, y: H - B }, (pmin / 1000).toFixed(2) + ' W'));
    box.append(g);
    box.append(h('p', null, '圖表範圍：' + view.text + '，' + hms(lo) + ' 至 ' + hms(hi) + '（雲端電腦時鐘）' +
      (pts.length ? '' : '；探針紀錄未涵蓋此範圍')));
    const lastRow = S.probe[S.probe.length - 1];
    box.append(h('p', null, (view.live ? '目前功率 ' : '最後取樣功率 ') + (lastRow ? (lastRow[1] / 1000).toFixed(2) + ' W' : '—') +
      (view.live || !lastRow ? '' : '（' + hms(lastRow[0]) + '）') + '・閒置中位數 ' +
      (S.idleMw ? (S.idleMw / 1000).toFixed(2) + ' W' : '—') + ' ', src('INA228 探針，主機時間戳記')));
    const shift = Number(pr.shift_s) || 0;
    if (shift) {
      box.append(h('p', { class: 'note' }, '本次重播的探針紀錄取自另一次量測，時間已平移 ' + fmtInt(Math.round(shift)) +
        ' 秒以對齊擷取時間，僅供測試畫面使用，不是本次連線的實測能耗。'));
    }
    // live: the latest sessions; replay and sealed: the sessions inside the chart range
    const rows = S.order.map((id) => S.sessions.get(id)).filter((x) => x.energy && (view.live ||
      ((x.t_syn || x.t_first) <= hi && (x.t_end || hi) >= lo))).slice(-6);
    if (rows.length) {
      const tb = h('tbody');
      for (const x of rows) {
        const e = x.energy;
        tb.append(h('tr', null, h('td', null, x.id), h('td', { class: 'num' },
          e.duration_s === null || e.duration_s === undefined ? '—' : e.duration_s.toFixed(1) + ' s'),
          h('td', { class: 'num' }, e.delta_j === null || e.delta_j === undefined ? '—' : '≈ ' + e.delta_j.toFixed(2) + ' J'),
          h('td', null, e.covered ? '' : '探針資料不完整')));
      }
      box.append(h('table', null, h('thead', null, h('tr', null, h('th', null, '連線'), h('th', { class: 'num' }, '時間窗'),
        h('th', { class: 'num' }, 'ΔE'), h('th', null, ''))), tb));
    }
    box.append(h('p', { class: 'note' }, 'ΔE ≈ 時間窗 [SYN − 2 s, FIN + 0.5 s] 內 INA228 累積能量的差，再扣除閒置中位數功率。探針每秒約 10 筆，單筆表單接近量測雜訊，因此一律標示「≈」。'));
  }

  // ------------------------------------------------------------------ 驗證項目
  function factText(name, f) {
    if (!f) return '';
    switch (name) {
      case 'G2': return (f.version === '0x0304' ? 'TLS 1.3' : (f.version || '')) + '・' + suiteName(f.suite) + '・SNI ' +
        (f.sni || '—') + (f.native_crosscheck ? '・獨立解密器交叉檢查 ' + f.native_crosscheck : '');
      case 'G3': return '應用資料 ' + fmtB((f.app_bytes_up || 0) + (f.app_bytes_down || 0)) + '，其中 EFHEW001 訊框 ' +
        fmtB(f.accounted_bytes);
      case 'G4': return ['request_id', 'request_hash', 'session_binding', 'response_binding'].map((k) =>
        k + (f[k + '_ok'] === true ? ' ✓' : (f[k + '_ok'] === false ? ' ✗' : ' —'))).join('・');
      case 'G5': return f.bit_exact ? fmtInt(f.words) + ' 個字組逐位元相同' : (f.bit_exact === false ? '有 ' +
        fmtInt(f.mismatched_words) + ' 個字組不同' : '');
      case 'G6': return fmtInt(f.uniform_limbs) + '／' + fmtInt(f.limbs) + ' 個分量均勻・非典型餘數 ' +
        fmtInt(f.noncanonical_words);
      case 'G8': return f.client_close_notify ? '回應之後用戶端只送出 close_notify' : '';
      default: return '';
    }
  }

  function renderGates(s) {
    const box = $('gates-body');
    box.replaceChildren();
    const res = s && (s.decode || {}).result;
    if (res) {
      const cls = res.classification;
      const tb = h('tbody');
      for (const g of QUICK) {
        const d = (res.gates || {})[g];
        if (!d) continue;
        const na = cls === 'handshake_only' && g !== 'G2';
        const probs = ((d.facts || {}).problems || []).concat((d.facts || {}).incomplete || []);
        tb.append(h('tr', null, h('td', null, g), h('td', null, d.title || S.titles[g] || ''),
          h('td', null, na ? h('span', { class: 'gate' }, '– 不適用（僅交握）') : gateBadge(d.status)),
          h('td', null, na ? '' : factText(g, d.facts), probs.length ? h('div', { class: 'muted' }, probs.join('；')) : null)));
      }
      box.append(h('p', null, '連線 ' + s.id + ' 的即時檢查（本主控台以擷取副本與金鑰紀錄執行）：'));
      box.append(h('table', { class: 'gates' }, h('thead', null, h('tr', null, h('th', null, '項目'), h('th', null, '內容'),
        h('th', null, '結果'), h('th', null, '依據'))), tb));
    } else if (s && (s.decode || {}).state === 'notls') {
      box.append(h('p', { class: 'empty' }, '此連線沒有任何 TLS 紀錄，因此沒有可檢查的內容。'));
    } else {
      box.append(h('p', { class: 'empty' }, s ? '此連線解碼之後，本面板列出 G2 至 G8 的檢查結果。' : '目前尚無連線。'));
    }
    const v = S.verify || {};
    if (v.gates && Object.keys(v.gates).length) {
      box.append(h('h3', null, '封存後的完整驗證（efhe_verify.py）'));
      const tb = h('tbody');
      for (const g of ALL_GATES) {
        const d = v.gates[g];
        if (!d) continue;
        tb.append(h('tr', null, h('td', null, g), h('td', null, d.title || S.titles[g] || ''), h('td', null, gateBadge(d.status))));
      }
      box.append(h('table', { class: 'gates' }, tb));
      box.append(h('p', null, '整體判定：' + (v.verdict || '—') + (v.evidence_pass ? '（G0 至 G10 全部通過）' : '')));
    }
  }

  // ------------------------------------------------------------------ 封存
  function renderSeal() {
    const box = $('seal-body');
    box.replaceChildren();
    const st = S.seal || {};
    const v = S.verify || {};
    const openNow = S.order.map((id) => S.sessions.get(id)).filter((x) => x.state === 'open');
    const btnSeal = $('btn-seal'), btnVerify = $('btn-verify');
    if (S.mode === 'replay') {
      box.append(h('p', null, '重播模式：本畫面只顯示既有證據資料夾的內容，不封存也不驗證。'));
    } else {
      box.append(h('p', null, '封存會依序停止即時封包、dumpcap、雲端伺服器與能耗探針，再以 efhe_seal.py 計算 capture.pcapng 的 SHA-256；封存紀錄必須先於任何第三方使用金鑰紀錄。'));
    }
    const stateText = { none: '尚未封存', running: '封存中…', sealed: '已封存', failed: '封存失敗', unreadable: '封存紀錄無法讀取' };
    box.append(h('p', null, '狀態：' + (stateText[st.state] || st.state || '—') + (st.message ? '（' + st.message + '）' : '')));
    if (st.capture) {
      box.append(h('p', null, '擷取已停止：封包 ' + fmtInt(st.capture.received) + '・遺失 ' +
        (st.capture.dropped === null || st.capture.dropped === undefined ? '—' : fmtInt(st.capture.dropped))));
    }
    if (st.sha256) {
      box.append(h('p', null, 'EFHE_CAPTURE_SEALED sha256＝'));
      box.append(h('p', { class: 'seal-hash' }, st.sha256));
      box.append(h('p', { class: 'note' }, '請拍照或抄錄此雜湊值；任何人之後皆可據此確認擷取檔未遭更動。'));
    }
    if (st.state === 'sealed') {
      const b = S.board || {};
      box.append(h('p', null, b.state === 'joined' ? '板端公開紀錄已取回：' + fmtInt(b.rows) + ' 列、虛構揭露 ' +
        fmtInt(b.disclosures) + ' 筆。' : '下一步：以 board_files.py fetch-public 取回板端公開紀錄與虛構揭露，再按「驗證」。'));
    }
    if (v.state && v.state !== 'none') {
      const vt = { running: '驗證中…', done: '驗證完成', failed: '驗證失敗' }[v.state] || v.state;
      box.append(h('p', null, '驗證：' + vt + (v.verdict ? '・判定 ' + v.verdict : '') +
        (v.evidence_pass === true ? '・EFHE_EVIDENCE_PASS' : (v.evidence_pass === false ? '・未全部通過' : ''))));
      if (v.report_html) box.append(h('p', null, h('a', { href: '/evidence/report.html', target: '_blank', rel: 'noopener' }, '開啟 report.html')));
    }
    const lines = (st.lines || []).concat(v.lines || []).filter((x) => /^EFHE_/.test(x));
    if (lines.length) box.append(h('div', { class: 'lines' }, lines.join('\n')));
    btnSeal.disabled = S.mode !== 'live' || st.state === 'running' || st.state === 'sealed' || openNow.length > 0;
    btnSeal.title = openNow.length ? '連線進行中，請待連線結束後再封存。' : '';
    btnVerify.disabled = S.mode !== 'live' || st.state !== 'sealed' || v.state === 'running';
  }

  // ------------------------------------------------------------------ render
  function render() {
    if (!S.loaded) return;
    const s = current();
    renderChips();
    renderSessions();
    renderWire(s);
    renderTls(s);
    renderHash(s);
    renderCloud(s);
    renderTiming(s);
    renderEnergy(s);
    renderGates(s);
    renderSeal();
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('follow').addEventListener('click', () => { S.follow = true; S.detail = null; render(); });
    $('btn-seal').addEventListener('click', () => post('/seal',
      '封存將停止擷取、雲端伺服器與能耗探針，並計算擷取檔的 SHA-256。之後本次執行不再接受新的連線。是否繼續？'));
    $('btn-verify').addEventListener('click', () => post('/verify'));
    window.addEventListener('resize', schedule);       // the energy chart is drawn at the panel's pixel width
    load();
  });
})();
