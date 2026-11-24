// Unit tests for demo/ui/lib.mjs. Uses the recorded fixture and the form schema of demo/model.
// Run from the repository root with a quoted glob, which Node expands itself (a bare directory argument fails on
// Node 21 and later, which loads it as a CommonJS module):  node --test "demo/ui/tests/*.test.mjs"
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  answersText, applyEvent, BADGES, bmiFromMetric, checkForm, createStore, csvText, DEFAULT_SIZES, evidenceView,
  failStage, fmtBytes, fmtDuration, fmtInt, fmtIrqDelta, fmtMHz, fmtPercent, fmtSci, fmtSigned, frameSizes, groupLabel,
  groupOfStage, hexPrefix, isFormRun, joinMixed, keptMarker, keptText, latestFormRun, levelScale, levelView, liveMarks,
  MODEL_NAME, NO_RETRY_LINE, parseBmiText, parseShot, presetFor, recordsText, REHEARSAL_LABEL, replayAt, replayPlan,
  runFinished, runSummary, showsScale, sourceLabel, SOURCES, STAGE_COUNT, stageCaption, stageDefs, stageStates,
  stageTimes, TIME_GROUPS, timeLabels, tlsRecords, tlsWireBytes, wipeText,
} from '../lib.mjs';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/run_M_pl.json', import.meta.url), 'utf8'));
const form = JSON.parse(readFileSync(new URL('../../model/form_cdc.json', import.meta.url), 'utf8'));
const libSource = readFileSync(new URL('../lib.mjs', import.meta.url), 'utf8');

function loadRun(events = fixture.events) {
  const store = createStore();
  let run = null;
  for (const env of events) run = applyEvent(store, env, env.seq) || run;
  return { store, run };
}

// The result envelope data the real gateway (efgw/runner.py with the fake client) produced for preset K.
const K_RESULT = {
  logit_ckks: 0.00018479930722152576, logit_plain: 0.00018239930722152575, abs_error: 2.400000000000004e-06,
  max_abs_error: 0.002, passed: true, level: null, level_code: null, level_name: null, level_label: null,
  observed_rate: null, observed_caption: null, boundary: [2, 3], boundary_text: '介於 L2／L3，不判定',
  plain_level: null, plain_boundary: [2, 3], message: null,
};

function withResult(data) {
  return fixture.events.map((e) => (e.type === 'result' ? { ...e, data: { ...e.data, ...data } } : e));
}

test('formatting helpers', () => {
  assert.equal(fmtInt(393472), '393,472');
  assert.equal(fmtInt(-1234567), '-1,234,567');
  assert.equal(fmtBytes(98560), '98,560 B');
  assert.equal(fmtBytes(null), '—');
  assert.equal(fmtDuration(61240), '61.2 µs');
  assert.equal(fmtDuration(4876310), '4.88 ms');
  assert.equal(fmtDuration(9816447), '9.82 ms');
  assert.equal(fmtDuration(1412553901), '1.41 s');
  assert.equal(fmtDuration(999999), '1.00 ms');
  assert.equal(fmtDuration(999), '999 ns');
  assert.equal(fmtDuration(-1), '—');
  assert.equal(fmtSci(0.002), '2×10⁻³');
  assert.equal(fmtSci(2.3999999999579913e-06), '2.4×10⁻⁶');
  assert.equal(fmtSci(1e-6), '1×10⁻⁶');
  assert.equal(fmtSci(0), '0');
  assert.equal(fmtPercent(0.2149), '21.5%');
  assert.equal(fmtMHz(199999998), '199.999998 MHz');
  assert.equal(fmtMHz(0), '—');
  assert.equal(fmtIrqDelta({ 62: 2, 61: 2 }), '+2／+2');
  assert.equal(fmtSigned(-0.5), '−0.500000');
  assert.equal(hexPrefix('8b07398844abb3c58789509402e53241b67c9b4d3df7a6e68df78ed9807fdfb7'), '8b07398844abb3c5');
  assert.equal(hexPrefix('not hex'), '—');
});

test('frame and TLS record sizes', () => {
  assert.deepEqual(tlsRecords(393472), [273, ...new Array(24).fill(16401)]);
  assert.equal(tlsWireBytes(393472), 394022);
  assert.equal(tlsWireBytes(98560), 98714);
  assert.equal(recordsText(tlsRecords(98560)), '1 × 273 B＋6 × 16,401 B');
  const m = fixture.events.find((e) => e.class === 'owner' && e.type === 'form.accepted');
  assert.equal(csvText(m.data.values, form), 'GenHlth,HighBP,BMI,Age\n3,1,31.2,9\n');
  assert.equal(csvText(m.data.values, form).length, 34);
});

test('fixture envelopes follow the event contract', () => {
  assert.equal(fixture.schema, 'edgefhe.demo.ui-fixture.v1');
  assert.equal(fixture.synthetic, true);
  let seq = 0;
  for (const e of fixture.events) {
    assert.deepEqual(Object.keys(e), ['v', 'seq', 'run', 'type', 'class', 't_mono_ns', 'data']);
    assert.equal(e.v, 1);
    assert.equal(e.seq, seq + 1);
    seq = e.seq;
    assert.match(e.run, /^r[0-9a-f]{12}$/);
    assert.ok(['owner', 'public'].includes(e.class));
    if (e.class === 'owner') assert.ok(['form.accepted', 'result', 'form.refused'].includes(e.type), e.type);
  }
  assert.equal(fixture.status.last_seq, seq);
  // The status and wiped fields of the gateway contract; board figures stay placeholders.
  assert.equal(fixture.status.rehearsal, false);
  assert.equal(fixture.status.retain_private, false);
  assert.equal(fixture.events.find((e) => e.type === 'wiped').data.retained_private, false);
  const types = fixture.events.map((e) => e.type);
  assert.deepEqual(types.slice(0, 3), ['form.accepted', 'form.accepted', 'client.spawned']);
  assert.deepEqual(types.slice(-4), ['client.exited', 'irq.delta', 'result', 'wiped']);
  assert.equal(fixture.events[0].class, 'public');
  assert.equal(fixture.events[1].class, 'owner');
});

test('reducer merges the two form.accepted envelopes and ignores old sequence numbers', () => {
  const { store, run } = loadRun();
  assert.equal(run.kind, 'form');
  assert.equal(run.backend, 'pl');
  assert.equal(run.commitment, fixture.events[0].data.commitment);
  assert.deepEqual(run.owner.values, { GenHlth: 3, HighBP: 1, BMI: 31.2, Age: 9 });
  assert.equal(run.owner.features.length, 4);
  assert.equal(run.result.level, 3);
  assert.ok(isFormRun(run) && runFinished(run));
  assert.equal(latestFormRun(store), run);
  assert.equal(applyEvent(store, fixture.events[3]), null);
  assert.equal(store.lastSeq, fixture.events.length);
  assert.equal(applyEvent(store, { seq: 'x' }), null);
});

test('stage states advance with the live markers of the board counters', () => {
  const store = createStore();
  const seen = [];
  for (const env of fixture.events) {
    const run = applyEvent(store, env);
    const states = stageStates(run, DEFAULT_SIZES);
    seen.push([env.type, env.data.label || '', states.indexOf('active') + 1]);
  }
  const at = (type, label = '') => seen.find((s) => s[0] === type && s[1] === label)[2];
  assert.equal(at('form.accepted'), 3);
  assert.equal(at('client.spawned'), 4);
  assert.equal(at('progress.irq', 'LOAD'), 5);
  assert.equal(at('progress.irq', 'ENC'), 9);
  assert.equal(seen.filter((s) => s[0] === 'progress.io').pop()[2], 12);
  assert.equal(at('client.exited'), 13);
  const { run } = loadRun();
  assert.deepEqual(stageStates(run), new Array(STAGE_COUNT).fill('done'));
  assert.deepEqual(stageStates(null), new Array(STAGE_COUNT).fill('idle'));
  const m = liveMarks(run, DEFAULT_SIZES);
  assert.equal(m.load, 1500071904);
  assert.equal(m.enc, 1600083117);
  assert.equal(m.uploadDone, 1625166830);
  assert.equal(m.downloadDone, 1625166830);
  assert.ok(m.downloaded >= DEFAULT_SIZES.response_frame_bytes);
  assert.deepEqual(m.irqLines, { 61: 2, 62: 2 });
});

test('every stage shows bytes and a measured time for the fixture', () => {
  const { run } = loadRun();
  const times = stageTimes(run, { submitToCommitNs: fixture.ui.submit_to_commitment_ns, recorded: true });
  assert.equal(times.submit.ns, 4183250);
  assert.equal(times.setup.ns, 3412480);
  assert.equal(times.cold.ns, 1412553901);
  assert.equal(times.prepare.ns, 4876310);
  assert.equal(times.roundtrip.ns, 9816447);
  assert.equal(times.decrypt.ns, 1702118);
  assert.equal(times.finish.ns, 1869650);
  assert.ok(times.extra.unitemized_ns > 0 && times.extra.unitemized_ns < times.extra.wall_ns);
  const defs = stageDefs('pl', { sizes: frameSizes(fixture.status, run), csvBytes: 34, wipedFiles: run.wiped.files });
  assert.equal(defs.length, STAGE_COUNT);
  for (const d of defs) {
    assert.ok(d.title && d.place && d.bytes && d.explain && d.badges.length, `stage ${d.n}`);
    const g = groupOfStage(d.n);
    assert.ok(g && times[g.id].ns > 0, `time of stage ${d.n}`);
    for (const b of d.badges) assert.ok(BADGES[b], b);
    assert.ok(SOURCES[g.source]);
  }
  assert.equal(defs[2].bytes, 'CSV 34 B');
  assert.equal(defs[8].bytes, '393,472 B');
  assert.equal(defs[8].bytesWire, '線上 394,022 B');
  assert.equal(defs[10].bytesWire, '線上 98,714 B');
  assert.equal(defs[12].bytes, '清除 3 個檔案');
  assert.equal(groupLabel(TIME_GROUPS[2]), '第 4–5 階段合計');
  const noLocal = stageTimes(run);
  assert.equal(noLocal.submit.ns, null);
});

test('backends change places and badges', () => {
  const pl = stageDefs('pl');
  const spec = stageDefs('spec');
  const cpu = stageDefs('cpu');
  assert.equal(pl[7].place, 'PL');
  assert.equal(pl[6].badges[0], 'chip');
  assert.equal(pl[3].place, 'ARM→PL');
  assert.equal(spec[7].place, 'ARM');
  assert.equal(spec[6].badges[0], 'plain');
  assert.equal(cpu[7].title, '公鑰加密（ARM）');
  assert.equal(cpu[12].bytes, '清除 2 個檔案');
  assert.equal(pl[9].place, '雲端');
  assert.equal(pl[9].title, '雲端加權加總（仿射運算）');
  // Stage 4: the private key stays on the ARM; the 98,304 B are the public key, labelled as such.
  assert.deepEqual(pl[3].badges, ['key', 'pubkey']);
  assert.equal(pl[3].bytesPrefix, '公鑰');
  assert.equal(BADGES.pubkey.label, '公鑰・可公開');
  assert.equal(BADGES.owner.label, '明文結果・僅擁有者');
  // Stage 7: the polynomials stay on the board (ARM memory, DMA buffer, FPGA), not only inside the chip.
  assert.ok(pl[6].explain.includes('KV260 板上（ARM 記憶體、DMA 緩衝區與 FPGA）'));
  assert.ok(!pl[6].explain.includes('晶片內部'));
  for (const defs of [pl, spec, cpu]) {
    for (const d of defs) {
      assert.ok(d.plain && d.plain.endsWith('。'), `plain text of stage ${d.n}`);
      assert.ok(d.short && d.short.length <= 6, `short label of stage ${d.n}`);
    }
  }
  assert.ok(stageDefs('pl', { retainPrivate: true })[12].bytesNote.includes('tmpfs'));
  assert.ok(!stageDefs('pl', { retainPrivate: false })[12].bytesNote.includes('tmpfs'));
});

test('the cloud holds no CKKS key: never a claim of no key at all', () => {
  const texts = [];
  for (const backend of ['pl', 'spec', 'cpu']) {
    for (const d of stageDefs(backend)) texts.push(d.title, d.bytesNote, d.plain, d.explain);
  }
  for (const t of texts) assert.ok(!/(?<!CKKS )任何金鑰|不持有任何金鑰|無任何金鑰/.test(t), t);
  const cloud = stageDefs('pl')[9];
  assert.ok(cloud.plain.includes('雲端沒有任何 CKKS 金鑰，無法解密'));
  assert.ok(cloud.explain.includes('沒有任何 CKKS 金鑰') && cloud.explain.includes('TLS 伺服器金鑰只保護傳輸'));
});

test('captions, answers and the wipe sentence', () => {
  const defs = stageDefs('pl');
  assert.deepEqual(stageCaption(defs, [10]), { head: '第 10 階段・雲端加權加總（仿射運算）', text: defs[9].plain });
  const group = stageCaption(defs, [9, 10, 11]);
  assert.equal(group.head, '第 9–11 階段');
  assert.equal(group.text, defs[8].plain + defs[9].plain + defs[10].plain);
  assert.equal(stageCaption(defs, []), null);
  assert.equal(answersText({ GenHlth: 3, HighBP: 1, BMI: 31.2, Age: 9 }, form), '自評健康 好、高血壓 是、BMI 31.2、年齡 60–64 歲');
  assert.equal(answersText({ GenHlth: 3, HighBP: 0, BMI: 82, Age: 7 }, form), '自評健康 好、高血壓 否、BMI 82.0、年齡 50–54 歲');
  assert.equal(answersText(null, form), '');
  assert.equal(wipeText(false, true), '本機閘道已以零覆寫並刪除 in.csv 與 score.txt，並刪除呼叫許可。');
  assert.ok(wipeText(true, true).includes('記憶體暫存區（tmpfs），直到公開揭露或重新開機為止'));
  assert.equal(MODEL_NAME, 'CDC 糖尿病模型');
  assert.ok(REHEARSAL_LABEL.startsWith('PC 演練（PL 位元精確軟體模型）'));
});

test('PC rehearsal: measured figures are labelled as rehearsal values, never as board counts', () => {
  const board = timeLabels(false);
  const pc = timeLabels(true);
  assert.equal(board.header, '實測時間');
  assert.equal(board.fact, '實測時間');
  assert.ok(board.legend.startsWith('此圖示標示板上核心計數的即時標記（每 25 ms 取樣'), board.legend);
  assert.equal(pc.header, 'PC 演練時間');
  assert.equal(pc.fact, 'PC 演練時間');
  assert.ok(pc.legend.startsWith('此圖示標示演練容器核心計數的即時標記（PC 演練值，每 25 ms 取樣'), pc.legend);
  for (const t of Object.values(pc)) assert.ok(!/板上|實測/.test(t), t);
  assert.equal(sourceLabel('kernel'), '板上核心計數');
  assert.equal(sourceLabel('kernel', true), '演練容器核心計數');
  assert.equal(sourceLabel('client', true), 'client JSON');
  assert.equal(sourceLabel('gateway', false), '閘道');
});

test('a kept private record names its host with mixed-script spacing', () => {
  assert.equal(joinMixed('保存於', 'KV260', '的記憶體'), '保存於 KV260 的記憶體');
  assert.equal(joinMixed('保存於', '演練容器', '的記憶體'), '保存於演練容器的記憶體');
  assert.equal(joinMixed('於', 'KV260', '（tmpfs）'), '於 KV260（tmpfs）');
  assert.equal(joinMixed('', 'KV260', ''), 'KV260');
  assert.equal(keptMarker(false), '私有紀錄暫存於 KV260');
  assert.equal(keptMarker(true), '私有紀錄暫存於演練容器');
  assert.equal(keptText(false), '保存於 KV260 的記憶體暫存區（tmpfs），直到公開揭露或重新開機為止');
  assert.equal(wipeText(true, true),
    '暫存檔已以零覆寫並刪除；此次的私有紀錄（回答與分數）保存於 KV260 的記憶體暫存區（tmpfs），直到公開揭露或重新開機為止。');
  assert.ok(wipeText(true, false, true).includes('保存於演練容器的記憶體暫存區'));
  const unspaced = /[\u3400-\u9fff]KV260|KV260[\u3400-\u9fff]/;
  for (const rehearsal of [false, true]) {
    for (const noScore of [false, true]) {
      for (const d of stageDefs('pl', { retainPrivate: true, rehearsal, noScore })) {
        for (const t of [d.title, d.plain, d.bytesNote, d.explain]) assert.ok(!unspaced.test(t), t);
      }
    }
  }
  assert.ok(stageDefs('pl', { retainPrivate: true })[12].plain.includes('私有紀錄暫存於 KV260，直到揭露或重新開機'));
});

test('stage 13 of a run without a score: files wiped, no level decided', () => {
  const stop = stageDefs('pl', { noScore: true })[12];
  assert.equal(stop.plain, '本機閘道未取得分數，因此未分級，只以零覆寫並刪除暫存檔。');
  assert.ok(stop.explain.startsWith('本機閘道未取得模型分數，因此不重算參考分數，也不決定風險等級'), stop.explain);
  assert.ok(stop.explain.includes('並刪除呼叫許可'));
  assert.ok(!/2 × 10⁻³|依等級邊界/.test(stop.explain + stop.plain));
  const normal = stageDefs('pl')[12];
  assert.ok(normal.explain.includes('2 × 10⁻³') && normal.explain.includes('依等級邊界決定風險等級'));
  assert.ok(stageDefs('cpu', { noScore: true })[12].explain.endsWith('只以零覆寫並刪除 CSV 與分數檔。'));
});

test('glossary: 紀錄 for the noun, 記錄 only as a verb', () => {
  for (const backend of ['pl', 'spec', 'cpu']) {
    for (const d of stageDefs(backend, { retainPrivate: true })) {
      for (const t of [d.title, d.plain, d.bytesNote, d.explain]) {
        assert.ok(!/記錄(?!於)/.test(t), t);
        assert.ok(!/板子|開發板|操作者|加密程序|前 16 位/.test(t), t);
      }
    }
  }
  const pl = stageDefs('pl');
  assert.ok(pl[8].bytesNote.includes('TLS 紀錄 1 × 273 B＋24 × 16,401 B'));
  assert.ok(pl[10].explain.includes('1 × 273 B＋6 × 16,401 B 的紀錄'));
  assert.ok(pl[9].explain.includes('記錄於雲端電腦的 server.log'));
});

test('replay plan uses one slow-down factor per range', () => {
  const { run } = loadRun();
  const times = stageTimes(run, { submitToCommitNs: fixture.ui.submit_to_commitment_ns });
  const all = replayPlan(times, 'all');
  assert.equal(all.factor, 10);
  assert.equal(all.banner, '依本次實測時間重播（×1/10）');
  assert.equal(all.segments.length, 7);
  assert.ok(Math.abs(all.totalMs - (all.realNs * 10) / 1e6) < 1e-6);
  for (const s of all.segments) assert.ok(Math.abs(s.durMs - (s.realNs * all.factor) / 1e6) < 1e-9);
  // The full replay states the client's unitemized start-up and exit, which no stage owns.
  assert.equal(all.excludedText, '（不含程式啟動與結束 217 ms）');
  const detail = replayPlan(times, 'detail');
  assert.equal(detail.excludedText, '');
  assert.equal(detail.factor, 500);
  assert.equal(detail.banner, '依本次實測時間重播（×1/500）');
  assert.deepEqual(detail.segments.map((s) => s.group), ['prepare', 'roundtrip', 'decrypt']);
  assert.equal(replayAt(detail, 0).group, 'prepare');
  assert.equal(replayAt(detail, detail.totalMs + 1), null);
  assert.equal(replayPlan(stageTimes(null)), null);
  const partial = replayPlan(stageTimes(run), 'all');
  assert.ok(!partial.segments.some((s) => s.group === 'submit'));
});

test('level card: level and observed rate of the test data', () => {
  const { run } = loadRun();
  const v = levelView(run, form);
  assert.equal(v.state, 'level');
  assert.equal(v.name, '第 3 級');
  assert.equal(v.label, '中高');
  assert.equal(v.observed, '測試資料中，落在此等級者實際為糖尿病前期或糖尿病的比例：21.5%（6,906 筆中 1,484 筆）');
  assert.equal(v.errorText, 'CKKS 與明文模型相差 2.4×10⁻⁶（容許 2×10⁻³）');
  assert.deepEqual(levelScale(form).map((i) => i.code), ['L1', 'L2', 'L3', 'L4']);
});

test('level card: boundary result of preset K shows no level', () => {
  const { run } = loadRun(withResult(K_RESULT));
  const v = levelView(run, form);
  assert.equal(v.state, 'boundary');
  assert.equal(v.title, '介於 L2／L3，不判定');
  assert.deepEqual(v.boundary, [2, 3]);
  assert.equal(v.explain, '模型分數與等級邊界的距離不超過 2×10⁻³，本系統不判定等級。');
  assert.equal(v.level, undefined);
  assert.equal(runSummary(run, form).outcome, '介於 L2／L3，不判定');
});

test('level card: failed verification shows the message and no level', () => {
  const failed = withResult({ passed: false, abs_error: 0.01, level: null, level_name: null, message: form.verification.failed_text });
  const events = [...failed.slice(0, -1), {
    v: 1, seq: failed.length, run: failed[0].run, type: 'error', class: 'public', t_mono_ns: 1, data: {
      code: 'VERIFICATION_FAILED', message: form.verification.failed_text, fatal: true, stop: false,
    },
  }, { ...failed[failed.length - 1], seq: failed.length + 1 }];
  const { run } = loadRun(events);
  const v = levelView(run, form);
  assert.equal(v.state, 'unverified');
  assert.equal(v.message, form.verification.failed_text);
  const states = stageStates(run);
  assert.equal(states[12], 'fail');
  assert.equal(states[11], 'done');
});

test('refused form: no process, no network, stages 3-13 skipped', () => {
  const store = createStore();
  const run = applyEvent(store, {
    v: 1, seq: 1, run: 'r000000000001', type: 'form.refused', class: 'owner', t_mono_ns: 5, data: {
      code: 'BMI_ABOVE_MAX', message: form.refusals.BMI_ABOVE_MAX, field: 'BMI', note: form.refusal_note,
    },
  });
  assert.deepEqual(stageStates(run).slice(0, 3), ['done', 'fail', 'skip']);
  assert.ok(runFinished(run) && isFormRun(run));
  const v = levelView(run, form);
  assert.equal(v.state, 'refused');
  assert.equal(v.code, 'BMI_ABOVE_MAX');
  assert.equal(v.note, form.refusal_note);
  assert.equal(showsScale(v.state), false);
  // A refused run carries no owner values and no backend: the history uses what this page sent.
  assert.equal(runSummary(run, form).preset, null);
  const j = form.presets.find((p) => p.id === 'J');
  const s = runSummary(run, form, { values: j.form, backend: 'pl' });
  assert.deepEqual([s.preset, s.backend, s.outcome, s.known], ['J', 'FPGA（PL）', '本機拒絕（BMI_ABOVE_MAX）', true]);
});

test('a STOP code fails the stage it belongs to and skips the rest', () => {
  assert.equal(failStage('TLS_PEER_PIN', 4), 5);
  assert.equal(failStage('SOCKET_CONNECT_ERROR', 4), 5);
  assert.equal(failStage('TLS_DEADLINE', 4), 5);
  assert.equal(failStage('TLS_DEADLINE', 8), 9);
  assert.equal(failStage('TLS_FATAL_OR_UNEXPECTED_EOF', 9), 10);
  assert.equal(failStage('NORMALIZATION_REFUSED', 4), 6);
  assert.equal(failStage('OWNER_CREATE', 3), 4);
  assert.equal(failStage('PL_PERMIT_INVOCATION_BINDING', 3), 3);
  assert.equal(failStage('PL_CLOCK_RANGE', 12), 13);
  assert.equal(failStage('WIRE_RESPONSE_BINDING', 10), 11);
  assert.equal(failStage('SOMETHING_NEW', 8), 9);
  const upToEnc = fixture.events.filter((e) => e.seq <= 12);
  const stop = [...upToEnc, {
    v: 1, seq: 13, run: upToEnc[0].run, type: 'client.exited', class: 'public', t_mono_ns: 9, data: {
      returncode: 1, wall_ns: 31000000000, overdue: false, status: null, stop: 'TLS_DEADLINE', stop_detail: '', report: null,
    },
  }, {
    v: 1, seq: 14, run: upToEnc[0].run, type: 'error', class: 'public', t_mono_ns: 10, data: {
      code: 'TLS_DEADLINE', message: '與雲端伺服器的通訊超過時限（30000 毫秒）。', fatal: true, stop: true,
    },
  }];
  const { run } = loadRun(stop);
  const states = stageStates(run);
  assert.deepEqual(states.slice(0, 8), new Array(8).fill('done'));
  assert.equal(states[8], 'fail');
  assert.deepEqual(states.slice(9), ['skip', 'skip', 'skip', 'skip']);
  const v = levelView(run, form);
  assert.equal(v.state, 'error');
  assert.equal(v.code, 'TLS_DEADLINE');
  assert.equal(v.stop, true);
  // The gateway's message plus exactly one fixed line; no level scale.
  assert.equal(v.message, '與雲端伺服器的通訊超過時限（30000 毫秒）。');
  assert.equal(v.line, NO_RETRY_LINE);
  assert.equal(NO_RETRY_LINE, '本系統不會自動重試，也不會中斷加密程式。');
  assert.equal(showsScale(v.state), false);
  assert.equal(showsScale('level') && showsScale('boundary') && showsScale('running'), true);
  // The gateway still wipes the files after a STOP: stage 13 is done, stages 10-12 stay skipped.
  const wiped = loadRun([...stop, {
    v: 1, seq: 15, run: upToEnc[0].run, type: 'wiped', class: 'public', t_mono_ns: 11, data: {
      files: ['permit', 'in.csv', 'score.txt'], run_dir_removed: true, problems: [], retained_private: false,
    },
  }]).run;
  assert.deepEqual(stageStates(wiped).slice(8), ['fail', 'skip', 'skip', 'skip', 'done']);
});

test('evidence strip values', () => {
  const { run } = loadRun();
  const ev = evidenceView(run, fixture.status);
  const result = fixture.events.find((e) => e.type === 'result').data.evidence;
  assert.equal(ev.commitment, result.commitment.slice(0, 16));
  assert.equal(ev.request, result.request_sha256.slice(0, 16));
  assert.equal(ev.response, result.response_payload_sha256.slice(0, 16));
  assert.equal(ev.deviceCalls, '2');
  assert.equal(ev.irq, '+2／+2');
  assert.equal(ev.irqPassed, true);
  assert.equal(ev.plHz, '199.999998 MHz');
  assert.equal(ev.frames, '393,472 B／98,560 B');
  assert.ok(ev.complete);
  assert.equal(evidenceView(null).request, '—');
  const running = loadRun(fixture.events.filter((e) => e.seq <= 12)).run;
  const partial = evidenceView(running, fixture.status);
  assert.equal(partial.commitment, result.commitment.slice(0, 16));
  assert.equal(partial.request, '—');
  assert.equal(partial.frames, '—');
});

test('form checks mirror the gateway rules for every preset', () => {
  for (const p of form.presets) {
    const got = checkForm(p.form, form);
    assert.equal(got ? got.code : null, p.expected.refused, p.id);
  }
  const m = { GenHlth: 3, HighBP: 1, BMI: 31.2, Age: 9 };
  assert.equal(checkForm({ ...m, Age: 0 }, form).code, 'AGE_UNDER_18');
  assert.equal(checkForm({ ...m, BMI: 31.25 }, form).code, 'BMI_FORMAT');
  assert.equal(checkForm({ ...m, BMI: 11.9 }, form).code, 'BMI_BELOW_MIN');
  assert.equal(checkForm({ ...m, BMI: 81.1 }, form).code, 'BMI_ABOVE_MAX');
  assert.equal(checkForm({ ...m, BMI: 81 }, form), null);
  assert.equal(checkForm({ ...m, BMI: '31.2' }, form).code, 'BMI_FORMAT');
  assert.equal(checkForm({ ...m, GenHlth: 6 }, form).code, 'GENHLTH_VALUE');
  assert.equal(checkForm({ ...m, HighBP: 2 }, form).code, 'HIGHBP_VALUE');
  assert.equal(checkForm({ GenHlth: 3 }, form).code, 'FORM_FIELDS');
  assert.equal(checkForm({ ...m, Age: 0 }, form).message, form.refusals.AGE_UNDER_18);
  assert.equal(parseBmiText(' 31.2 '), 31.2);
  assert.equal(parseBmiText('82'), 82);
  assert.equal(parseBmiText(''), '');
  assert.equal(parseBmiText('abc'), 'abc');
  assert.equal(bmiFromMetric(175, 70), 22.9);
  assert.equal(bmiFromMetric(160, 50), 19.5);
  assert.equal(bmiFromMetric('x', 70), null);
  assert.equal(presetFor(m, form.presets).id, 'M');
  assert.equal(presetFor({ ...m, Age: 8 }, form.presets), null);
});

test('screenshot mode parameters', () => {
  assert.deepEqual(parseShot(''), { mode: null, stage: null, run: null });
  assert.deepEqual(parseShot('?shot=all'), { mode: 'all', stage: null, run: null });
  assert.deepEqual(parseShot('?shot=stage=7'), { mode: 'stage', stage: 7, run: null });
  assert.deepEqual(parseShot('?shot=stage&stage=3'), { mode: 'stage', stage: 3, run: null });
  assert.deepEqual(parseShot('?stage=13'), { mode: 'stage', stage: 13, run: null });
  assert.deepEqual(parseShot('?shot=9'), { mode: 'stage', stage: 9, run: null });
  assert.deepEqual(parseShot('?shot=stage=14'), { mode: 'all', stage: null, run: null });
  assert.deepEqual(parseShot('?shot=all&run=r62bcc80b3dcb'), { mode: 'all', stage: null, run: 'r62bcc80b3dcb' });
  assert.equal(parseShot('?shot=all&run=../x').run, null);
});

test('history summary of a run', () => {
  const { run } = loadRun();
  const s = runSummary(run, form);
  assert.equal(s.preset, 'M');
  assert.equal(s.backend, 'FPGA（PL）');
  assert.equal(s.outcome, '第 3 級（中高）');
  assert.match(s.request, /^[0-9a-f]{12}$/);
});

test('copy rules: a level, never a probability; no version labels', () => {
  const texts = [];
  for (const backend of ['pl', 'spec', 'cpu']) {
    for (const d of stageDefs(backend)) texts.push(d.title, d.place, d.where, d.bytes, d.bytesNote, d.explain);
  }
  for (const b of Object.values(BADGES)) texts.push(b.label);
  texts.push(...Object.values(SOURCES));
  const { run } = loadRun();
  texts.push(...Object.values(levelView(run, form)).filter((v) => typeof v === 'string'));
  for (const backend of ['pl', 'spec', 'cpu']) {
    for (const d of stageDefs(backend, { retainPrivate: true })) texts.push(d.plain, d.short, d.bytesNote, d.explain);
  }
  texts.push(NO_RETRY_LINE, REHEARSAL_LABEL, MODEL_NAME, wipeText(true, true), wipeText(false, false));
  for (const t of texts) {
    assert.ok(!/機率|V8b|rev ?5|這一版|前一版|上一版|板子/i.test(t), t);
  }
  assert.ok(!/機率|p_balanced/.test(libSource));
  // The observed rate is labelled in the form copy as a statistic of the test data, not the respondent's own.
  const note = form.levels.observed_note;
  assert.ok(note.includes('測試資料的群體統計') && note.includes('非填答者'), note);
  assert.ok(!/%|％/.test(note));
});
