// EdgeFHE owner screen: DOM wiring, Server-Sent Events, form submission, replay and screenshot mode.
// The page talks only to its own origin: the loopback gateway (127.0.0.1:8080) or the owner-laptop bridge
// (127.0.0.1:8780), with the paths of demo/edge/EVENTS.md section 5. Runs and the screen's own measurements live in
// this page's memory only; nothing is written to browser storage, and a reload starts again from the gateway's
// in-memory event log. All dynamic text is set with textContent.
import * as L from './lib.mjs';

const SVG_NS = 'http://www.w3.org/2000/svg';
const API = '/api/v1';
const FIELDS = ['GenHlth', 'HighBP', 'BMI', 'Age'];
const RECONNECT_MS = 1500;
const STATUS_RETRY_MS = 2000;
const TIMER_MS = 100;
const REPLAY_TICK_MS = 50;
const REPLAY_HOLD_MS = 1500;
const HISTORY_MAX = 12;
const SHOT_W = 1920;
const SHOT_H = 1080;

const el = {
  chipBackend: document.getElementById('chip-backend-value'),
  chipModel: document.getElementById('chip-model-value'),
  chipCloud: document.getElementById('chip-cloud-value'),
  chipLink: document.getElementById('chip-link'),
  chipLinkText: document.getElementById('chip-link-text'),
  bannerDev: document.getElementById('banner-dev'),
  bannerOffline: document.getElementById('banner-offline'),
  noticeFictional: document.getElementById('notice-fictional'),
  presets: document.getElementById('presets'),
  presetNote: document.getElementById('preset-note'),
  form: document.getElementById('form'),
  optGenHlth: document.getElementById('opt-GenHlth'),
  optHighBP: document.getElementById('opt-HighBP'),
  lgGenHlth: document.getElementById('lg-GenHlth'),
  lgHighBP: document.getElementById('lg-HighBP'),
  lgBMI: document.getElementById('lg-BMI'),
  lgAge: document.getElementById('lg-Age'),
  inBMI: document.getElementById('in-BMI'),
  unitBMI: document.getElementById('unit-BMI'),
  inAge: document.getElementById('in-Age'),
  msgGenHlth: document.getElementById('msg-GenHlth'),
  msgHighBP: document.getElementById('msg-HighBP'),
  msgBMI: document.getElementById('msg-BMI'),
  msgAge: document.getElementById('msg-Age'),
  qGenHlth: document.getElementById('q-GenHlth'),
  qHighBP: document.getElementById('q-HighBP'),
  qBMI: document.getElementById('q-BMI'),
  qAge: document.getElementById('q-Age'),
  bmiHeight: document.getElementById('bmi-height'),
  bmiWeight: document.getElementById('bmi-weight'),
  bmiApply: document.getElementById('bmi-apply'),
  bmiFormula: document.getElementById('bmi-formula'),
  backendOptions: document.getElementById('backend-options'),
  submit: document.getElementById('submit'),
  formMsg: document.getElementById('form-msg'),
  historySummary: document.getElementById('history-summary'),
  historyNote: document.getElementById('history-note'),
  historyList: document.getElementById('history-list'),
  timer: document.getElementById('timer'),
  replayAll: document.getElementById('replay-all'),
  replayDetail: document.getElementById('replay-detail'),
  replayStop: document.getElementById('replay-stop'),
  replayBanner: document.getElementById('replay-banner'),
  liveNote: document.getElementById('live-note'),
  liveNoteIcon: document.getElementById('live-note-icon'),
  liveNoteText: document.getElementById('live-note-text'),
  thTime: document.getElementById('th-time'),
  stageBody: document.getElementById('stage-body'),
  stageCaption: document.getElementById('stage-caption'),
  captionHead: document.getElementById('caption-head'),
  captionText: document.getElementById('caption-text'),
  timeSummary: document.getElementById('time-summary'),
  runWarnings: document.getElementById('run-warnings'),
  levelCard: document.getElementById('level-card'),
  levelKicker: document.getElementById('level-kicker'),
  levelMain: document.getElementById('level-main'),
  levelSub: document.getElementById('level-sub'),
  levelScale: document.getElementById('level-scale'),
  levelObserved: document.getElementById('level-observed'),
  levelObservedNote: document.getElementById('level-observed-note'),
  levelCheck: document.getElementById('level-check'),
  levelAnswers: document.getElementById('level-answers'),
  levelNotes: document.getElementById('level-notes'),
  custodyPl: document.getElementById('custody-pl'),
  ssCustodyPl: document.getElementById('ss-custody-pl'),
  techLogits: document.getElementById('tech-logits'),
  contribBody: document.getElementById('contrib-body'),
  techNote: document.getElementById('tech-note'),
  evRun: document.getElementById('ev-run'),
  evCommitment: document.getElementById('ev-commitment'),
  evRequest: document.getElementById('ev-request'),
  evResponse: document.getElementById('ev-response'),
  evCalls: document.getElementById('ev-calls'),
  evIrq: document.getElementById('ev-irq'),
  evPlhz: document.getElementById('ev-plhz'),
  evFrames: document.getElementById('ev-frames'),
  shotStage: document.getElementById('shot-stage'),
  ssNum: document.getElementById('ss-num'),
  ssTitle: document.getElementById('ss-title'),
  ssPlace: document.getElementById('ss-place'),
  ssBadges: document.getElementById('ss-badges'),
  ssBytes: document.getElementById('ss-bytes'),
  ssBytesNote: document.getElementById('ss-bytes-note'),
  ssTime: document.getElementById('ss-time'),
  ssTimeLabel: document.getElementById('ss-time-label'),
  ssLive: document.getElementById('ss-live'),
  ssExplain: document.getElementById('ss-explain'),
  ssStrip: document.getElementById('ss-strip'),
  ssRun: document.getElementById('ss-run'),
};

const state = {
  status: null,
  form: null,
  store: L.createStore(),
  follow: true,
  selected: null,
  local: new Map(),
  pending: null,
  source: null,
  initialSeq: 0,
  shot: L.parseShot(window.location.search),
  replay: null,
  timer: null,
  rows: [],
  renderQueued: false,
  presetId: null,
  statusTimer: null,
  techNotice: '',
  formRun: null,
  pickedStage: null,
  scrolledFor: null,
  linkText: '',
};

// ---------------------------------------------------------------------------------------------- DOM helpers
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    if (props.className) node.className = props.className;
    if (props.text !== undefined) node.textContent = props.text;
    if (props.attrs) for (const [k, v] of Object.entries(props.attrs)) node.setAttribute(k, v);
    if (props.dataset) for (const [k, v] of Object.entries(props.dataset)) node.dataset[k] = v;
  }
  for (const c of children) {
    if (c === null || c === undefined || c === '') continue;
    node.append(c);
  }
  return node;
}

function svgNode(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
  return node;
}

const ICONS = {
  unlock: ['M7 11V8a5 5 0 0 1 9.6-1.9', 'M5 11h14v10H5z'],
  lock: ['M7 11V8a5 5 0 0 1 10 0v3', 'M5 11h14v10H5z'],
  chip: ['M7 7h10v10H7z', 'M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4'],
  shield: ['M12 3l8 3v6c0 4.8-3.4 8-8 9-4.6-1-8-4.2-8-9V6z', 'M9 11.5l2.2 2.2L15.5 9.4'],
  tag: ['M3 12V3h9l9 9-9 9z', 'M7.5 7.5h.01'],
  person: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M4 21a8 8 0 0 1 16 0'],
  key: ['M10 14a4 4 0 1 1 0-8 4 4 0 0 1 0 8z', 'M13.5 11.5L21 19', 'M17.5 15.5l2-2', 'M19.5 17.5l1.5-1.5'],
  pubkey: ['M8 13a3 3 0 1 1 0-6 3 3 0 0 1 0 6z', 'M10.5 11.5L16 17', 'M14 15l1.5-1.5', 'M16 5.5a6 6 0 0 1 3.5 3.5',
    'M15 2.5a9.5 9.5 0 0 1 6.5 6.5'],
  pulse: ['M2 12h4l3-7 4 14 3-7h6'],
  check: ['M4 12.5l5 5L20 6.5'],
  play: ['M7 4l13 8-13 8z'],
  cross: ['M5 5l14 14M19 5L5 19'],
  dash: ['M5 12h14'],
  circle: ['M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16z'],
};

function icon(name, cls) {
  const node = svgNode('svg', { class: cls || 'icon', viewBox: '0 0 24 24', 'aria-hidden': 'true', focusable: 'false' });
  for (const d of ICONS[name] || []) node.appendChild(svgNode('path', { d }));
  return node;
}

function badge(kind) {
  const b = L.BADGES[kind];
  return h('span', { className: `badge badge-${kind}` }, icon(b.icon), h('span', { text: b.label }));
}

const STATE_ICON = { done: 'check', active: 'play', fail: 'cross', skip: 'dash', wait: 'circle', idle: 'circle' };
const STATE_TEXT = { done: '完成', active: '進行中', fail: '未通過', skip: '略過', wait: '等待', idle: '尚未開始' };

// ------------------------------------------------------------------------------------------------- network
function isDev() {
  return !!(state.status && state.status.dev_fixture);
}

// The PC rehearsal (gateway serve with the rehearsal switches): x86 timings, never board figures.
function isRehearsal() {
  return !!(state.status && state.status.rehearsal === true);
}

function retainPrivate() {
  return !!(state.status && state.status.retain_private === true);
}

function setLink(kind, text) {
  state.linkText = text;
  const open = kind === 'open';
  el.chipLink.dataset.state = kind;
  el.chipLink.dataset.dev = open && (isDev() || isRehearsal()) ? 'true' : 'false';
  el.chipLinkText.textContent = open && isDev() ? '模擬閘道・合成資料'
    : open && isRehearsal() ? 'PC 演練・非板上實測' : text;
  el.chipLink.title = open && isRehearsal() ? L.REHEARSAL_LABEL : '';
}

async function loadStatus() {
  try {
    const res = await fetch(`${API}/status`, { cache: 'no-store', credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const status = await res.json();
    if (!status || !status.form || !Array.isArray(status.form.fields)) throw new Error('status without form');
    const names = status.form.fields.map((f) => f.name).join(',');
    if (names !== FIELDS.join(',')) throw new Error(`unexpected form fields ${names}`);
    el.bannerOffline.hidden = true;
    return status;
  } catch (err) {
    el.bannerOffline.textContent = '本畫面無法連線至本機閘道（/api/v1/status）。請確認展示閘道正在執行；本畫面每 2 秒重試一次。';
    el.bannerOffline.hidden = false;
    setLink('closed', '閘道連線中斷');
    return null;
  }
}

async function start() {
  const status = await loadStatus();
  if (!status) {
    state.statusTimer = setTimeout(start, STATUS_RETRY_MS);
    return;
  }
  state.status = status;
  const first = state.form === null;
  state.form = status.form;
  if (first) setupForm();
  renderStatic();
  state.initialSeq = typeof status.last_seq === 'number' ? status.last_seq : 0;
  connect(0);
  scheduleRender();
}

function connect(since) {
  if (state.source) state.source.close();
  setLink('connecting', '閘道連線中');
  const source = new EventSource(`${API}/events?since=${since}`);
  state.source = source;
  source.onopen = () => setLink('open', '閘道連線正常');
  source.onmessage = (msg) => {
    let env;
    try { env = JSON.parse(msg.data); } catch (err) { return; }
    handleEnvelope(env);
  };
  source.onerror = () => {
    source.close();
    if (state.source !== source) return;
    state.source = null;
    setLink('closed', '閘道連線中斷，正在重新連線');
    setTimeout(reconnect, RECONNECT_MS);
  };
}

async function reconnect() {
  const status = await loadStatus();
  if (!status) {
    setTimeout(reconnect, STATUS_RETRY_MS);
    return;
  }
  state.status = status;
  if (typeof status.last_seq === 'number' && status.last_seq < state.store.lastSeq) {
    // A new gateway process numbers its events from 1 again: start over from its log.
    state.store = L.createStore();
    state.local.clear();
    state.initialSeq = status.last_seq;
    connect(0);
  } else {
    connect(state.store.lastSeq);
  }
  renderStatic();
  scheduleRender();
}

function handleEnvelope(env) {
  const known = env && state.store.runs.has(env.run);
  const run = L.applyEvent(state.store, env, performance.now());
  if (!run) return;
  if (!known && L.isFormRun(run) && run.firstSeq > state.initialSeq) {
    state.follow = true;
    if (state.replay) stopReplay();
  }
  if (env.type === 'client.spawned' && !L.isFormRun(run)) return;
  scheduleRender();
}

// ---------------------------------------------------------------------------------------------------- form
function fieldSchema(name) {
  return state.form.fields.find((f) => f.name === name);
}

function selectedBackend() {
  const c = el.backendOptions.querySelector('input[name="backend"]:checked');
  return c ? c.value : (state.status && state.status.default_backend) || 'pl';
}

function radioGroup(container, name, options) {
  const nodes = options.map((o) => {
    const input = h('input', { attrs: { type: 'radio', name, value: String(o.value) } });
    return h('label', o.title ? { attrs: { title: o.title } } : {}, input, h('span', { text: o.label }));
  });
  container.replaceChildren(...nodes);
}

function setRadio(name, value) {
  for (const input of el.form.querySelectorAll(`input[name="${name}"]`)) input.checked = input.value === String(value);
}

function setupForm() {
  const g = fieldSchema('GenHlth');
  const b = fieldSchema('HighBP');
  const bmi = fieldSchema('BMI');
  const age = fieldSchema('Age');
  el.lgGenHlth.textContent = g.label;
  el.lgHighBP.textContent = b.label;
  el.lgBMI.textContent = bmi.label;
  el.lgAge.textContent = age.label;
  el.qGenHlth.textContent = g.question || '';
  el.qHighBP.textContent = b.question || '';
  el.qBMI.textContent = bmi.question || '';
  el.qAge.textContent = age.question || '';
  el.unitBMI.textContent = bmi.unit || 'kg/m²';
  radioGroup(el.optGenHlth, 'GenHlth', g.options);
  radioGroup(el.optHighBP, 'HighBP', b.options);
  el.inBMI.setAttribute('placeholder', `${bmi.minimum.toFixed(bmi.decimals)}–${bmi.maximum.toFixed(bmi.decimals)}`);
  if (bmi.helper) el.bmiFormula.textContent = `${bmi.helper.formula}，${bmi.helper.rounding}。`;
  const ageOptions = [h('option', { text: '請選擇年齡層', attrs: { value: '' } })];
  for (const o of age.options) ageOptions.push(h('option', { text: o.label, attrs: { value: String(o.value) } }));
  for (const o of age.refused_options || []) {
    ageOptions.push(h('option', { text: `${o.label}（本模型不評估）`, attrs: { value: String(o.value) } }));
  }
  el.inAge.replaceChildren(...ageOptions);
  // The short names of the top-bar chip keep the three backends on one row; the full name is the tooltip.
  const backends = (state.status.backends || ['pl']).map((name) => ({
    value: name, label: L.BACKEND_SHORT[name] || name, title: L.BACKEND_LABELS[name] || name,
  }));
  radioGroup(el.backendOptions, 'backend', backends);
  setRadio('backend', state.status.default_backend || backends[0].value);
  const presets = state.form.presets || [];
  el.presets.replaceChildren(...presets.map((p) => {
    const button = h('button', { className: 'preset', attrs: { type: 'button', 'aria-pressed': 'false', title: p.label } },
      h('b', { text: p.id }), h('span', { text: L.PRESET_TAGS[p.role] || '範例' }));
    button.dataset.preset = p.id;
    button.addEventListener('click', () => applyPreset(p));
    return button;
  }));
  const notice = state.form.notice || [];
  if (notice[1]) el.noticeFictional.replaceChildren(h('strong', { text: '請只填寫虛構的回答。' }), notice[1]);
  // Under the level card: not a diagnosis, and a level rather than a probability. The training-data note goes to the
  // technical details.
  const notes = [notice[0], notice[2]].filter(Boolean).map((t) => h('li', { text: t }));
  if (notes.length) el.levelNotes.replaceChildren(...notes);
  state.techNotice = notice[3] || '';
  const main = presets.find((p) => p.role === 'main');
  if (main) applyPreset(main);
}

function applyValues(values) {
  setRadio('GenHlth', values.GenHlth);
  setRadio('HighBP', values.HighBP);
  el.inBMI.value = typeof values.BMI === 'number' ? values.BMI.toFixed(fieldSchema('BMI').decimals || 1)
    : String(values.BMI == null ? '' : values.BMI);
  el.inAge.value = values.Age == null ? '' : String(values.Age);
}

function applyPreset(p) {
  applyValues(p.form);
  onFormInput();
}

// The answers of a run as far as this page knows them: the owner event, or what this page itself sent.
function runValues(run) {
  if (!run) return null;
  if (run.owner && run.owner.values) return run.owner.values;
  const mine = state.local.get(run.id);
  return mine && mine.values ? mine.values : null;
}

function runBackend(run) {
  if (!run) return null;
  if (run.backend) return run.backend;
  const mine = state.local.get(run.id);
  return mine && mine.backend ? mine.backend : null;
}

// Whenever the displayed run changes (page load, a new run, a history entry, screenshot mode), the form shows that
// run's own answers, or an empty form when this page does not know them, so that the form and the result never
// belong to different runs. Edits after that stay until the displayed run changes again.
function syncForm(run) {
  if (!run) return;
  if (state.formRun === run.id) return;
  state.formRun = run.id;
  const values = runValues(run);
  if (values) {
    applyValues(values);
    const backend = runBackend(run);
    if (backend) setRadio('backend', backend);
    onFormInput();
  } else {
    applyValues({ GenHlth: null, HighBP: null, BMI: '', Age: null });
    onFormInput(true);
  }
}

function readForm() {
  const radio = (name) => {
    const c = el.form.querySelector(`input[name="${name}"]:checked`);
    return c ? Number(c.value) : null;
  };
  return {
    GenHlth: radio('GenHlth'),
    HighBP: radio('HighBP'),
    BMI: L.parseBmiText(el.inBMI.value),
    Age: el.inAge.value === '' ? null : Number(el.inAge.value),
  };
}

const FIELD_MSG = () => ({ GenHlth: el.msgGenHlth, HighBP: el.msgHighBP, BMI: el.msgBMI, Age: el.msgAge });

function onFormInput(quiet) {
  if (!state.form) return;
  const values = readForm();
  const preset = L.presetFor(values, state.form.presets);
  state.presetId = preset ? preset.id : null;
  for (const button of el.presets.querySelectorAll('button.preset')) {
    button.setAttribute('aria-pressed', button.dataset.preset === state.presetId ? 'true' : 'false');
  }
  const empty = FIELDS.every((name) => values[name] === null || values[name] === '');
  el.presetNote.textContent = preset ? `${preset.id}｜${preset.label}`
    : empty ? '請選擇一份示範表單，或自行填寫下列四個欄位。' : '自行填寫的表單（請只填寫虛構的回答）。';
  const check = L.checkForm(values, state.form);
  const msgs = FIELD_MSG();
  for (const name of FIELDS) {
    const show = quiet !== true && check && check.field === name && !(name === 'BMI' && values.BMI === '');
    msgs[name].textContent = show ? check.message : '';
    msgs[name].closest('.field').dataset.invalid = show ? 'true' : 'false';
  }
}

function applyBmiHelper() {
  const bmi = L.bmiFromMetric(el.bmiHeight.value, el.bmiWeight.value);
  if (bmi === null) {
    el.msgBMI.textContent = '請輸入合理的身高（公分）與體重（公斤）後再計算。';
    return;
  }
  el.inBMI.value = bmi.toFixed(1);
  onFormInput();
}

// The message under the submit button; `line` is the fixed no-retry line under a gateway error.
function setFormMessage(text, line) {
  el.formMsg.replaceChildren(...[text, line].filter(Boolean).map((t) => h('span', { text: t })));
  if (text) revealInColumn(el.formMsg);
}

// Above 1860 CSS px each column scrolls by itself: bring a message into view inside its column only, so that the
// page itself never scrolls.
function revealInColumn(node) {
  const col = node.closest('.col');
  if (!col || col.scrollHeight <= col.clientHeight) return;
  const r = node.getBoundingClientRect();
  const c = col.getBoundingClientRect();
  if (r.bottom > c.bottom) col.scrollTop += r.bottom - c.bottom + 8;
  else if (r.top < c.top) col.scrollTop -= c.top - r.top + 8;
}

async function submitForm(event) {
  event.preventDefault();
  if (!state.form || state.pending || activeRun()) return;
  const values = readForm();
  onFormInput();
  const backend = selectedBackend();
  const t0 = performance.now();
  state.pending = { t0 };
  state.follow = true;
  setFormMessage('');
  renderControls(displayedRun(), false);
  let res = null;
  let body = {};
  try {
    res = await fetch(`${API}/submit`, {
      method: 'POST', cache: 'no-store', credentials: 'same-origin', redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-EdgeFHE-Owner': '1' },
      body: JSON.stringify({ form: values, backend }),
    });
    body = await res.json().catch(() => ({}));
  } catch (err) {
    res = null;
  }
  const t1 = performance.now();
  state.pending = null;
  if (body && typeof body.run === 'string') state.local.set(body.run, { submitAt: t0, postMs: t1 - t0, values, backend });
  if (!res) {
    setFormMessage('本畫面無法連線至本機閘道，表單未送出。');
  } else if (res.status === 202) {
    setFormMessage('');
  } else if (res.status === 400 && body && body.run) {
    // The field message and the level card give the reason and the refusal note; one short line here.
    setFormMessage('本系統於本機拒絕此筆輸入。');
  } else {
    // The gateway did not start a run (busy, lock, configuration): its one-sentence message, then the fixed line.
    const text = body && body.message ? body.message : `本機閘道拒絕此請求（HTTP ${res.status}）。`;
    setFormMessage(text, res.status === 409 || res.status >= 500 ? L.NO_RETRY_LINE : '');
  }
  scheduleRender();
}

// ------------------------------------------------------------------------------------------------- choosing
function displayedRun() {
  const s = state.store;
  if (state.shot.mode) {
    if (state.shot.run && s.runs.has(state.shot.run)) return s.runs.get(state.shot.run);
    return L.latestFormRun(s, { finishedOnly: true }) || L.latestFormRun(s);
  }
  if (!state.follow && state.selected && s.runs.has(state.selected)) return s.runs.get(state.selected);
  return L.latestFormRun(s);
}

function activeRun() {
  const run = L.latestFormRun(state.store);
  return run && !L.runFinished(run) ? run : null;
}

function localMeasure(run) {
  if (!run) return null;
  const mine = state.local.get(run.id);
  if (mine && typeof run.arrival.accepted === 'number') {
    return { submitToCommitNs: Math.max(0, run.arrival.accepted - mine.submitAt) * 1e6 };
  }
  const dev = state.status && state.status.dev_fixture && state.status.dev_fixture.ui;
  const rec = dev && dev[run.id];
  if (rec && typeof rec.submit_to_commitment_ns === 'number') return { submitToCommitNs: rec.submit_to_commitment_ns, recorded: true };
  return null;
}

// ------------------------------------------------------------------------------------------------- rendering
function scheduleRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  window.requestAnimationFrame(() => {
    state.renderQueued = false;
    render();
  });
}

function renderStatic() {
  const st = state.status;
  if (!st) return;
  const m = st.model || {};
  const sha = String(m.sha256 || '');
  // The chip key already says 模型: the value is the plain name and the hash prefix; the full hash is the tooltip.
  el.chipModel.textContent = `CDC 糖尿病・${sha.slice(0, 8) || '—'}`;
  el.chipModel.title = sha ? `${L.MODEL_NAME}，SHA-256 ${sha}` : '';
  const c = st.cloud || {};
  const tls = `${c.protocol || 'TLS 1.3'}${c.mutual_authentication ? ' 雙向認證' : ''}`;
  el.chipCloud.textContent = `${c.host || '—'}:${c.port || '—'}・${tls}`;
  const dev = st.dev_fixture;
  const lines = [];
  if (dev && isRehearsal()) lines.push('合成資料・PC 演練・非實測');
  else if (dev) lines.push('開發用合成資料，非實測結果');
  else if (isRehearsal()) lines.push('PC 演練・非板上實測');
  el.bannerDev.replaceChildren(...lines.map((t) => h('span', { text: t })));
  el.bannerDev.title = dev ? dev.file || '' : '';
  el.bannerDev.hidden = !lines.length;
  // In the PC rehearsal every measured figure comes from the rehearsal container: the column header, the legend of
  // the live markers and the stage screenshot say so.
  const labels = L.timeLabels(isRehearsal());
  el.thTime.textContent = labels.header;
  el.liveNoteText.textContent = labels.legend;
  el.ssTimeLabel.textContent = labels.fact;
  el.historySummary.textContent = '執行紀錄（含回答與等級，存於閘道記憶體）';
  el.historyNote.textContent = '本清單取自本機閘道記憶體中的事件，重新整理本頁後仍會列出，本機閘道停止後才清除。'
    + (retainPrivate() ? `此外，本機閘道將每次推論的私有紀錄${L.keptText(isRehearsal())}。` : '');
  setLink(el.chipLink.dataset.state || 'connecting', state.linkText || '閘道連線中');
}

function render() {
  if (!state.form) return;
  const run = displayedRun();
  const active = !!run && !L.runFinished(run);
  const anyActive = active || !!activeRun();
  document.documentElement.classList.toggle('run-active', anyActive);
  if (anyActive && state.replay) stopReplay();
  const sizes = L.frameSizes(state.status, run);
  const backend = run && run.backend ? run.backend : selectedBackend();
  const features = ((state.status && state.status.model && state.status.model.features) || FIELDS).length;
  const csvBytes = run && run.owner ? new TextEncoder().encode(L.csvText(run.owner.values, state.form)).length : null;
  const defs = L.stageDefs(backend, {
    sizes, features, csvBytes, wipedFiles: run && run.wiped ? run.wiped.files : null, retainPrivate: retainedFor(run),
    rehearsal: isRehearsal(), noScore: !!(run && run.fatal && !run.result),
  });
  const ctx = {
    run, backend, sizes, defs,
    states: L.stageStates(run, sizes),
    times: L.stageTimes(run, localMeasure(run)),
    marks: L.liveMarks(run, sizes),
  };
  el.chipBackend.textContent = L.BACKEND_SHORT[backend] || backend;
  el.chipBackend.title = L.BACKEND_LABELS[backend] || backend;
  syncForm(run);
  renderStages(ctx);
  renderTimeSummary(ctx);
  renderCaption(ctx);
  renderWarnings(run);
  renderLevel(run);
  renderTech(run);
  renderCustody(backend);
  renderEvidence(run);
  renderHistory(run);
  renderControls(run, anyActive);
  renderTimer(run, active);
  if (state.shot.mode === 'stage') renderShotStage(ctx);
  if (state.replay) tickReplay();
  revealResult(run);
}

// Whether the private record of this run stays on the board: the wiped event says so for a finished run; before
// that, the gateway status gives the setting.
function retainedFor(run) {
  if (run && run.wiped && typeof run.wiped.retained_private === 'boolean') return run.wiped.retained_private;
  return retainPrivate();
}

// At 1860 CSS px or less the result card is above the stage table; when a new run ends while the card is out of view
// (the page was scrolled to follow the stages), bring the card back into view once.
function revealResult(run) {
  if (state.shot.mode || !run || !L.runFinished(run) || state.scrolledFor === run.id) return;
  state.scrolledFor = run.id;
  if (run.firstSeq <= state.initialSeq) return;
  const r = el.levelCard.getBoundingClientRect();
  if (r.top < 0 || r.bottom > window.innerHeight) el.levelCard.scrollIntoView({ block: 'nearest' });
}

// Live markers from the board's kernel counters (sampled every 25 ms). Each one is a moment measured from the
// client's start, not a duration; the counters are the client's cumulative read and write bytes. A marker is a list
// of parts: strings, and {at: '啟動後 1.50 s'}, which the table keeps on one line.
function liveParts(n, ctx, detail = false) {
  const { run, marks: m, states } = ctx;
  if (!run) return [];
  const st = states[n - 1];
  if (st === 'fail') {
    if (run.refused) return [`本機拒絕：${run.refused.code}`];
    if (run.fatal) return [`停止：${run.fatal.code}`];
  }
  if (run.refused) return [];
  const pl = run.backend === 'pl';
  const at = (t) => ({ at: `啟動後 ${L.fmtDuration(t)}` });
  const waiting = (text) => (st === 'active' ? [text] : []);
  switch (n) {
    case 4:
      if (!pl) return [];
      return m.load !== null ? ['LOAD 中斷・', at(m.load)] : waiting('等待 LOAD 中斷');
    case 5:
      return m.firstWrite !== null ? ['首次寫出・', at(m.firstWrite)] : waiting('等待 TLS 交握');
    case 8:
      if (!pl) return [];
      return m.enc !== null ? ['ENC 中斷・', at(m.enc)] : waiting('等待 ENC 中斷');
    case 9:
      if (m.uploadStarted === null) return waiting('等待上傳');
      if (m.uploadDone === null) return [`上傳中：累計寫出 ${L.fmtBytes(m.wchar)}（含交握）`];
      return ['上傳完成・', at(m.uploadDone), detail ? `（累計寫出 ${L.fmtBytes(m.wchar)}，含 TLS 交握）` : ''];
    case 10:
      return run.fatal ? [] : ['運算時間見雲端畫面'];
    case 11:
      if (m.downloaded === null || m.downloadStarted === null) return waiting('等待下載');
      if (m.downloadDone === null) return [`下載中：累計讀入 ${L.fmtBytes(m.downloaded)}`];
      return ['下載完成・', at(m.downloadDone), detail ? `（上傳開始後累計讀入 ${L.fmtBytes(m.downloaded)}）` : ''];
    case 13:
      if (!run.wiped) return [];
      if (run.fatal && !run.result) return ['暫存檔已覆寫並刪除，未分級'];
      return [retainedFor(run) ? L.keptMarker(isRehearsal()) : '暫存檔已覆寫並刪除'];
    default:
      return [];
  }
}

function liveText(n, ctx, detail = false) {
  return liveParts(n, ctx, detail).map((p) => (typeof p === 'string' ? p : p.at)).join('');
}

// Stages whose markers come from the board's kernel counters (DMA interrupts, wchar/rchar).
function kernelMarker(n, backend) {
  return n === 5 || n === 9 || n === 11 || ((n === 4 || n === 8) && backend === 'pl');
}

function timeCell(group, ctx) {
  const t = ctx.times[group.id];
  const td = h('td', { className: group.stages.length > 1 ? 'st-time st-group' : 'st-time' });
  td.rowSpan = group.stages.length;
  const measured = t && typeof t.ns === 'number';
  let source = L.SOURCES[group.source];
  if (group.id === 'submit' && t.recorded) source += '（紀錄）';
  td.append(h('b', { text: measured ? L.fmtDuration(t.ns) : '—' }), h('span', { className: 'src', text: source }));
  if (group.stages.length > 1) td.append(h('small', { text: L.groupLabel(group) }));
  if (group.id === 'submit' && !measured && ctx.run && L.runFinished(ctx.run) && !ctx.run.refused) {
    td.append(h('small', { text: '非本畫面送出，未量測' }));
  }
  return td;
}

function liveLine(n, ctx) {
  const parts = liveParts(n, ctx).filter(Boolean);
  if (!parts.length) return null;
  const text = liveText(n, ctx);
  const kernel = kernelMarker(n, ctx.backend) && !/^(本機拒絕|停止)/.test(text);
  return h('span', { className: kernel ? 'st-live st-kernel' : 'st-live' }, kernel ? icon('pulse', 'icon live-icon') : null,
    h('span', {}, ...parts.map((p) => (typeof p === 'string' ? p : h('span', { className: 'nowrap', text: p.at })))));
}

function bytesCell(d) {
  const td = h('td', { className: d.bytesText ? 'st-bytes st-bytes-text' : 'st-bytes', attrs: { title: d.bytesNote } });
  if (d.bytesPrefix) td.append(h('span', { className: 'bytes-prefix', text: d.bytesPrefix }));
  td.append(d.bytes);
  if (d.bytesWire) td.append(h('small', { text: d.bytesWire }));
  return td;
}

function renderStages(ctx) {
  const rows = [];
  for (const d of ctx.defs) {
    const st = ctx.states[d.n - 1];
    const tr = h('tr', {
      dataset: { stage: String(d.n), state: st },
      attrs: { tabindex: '0', 'aria-selected': state.pickedStage === d.n ? 'true' : 'false' },
    });
    if (state.pickedStage === d.n) tr.classList.add('is-picked');
    tr.append(h('td', { className: 'c-n' }, h('span', { className: 'st-num' }, icon(STATE_ICON[st], 'icon state-icon'),
      h('span', { text: String(d.n) }), h('span', { className: 'sr-only', text: STATE_TEXT[st] }))));
    tr.append(h('td', { attrs: { title: `${d.title}・${d.where}：按一下顯示說明` } },
      h('span', { className: 'st-title', text: d.title }), liveLine(d.n, ctx)));
    tr.append(h('td', { className: 'st-place', text: d.place }));
    tr.append(h('td', {}, h('span', { className: 'st-badges' }, ...d.badges.map(badge))));
    tr.append(bytesCell(d));
    const g = L.groupOfStage(d.n);
    if (g.stages[0] === d.n) tr.append(timeCell(g, ctx));
    tr.addEventListener('click', () => pickStage(d.n));
    tr.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        pickStage(d.n);
      }
    });
    rows.push(tr);
  }
  const focused = document.activeElement && document.activeElement.closest
    ? document.activeElement.closest('#stage-body tr') : null;
  el.stageBody.replaceChildren(...rows);
  state.rows = rows;
  state.ctx = ctx;
  if (focused) {
    const again = rows.find((tr) => tr.dataset.stage === focused.dataset.stage);
    if (again) again.focus({ preventScroll: true });
  }
}

function pickStage(n) {
  state.pickedStage = state.pickedStage === n ? null : n;
  scheduleRender();
}

// The plain-language caption under the table: the replayed group, the active stage of a running form, or a stage
// the presenter picked. While it is shown, the time summary of the finished run steps aside.
function captionStages(ctx) {
  const r = state.replay;
  if (r) {
    const seg = L.replayAt(r.plan, performance.now() - r.t0);
    return seg ? seg.stages : null;
  }
  if (ctx.run && !L.runFinished(ctx.run)) {
    const i = ctx.states.indexOf('active');
    if (i >= 0) return [i + 1];
  }
  return state.pickedStage ? [state.pickedStage] : null;
}

function renderCaption(ctx) {
  const cap = L.stageCaption(ctx.defs, captionStages(ctx));
  el.stageCaption.hidden = !cap;
  el.timeSummary.hidden = !!cap;
  if (!cap) return;
  el.captionHead.textContent = cap.head;
  el.captionText.textContent = cap.text;
}

function renderTimeSummary(ctx) {
  const { run, times } = ctx;
  const x = times.extra || {};
  if (!run || !run.exited || typeof x.wall_ns !== 'number') {
    el.timeSummary.textContent = run && run.wiped ? L.wipeText(retainedFor(run), run.backend === 'pl', isRehearsal()) : '';
    return;
  }
  const f = L.fmtDuration;
  const parts = [];
  if (typeof times.cold.ns === 'number') parts.push(`第 4–5 階段 ${f(times.cold.ns)}`);
  if (typeof x.elapsed_ns === 'number') parts.push(`第 6–12 階段與收尾 ${f(x.elapsed_ns)}`);
  if (typeof x.unitemized_ns === 'number' && typeof x.input_read_ns === 'number') {
    parts.push(`程式啟動、讀取 CSV 與結束 ${f(x.unitemized_ns + x.input_read_ns)}`);
  }
  // The gateway's own total (commitment to wipe) is the timer above the table.
  let text = `${isRehearsal() ? 'PC 演練值：' : ''}加密程式共執行 ${f(x.wall_ns)}（閘道量測）`;
  if (parts.length) text += `，其中${parts.join('、')}`;
  text += '。';
  // A kept private record is stated in full; otherwise the stage 13 marker says that the files were wiped.
  if (run.wiped && retainedFor(run)) text += L.wipeText(true, run.backend === 'pl', isRehearsal());
  el.timeSummary.textContent = text;
}

// Non-fatal error events (a watchdog report, a sampler or record problem): each message with its code, then the fixed
// line once, unless the level card already shows it for a fatal error of the same run.
function renderWarnings(run) {
  const items = [];
  if (run) {
    for (const e of run.errors) {
      if (!e.fatal && e.message) items.push(h('li', { text: `${e.message}（${e.code}）` }));
    }
    if (items.length && !run.fatal) items.push(h('li', { className: 'warn-line', text: L.NO_RETRY_LINE }));
  }
  el.runWarnings.replaceChildren(...items);
}

function renderLevel(run) {
  const v = L.levelView(run, state.form);
  el.levelCard.dataset.state = v.state;
  const set = (kicker, main, sub, check) => {
    el.levelKicker.textContent = kicker;
    el.levelMain.textContent = main;
    el.levelSub.textContent = sub || '';
    el.levelCheck.textContent = check || '';
  };
  let observed = false;
  switch (v.state) {
    case 'idle':
      set('尚未送出表單', '—', '請於左側選擇示範表單或填寫虛構的回答，再按「於本機加密並送出」。');
      break;
    case 'running':
      set('推論進行中', '加密與運算中', '本機正在加密並等待雲端電腦回傳密文；完成前本畫面不顯示風險等級。');
      break;
    case 'level':
      set('風險等級', v.name || '—', v.label || '', v.errorText);
      observed = !!v.observed;
      break;
    case 'boundary':
      set('風險等級', v.title, v.explain, v.errorText);
      break;
    case 'unverified':
      set('推論驗證失敗', '不顯示風險等級', v.message, v.errorText);
      break;
    case 'refused':
      set('本機拒絕此筆輸入', '不進行評估', v.message, v.note);
      break;
    case 'error':
      // The gateway's one-sentence message is the headline, the code is secondary, and one fixed line follows.
      set('此次推論未完成', v.message || v.code || '—', `${v.stop ? '停止代碼' : '錯誤代碼'}：${v.code || '—'}`, v.line);
      break;
    default:
      set('—', '—', '');
  }
  // The observed rate is a statistic of the test data, labelled as such, with the note that it is not the
  // respondent's own probability.
  if (observed) {
    el.levelObserved.replaceChildren(h('b', { className: 'stat-tag', text: '資料集統計' }), v.observed);
    el.levelObservedNote.textContent = (state.form.levels && state.form.levels.observed_note) || '';
  } else {
    el.levelObserved.replaceChildren();
    el.levelObservedNote.textContent = '';
  }
  renderAnswers(run, v.state);
  const nodes = [];
  if (L.showsScale(v.state)) {
    for (const item of L.levelScale(state.form)) {
      const li = h('li', {}, h('b', { text: item.code }), h('span', { text: item.label }));
      if (v.state === 'level' && v.level === item.level) li.classList.add('is-current');
      if (v.state === 'boundary' && v.boundary.includes(item.level)) li.classList.add('is-near');
      nodes.push(li);
      if (v.state === 'boundary' && item.level === v.boundary[0]) nodes.push(h('li', { className: 'is-edge', text: '邊界' }));
    }
  }
  el.levelScale.replaceChildren(...nodes);
}

// Which answers the card belongs to: the run's preset and answers, so a result is never read against other answers.
function renderAnswers(run, viewState) {
  if (!run || viewState === 'idle') {
    el.levelAnswers.textContent = '';
    return;
  }
  const values = runValues(run);
  if (!values) {
    el.levelAnswers.textContent = '本畫面未取得此結果的回答，因此表單已清空。';
    return;
  }
  const preset = L.presetFor(values, state.form.presets);
  const who = preset ? `示範表單 ${preset.id}` : '自行填寫';
  el.levelAnswers.textContent = `${viewState === 'running' ? '本次回答' : '此結果對應的回答'}（${who}）：`
    + `${L.answersText(values, state.form)}`;
}

function optionLabel(field, value) {
  if (field.kind !== 'choice') return typeof value === 'number' ? value.toFixed(field.decimals || 0) : String(value);
  const all = [...(field.options || []), ...(field.refused_options || [])];
  const o = all.find((x) => x.value === value);
  return o ? o.label : String(value);
}

function contributionBar(value, scale) {
  const node = svgNode('svg', { viewBox: '0 0 130 22', 'aria-hidden': 'true', focusable: 'false' });
  const mid = 65;
  const len = scale > 0 ? Math.min(1, Math.abs(value) / scale) * 60 : 0;
  node.appendChild(svgNode('rect', {
    class: value >= 0 ? 'bar-pos' : 'bar-neg', x: value >= 0 ? mid : mid - len, y: 4, width: Math.max(len, 0.5), height: 14,
  }));
  node.appendChild(svgNode('line', { class: 'axis', x1: mid, y1: 0, x2: mid, y2: 22 }));
  return node;
}

function renderTech(run) {
  if (!run || !run.result || !run.owner || !Array.isArray(run.owner.contributions)) {
    el.techLogits.replaceChildren();
    el.contribBody.replaceChildren();
    el.techNote.textContent = `完成推論後，本區顯示 CKKS 解密分數、明文參考分數與各特徵的貢獻。${state.techNotice || ''}`;
    return;
  }
  const r = run.result;
  const o = run.owner;
  const plain = r.plain_boundary ? `介於 L${r.plain_boundary[0]}／L${r.plain_boundary[1]}`
    : r.plain_level ? `第 ${r.plain_level} 級` : '—';
  const same = (r.plain_level === r.level) && JSON.stringify(r.plain_boundary) === JSON.stringify(r.boundary);
  const rows = [
    ['CKKS 解密分數（logit）', L.fmtSigned(r.logit_ckks, 6)],
    ['明文參考分數（logit）', L.fmtSigned(r.logit_plain, 6)],
    ['兩者差異', `${L.fmtSci(r.abs_error)}（容許 ${L.fmtSci(r.max_abs_error)}）`],
    ['明文參考分數的分級', `${plain}${r.passed ? (same ? '（與 CKKS 結果相同）' : '（與 CKKS 結果不同）') : ''}`],
    ['模型', `${L.MODEL_NAME}（SHA-256 ${String(run.model_sha256 || '').slice(0, 8) || '—'}…）`],
    ['模型識別碼（技術用）', run.model || '—'],
  ];
  el.techLogits.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', { text: v })]));
  const values = o.values || {};
  const scale = Math.max(Math.abs(o.bias || 0), ...o.contributions.map((c) => Math.abs(c)));
  const body = (o.features || FIELDS).map((name, i) => {
    const field = fieldSchema(name) || { short: name, kind: 'number' };
    return h('tr', {}, h('td', { text: field.short || field.label || name }), h('td', { text: optionLabel(field, values[name]) }),
      h('td', { text: L.fmtSigned(o.z[i], 3) }), h('td', { text: L.fmtSigned(o.contributions[i], 3) }),
      h('td', {}, contributionBar(o.contributions[i], scale)));
  });
  body.push(h('tr', {}, h('td', { text: '偏差 B' }), h('td', { text: '' }), h('td', { text: '' }),
    h('td', { text: L.fmtSigned(o.bias, 3) }), h('td', {}, contributionBar(o.bias, scale))));
  body.push(h('tr', { className: 'total' }, h('td', { text: '合計' }), h('td', { text: '明文參考分數' }), h('td', { text: '' }),
    h('td', { text: L.fmtSigned(r.logit_plain, 3) }), h('td', { text: '' })));
  el.contribBody.replaceChildren(...body);
  el.techNote.textContent = `模型分數 ＝ B ＋ Σ w·z，z ＝（x − μ）／σ；本區的數值只在本機畫面顯示。${state.techNotice || ''}`;
}

// The FPGA row of the custody panels follows the backend of the displayed (or selected) run.
const CUSTODY_PL = {
  pl: '公鑰；加密期間的明文多項式',
  spec: '不適用：此次由 PL 位元精確軟體模型加密',
  cpu: '不適用：此次由 ARM 加密',
};

function renderCustody(backend) {
  const text = CUSTODY_PL[backend] || CUSTODY_PL.pl;
  el.custodyPl.textContent = text;
  el.ssCustodyPl.textContent = text;
  el.custodyPl.closest('tr').dataset.na = backend === 'pl' ? 'false' : 'true';
}

function renderEvidence(run) {
  const v = L.evidenceView(run, state.status);
  el.evCommitment.textContent = v.commitment;
  el.evRequest.textContent = v.request;
  el.evResponse.textContent = v.response;
  el.evCalls.textContent = v.deviceCalls;
  el.evIrq.textContent = v.irq + (v.irqPassed === true ? '（相符）' : v.irqPassed === false ? '（不符）' : '');
  el.evPlhz.textContent = v.plHz;
  el.evFrames.textContent = v.frames;
  el.evRun.textContent = run ? run.id : '—';
}

function runTime(run) {
  if (run.firstSeq <= state.initialSeq || typeof run.arrival.first !== 'number') return '載入前';
  return L.fmtClock(new Date(performance.timeOrigin + run.arrival.first));
}

function renderHistory(current) {
  const runs = state.store.order.map((id) => state.store.runs.get(id)).filter(L.isFormRun).reverse().slice(0, HISTORY_MAX);
  const items = runs.map((run) => {
    const s = L.runSummary(run, state.form, state.local.get(run.id) || null);
    const text = [runTime(run), s.preset || (s.known ? '自填' : '回答未知'), s.backend, s.outcome].join('・');
    const button = h('button', { attrs: { type: 'button' } }, text,
      s.request !== '—' ? h('span', { className: 'mono', text: `・req ${s.request}` }) : null);
    button.setAttribute('aria-current', current && current.id === run.id ? 'true' : 'false');
    button.addEventListener('click', () => {
      state.follow = false;
      state.selected = run.id;
      stopReplay();
      scheduleRender();
    });
    return h('li', {}, button);
  });
  el.historyList.replaceChildren(...items);
}

function renderControls(run, active) {
  const busy = !!state.pending || !!activeRun() || !!(state.status && state.status.busy && !state.store.lastSeq);
  el.submit.disabled = busy || !state.form;
  el.submit.textContent = busy ? '推論進行中…' : '於本機加密並送出';
  for (const input of el.backendOptions.querySelectorAll('input')) input.disabled = busy;
  const done = !!run && L.runFinished(run) && !run.refused && !active && !!(run.exited && run.exited.report);
  const times = done ? L.stageTimes(run, localMeasure(run)) : null;
  el.replayAll.disabled = !done || !L.replayPlan(times, 'all');
  el.replayDetail.disabled = !done || !L.replayPlan(times, 'detail');
}

function renderTimer(run, active) {
  if (active) {
    if (!state.timer) state.timer = setInterval(updateTimer, TIMER_MS);
    updateTimer();
    return;
  }
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  if (!run) el.timer.textContent = '尚未開始';
  else if (run.refused) el.timer.textContent = '本機拒絕';
  else {
    const total = L.stageTimes(run).extra.gateway_total_ns;
    el.timer.textContent = typeof total === 'number'
      ? `全程 ${L.fmtDuration(total)}（閘道${isRehearsal() ? '・PC 演練' : ''}）` : '已結束';
  }
}

function updateTimer() {
  const run = displayedRun();
  if (!run) return;
  const mine = state.local.get(run.id);
  const startAt = mine ? mine.submitAt : run.arrival.first;
  if (typeof startAt !== 'number') return;
  el.timer.textContent = `已經過 ${((performance.now() - startAt) / 1000).toFixed(1)} s`;
}

function renderShotStage(ctx) {
  const n = state.shot.stage;
  const d = ctx.defs[n - 1];
  const g = L.groupOfStage(n);
  const t = ctx.times[g.id];
  el.ssNum.textContent = `第 ${n} 階段（共 ${L.STAGE_COUNT} 個階段）`;
  el.ssTitle.textContent = d.title;
  el.ssPlace.textContent = `位置：${d.place}・${d.where}`;
  el.ssBadges.replaceChildren(...d.badges.map(badge));
  el.ssBytes.textContent = (d.bytesPrefix ? `${d.bytesPrefix} ` : '') + d.bytes + (d.bytesWire ? `（${d.bytesWire}）` : '');
  el.ssBytesNote.textContent = d.bytesNote || '';
  const measured = t && typeof t.ns === 'number';
  const src = L.sourceLabel(g.source, isRehearsal()) + (t && t.recorded ? '（紀錄）' : '') + (isRehearsal() ? '・PC 演練值' : '');
  el.ssTime.textContent = `${measured ? L.fmtDuration(t.ns) : '—'}（${src}${g.stages.length > 1 ? `，${L.groupLabel(g)}` : ''}）`;
  const live = liveText(n, ctx, true);
  const fromKernel = live && kernelMarker(n, ctx.backend) && !/^(本機拒絕|停止)/.test(live);
  // The source leads the marker ('板上核心計數：LOAD 中斷・啟動後 1.50 s'), so a detail in brackets never nests.
  el.ssLive.textContent = live ? `${fromKernel ? `${L.sourceLabel('kernel', isRehearsal())}：` : ''}${live}` : '—';
  el.ssExplain.textContent = d.explain;
  el.ssStrip.replaceChildren(...ctx.defs.map((x) => {
    const li = h('li', { attrs: { title: x.title } }, h('b', { text: String(x.n) }), h('span', { text: x.short || x.title }));
    if (x.n === n) li.classList.add('is-current');
    return li;
  }));
  const run = ctx.run;
  el.ssRun.textContent = (run ? `執行 ${run.id}・${L.BACKEND_LABELS[ctx.backend] || ctx.backend}` : '尚無已完成的表單')
    + (isDev() ? '・開發用合成資料，非實測' : '') + (isRehearsal() ? `・${L.REHEARSAL_LABEL}` : '');
}

// ---------------------------------------------------------------------------------------------------- replay
function startReplay(range) {
  const run = displayedRun();
  if (!run || !L.runFinished(run) || run.refused || !(run.exited && run.exited.report)) return;
  const plan = L.replayPlan(L.stageTimes(run, localMeasure(run)), range);
  if (!plan) return;
  stopReplay();
  state.replay = { plan, t0: performance.now(), run: run.id, timer: setInterval(tickReplay, REPLAY_TICK_MS) };
  el.replayStop.hidden = false;
  tickReplay();
}

function segmentLabel(seg) {
  const s = seg.stages;
  return s.length > 1 ? `第 ${s[0]}–${s[s.length - 1]} 階段` : `第 ${s[0]} 階段`;
}

function tickReplay() {
  const r = state.replay;
  if (!r) return;
  const ms = performance.now() - r.t0;
  const seg = L.replayAt(r.plan, ms);
  const on = new Set(seg ? seg.stages : []);
  const past = new Set();
  for (const s of r.plan.segments) if (s.startMs + s.durMs <= ms) for (const n of s.stages) past.add(n);
  for (const tr of state.rows) {
    const n = Number(tr.dataset.stage);
    tr.classList.toggle('rp-on', on.has(n));
    tr.classList.toggle('rp-off', !on.has(n) && !past.has(n));
  }
  const realNs = (Math.min(ms, r.plan.totalMs) / r.plan.factor) * 1e6;
  const skipped = L.TIME_GROUPS.filter((g) => !r.plan.segments.some((s) => s.group === g.id)
    && g.stages[0] >= L.REPLAY_RANGES[r.plan.range].first && g.stages[g.stages.length - 1] <= L.REPLAY_RANGES[r.plan.range].last);
  // One line: the caption under the table names the replayed stages.
  let text = `${r.plan.banner}：${L.fmtDuration(realNs)}／${L.fmtDuration(r.plan.realNs)}${r.plan.excludedText}`;
  if (skipped.length) text += `（${skipped.map((g) => segmentLabel(g)).join('、')}未量測）`;
  el.replayBanner.textContent = text;
  el.replayBanner.hidden = false;
  el.liveNote.hidden = true;
  if (state.ctx) renderCaption(state.ctx);
  if (ms > r.plan.totalMs + REPLAY_HOLD_MS) stopReplay();
}

function stopReplay() {
  const r = state.replay;
  if (!r) return;
  clearInterval(r.timer);
  state.replay = null;
  for (const tr of state.rows) tr.classList.remove('rp-on', 'rp-off');
  el.replayBanner.hidden = true;
  el.liveNote.hidden = false;
  el.replayStop.hidden = true;
  scheduleRender();
}

// ----------------------------------------------------------------------------------------- screenshot mode
function applyShot() {
  const root = document.documentElement;
  root.classList.toggle('shot', !!state.shot.mode);
  root.classList.toggle('shot-stage-mode', state.shot.mode === 'stage');
  el.shotStage.hidden = state.shot.mode !== 'stage';
  fitShot();
}

function fitShot() {
  const root = document.documentElement;
  if (!state.shot.mode) {
    root.style.removeProperty('--shot-scale');
    return;
  }
  const scale = Math.min(1, window.innerWidth / SHOT_W, window.innerHeight / SHOT_H);
  root.style.setProperty('--shot-scale', String(scale));
}

function setShot(mode, stage) {
  const params = new URLSearchParams(window.location.search);
  params.delete('stage');
  params.delete('shot');
  const rest = params.toString();
  const shot = mode ? `shot=${mode === 'stage' ? `stage=${Number(stage)}` : 'all'}` : '';
  const query = [shot, rest].filter(Boolean).join('&');
  window.history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
  state.shot = L.parseShot(window.location.search);
  applyShot();
  scheduleRender();
}

function onKey(event) {
  if (event.target && event.target.closest && event.target.closest('input, select, textarea')) return;
  if (event.key === 'S' && event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
    setShot(state.shot.mode ? null : 'all');
    event.preventDefault();
    return;
  }
  if (!state.shot.mode) {
    if (event.key === 'Escape' && state.pickedStage) {
      state.pickedStage = null;
      scheduleRender();
    }
    return;
  }
  if (event.key === 'Escape') setShot(null);
  else if (event.key === 'ArrowRight') {
    setShot('stage', state.shot.mode === 'stage' ? Math.min(L.STAGE_COUNT, state.shot.stage + 1) : 1);
  } else if (event.key === 'ArrowLeft') {
    if (state.shot.mode === 'stage' && state.shot.stage > 1) setShot('stage', state.shot.stage - 1);
    else setShot('all');
  }
}

// ------------------------------------------------------------------------------------------------------ init
function init() {
  applyShot();
  el.liveNoteIcon.replaceChildren(icon('pulse', 'icon live-icon'));
  el.form.addEventListener('submit', submitForm);
  el.form.addEventListener('input', onFormInput);
  el.form.addEventListener('change', (event) => {
    onFormInput();
    if (event.target && event.target.name === 'backend') scheduleRender();
  });
  el.bmiApply.addEventListener('click', applyBmiHelper);
  el.replayAll.addEventListener('click', () => startReplay('all'));
  el.replayDetail.addEventListener('click', () => startReplay('detail'));
  el.replayStop.addEventListener('click', stopReplay);
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', fitShot);
  start();
}

init();
