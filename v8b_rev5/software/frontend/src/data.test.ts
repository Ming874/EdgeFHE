/// <reference types="node" />
import { describe, expect, it } from 'vitest';
import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { decodePinnedBundle, liveErrorText, MAX_BUNDLE_BYTES, OfflineAdapter, validateBundle, validateLiveResult } from './data';
import type { Bundle } from './types';

const raw = readFileSync(resolve('public/data/showcase.json'));
const original = JSON.parse(raw.toString()) as Bundle;
const fresh = () => structuredClone(original);
Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });

describe('accepted evidence and public demo remain separate', () => {
  it('accepts all three models, twelve cases and checked evidence', () => {
    const b=validateBundle(fresh()); expect(b.cases).toHaveLength(12);
    expect(b.campaign.networkE2E).toBe(false);expect(b.models[1].task).toBe('regression');
    expect(b.lan.networkE2E).toBe(true);expect(b.lan.runs.map(r=>r.id)).toEqual(['wired','wifi']);expect(b.lan.soak.series).toHaveLength(360);
  });
  const negatives: [string, (b: Bundle) => void][] = [
    ['wrong schema',b => { b.schema='wrong' as Bundle['schema']; }],
    ['wrong model order',b => { b.models.reverse(); }],
    ['model identity',b => { b.models[0].sourceSha256='unknown'; }],
    ['duplicated sources',b => { b.sources[1].id=b.sources[0].id; }],
    ['removed public case',b => { b.cases.pop(); }],
    ['case claims hardware',b => { b.cases[0].provenance='live' as never; }],
    ['case score',b => { b.cases[0].score+=1; }],
    ['case dimensions',b => { b.cases[0].raw.pop(); }],
    ['case out of bounds',b => { b.cases[0].normalized[0]=9; }],
    ['case NaN',b => { b.cases[0].score=NaN; }],
    ['network claim',b => { b.campaign.networkE2E=true as never; }],
    ['100% claim',b => { b.campaign.wholeProjectAcceptance=true as never; }],
    ['wrong session count',b => { b.campaign.sessions=1; }],
    ['wrong call count',b => { b.campaign.deviceCalls=1; }],
    ['invalid confidence interval',b => { b.campaign.models[0].W1_latency_speedup.CI95=[9,1]; }],
    ['zero throughput',b => { b.campaign.models[0].W2_candidate_records_s=0; }],
    ['negative stage',b => { b.campaign.models[0].stage_median_of_session_medians_ms['1'].candidate.cleanup_ns=-1; }],
    ['erased precision exception',b => { b.campaign.precisionExceedances['diabetes-symptom-logreg-v1']=0; }],
    ['fake resource',b => { b.hardware.resources[0].percent=101; }],
    ['missing recorded sensors',b => { b.sensors.sessions.pop(); }],
    ['invented energy CI',b => { b.energy.hasCI=true as never; }],
    ['unsafe paper URL',b => { b.papers[0].url='javascript:alert(1)'; }],
    ['whole project claim via release gate',b => { b.gates.find(g=>g.id==='release')!.state='accepted'; }],
    ['LAN claims whole project',b => { b.lan.wholeProjectAcceptance=true as never; }],
    ['LAN without network',b => { b.lan.networkE2E=false as never; }],
    ['LAN run order',b => { b.lan.runs.reverse(); }],
    ['LAN unknown source',b => { b.lan.runs[0].sourceId='invented'; }],
    ['LAN failing gate passed off',b => { b.lan.runs[0].models[0].latency={point:.9,CI95:[.8,1.1]}; }],
    ['LAN symptom model added',b => { b.lan.runs[0].models[1].model='diabetes-symptom-logreg-v1'; }],
    ['LAN zero network time',b => { b.lan.runs[1].models[0].decompositionMs.pl.net=0; }],
    ['Wi-Fi soak claimed complete',b => { b.lan.runs[1].soakComplete=true; }],
    ['soak window dropped',b => { b.lan.soak.series.pop(); }],
    ['soak window reordered',b => { [b.lan.soak.series[0],b.lan.soak.series[1]]=[b.lan.soak.series[1],b.lan.soak.series[0]]; }],
    ['soak shortened',b => { b.lan.soak.activeHours=5.5; }],
    ['soak records inflated',b => { b.lan.soak.records+=1; }],
    ['soak precision hidden',b => { b.lan.soak.maxError=NaN; }],
    ['soak model summary missing',b => { delete (b.lan.soak.perModel as Record<string,unknown>)['ai4i-failure-logreg-v1']; }],
  ];
  it.each(negatives)('rejects %s',(label,mutate) => { const b=fresh();mutate(b);expect(() => validateBundle(b),label).toThrow(); });
  it('rejects unexpected root data rather than carrying private fields',()=>{
    expect(()=>validateBundle({...fresh(),private_key:'not allowed'})).toThrow();
  });
  it('preserves all recorded exceptions',()=>{
    expect(validateBundle(fresh()).campaign.precisionExceedances).toEqual({
      'ai4i-failure-logreg-v1':1,'diabetes-progression-ridge-v1':0,'diabetes-symptom-logreg-v1':25});
  });
  it('checks the exact packaged content digest',async()=>{expect((await decodePinnedBundle(raw)).schema).toBe('edgefhe.showcase.v2');});
  it('rejects resealed or reformatted report bytes',async()=>{
    const b=fresh();b.campaign.models[0].W1_candidate_P50_ms=1;
    await expect(decodePinnedBundle(new TextEncoder().encode(JSON.stringify(b)))).rejects.toThrow('來源身分不符');
  });
  it('rejects oversized and empty bytes',async()=>{
    await expect(decodePinnedBundle(new Uint8Array())).rejects.toThrow('檔案大小');
    await expect(decodePinnedBundle(new Uint8Array(MAX_BUNDLE_BYTES+1))).rejects.toThrow('檔案大小');
  });
  it('offline adapter cannot submit live inference or contact a board',async()=>{
    const a=new OfflineAdapter(validateBundle(fresh()));expect(a.connection()).toBe('offline');
    expect(await a.models()).toHaveLength(3);expect((await a.referenceCase('public-1-0')).provenance).toBe('reference-demo');
    await expect(a.runInference()).rejects.toThrow('真機服務未接入');
    await expect(a.referenceCase('unknown')).rejects.toThrow();
    expect(a.subscribe()).toBeTypeOf('function');
  });
});

const replies = JSON.parse(readFileSync(resolve('tests/fixtures/live_replies_local_qa.json')).toString());
const live = () => structuredClone(replies['diabetes-progression-ridge-v1']);
describe('live replies are validated before display', () => {
  it('accepts a reply produced by the local end-to-end chain', () => {
    const r = validateLiveResult(live()); expect(Object.keys(r.results)).toEqual(['cpu', 'pl']); expect(r.results.pl!.ciphertexts).toBe(10);
  });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const bad: [string, (r: Record<string, any>) => void][] = [
    ['not live provenance', r => { r.provenance = 'reference-demo'; }],
    ['error passed as result', r => { r.type = 'error'; }],
    ['unknown backend', r => { r.results.gpu = r.results.pl; }],
    ['no backend', r => { r.results = {}; }],
    ['NaN score', r => { r.results.pl.score = NaN; }],
    ['bad digest', r => { r.results.pl.request_sha256 = 'abc'; }],
    ['CPU claims DMA', r => { r.results.cpu.dma = 3; }],
    ['negative DMA', r => { r.results.pl.dma = -1; }],
    ['missing reference', r => { delete r.reference; }],
  ];
  it.each(bad)('rejects %s', (_label, mutate) => { const r = live(); mutate(r); expect(() => validateLiveResult(r)).toThrow(); });
  it('explains refusals with the field name and never claims a retry', () => {
    expect(liveErrorText({ code: 'INPUT_OUT_OF_RANGE', detail: 'bmi' }, ['年齡', 'BMI'], ['age', 'bmi'])).toContain('「BMI」超出模型可接受範圍');
    expect(liveErrorText({ code: 'GATEWAY_CHANNEL_LOST_NOT_RETRIED' }, [], [])).toContain('沒有自動重送');
    expect(liveErrorText({ code: 'SOMETHING_NEW' }, [], [])).toContain('SOMETHING_NEW');
  });
});
