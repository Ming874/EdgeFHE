export type View = 'tour' | 'live' | 'application' | 'system' | 'engineering';
export type Provenance = 'reference-demo' | 'checked-evidence' | 'live';
export type ConnectionState = 'offline' | 'disconnected' | 'live';
export interface Model {
  id: string; title: string; short: string; features: string[]; featureCount: number;
  task: 'regression' | 'classification'; sourceSha256: string; threshold: number;
  weightScale: number; outputScale: number; inputAbsMax: number; primary: boolean;
  sourceName: string; targetMean: number | null; targetStd: number | null; notice: string;
}
export interface PublicCase {
  id: string; modelId: string; label: string; raw: number[]; normalized: number[];
  contributions: number[]; bias: number; score: number; boundary: boolean;
  provenance: 'reference-demo'; referenceKind: 'native-cpp-public-scalar-affine'; notice: string;
}
export interface Interval { point: number; CI95: [number, number] }
export interface Stages { normalize_ns: number; encrypt_ns: number; cloud_ns: number;
  decrypt_ns: number; sink_ns: number; cleanup_ns: number; wall_ns: number }
export interface ModelEvidence {
  model: string; primary: boolean; model_pass: boolean;
  W1_latency_speedup: Interval; W2_sustained_speedup: Interval;
  P95_speedup: Record<'1' | '2048', Interval>;
  W1_cpu_P50_ms: number; W1_candidate_P50_ms: number;
  W2_cpu_records_s: number; W2_candidate_records_s: number;
  W2_window_max_error: Record<'cpu' | 'pl', number>;
  W2_window_requests: Record<'cpu' | 'pl', number>;
  W2_max_in_flight: Record<'cpu' | 'pl', number>;
  stage_median_of_session_medians_ms: Record<'1' | '2048', Record<'cpu' | 'candidate', Stages>>;
}
export type Backend = 'cpu' | 'pl';
export interface LanDecomposition { prepare: number; net: number; decrypt: number; total: number }
export interface LanModel { model: string; latency: Interval; records: Interval; P95: Interval;
  decompositionMs: Record<Backend, LanDecomposition>; windowRecordsPerS: Record<Backend, number> }
export interface LanRun { id: 'wired' | 'wifi'; title: string; bootTag: string; sessions: number; connections: number;
  soakWindows: number; soakComplete: boolean; serverResponses: number | null; deviceCalls: number; sourceId: string;
  note: string; models: LanModel[]; serverConnections: number; serverCloudMedianMs: number; scope: string }
export interface SoakModel { windows: number; meanRecordsPerS: number; minRecordsPerS: number; maxRecordsPerS: number;
  firstHour: number; lastHour: number; p50Ms: number; p99Ms: number; maxMs: number }
export interface Lan {
  provenance: 'checked-evidence'; networkE2E: true; wholeProjectAcceptance: false; scope: string;
  topology: { front: string; edge: string; cloud: string; transport: string; lan: string };
  runs: LanRun[];
  soak: { sourceId: string; windows: number; activeHours: number; requests: number; records: number; deviceCalls: number;
    maxError: number; perModel: Record<string, SoakModel>;
    series: { index: number; model: string; recordsPerS: number; activeHour: number }[] };
}
export interface Bundle {
  schema: 'edgefhe.showcase.v2'; version: string; generatedOn: string;
  sources: { id: string; label: string; sha256: string; kind: string }[];
  models: Model[]; cases: PublicCase[];
  campaign: { id: string; title: string; acceptedStatus: string; provenance: 'checked-evidence'; sourceId: string;
    sessions: number; pairs: number; warmups: number; windowSeconds: number; workers: number;
    networkE2E: false; wholeProjectAcceptance: false; scope: string; confidenceScope: string;
    deviceCalls: number; sessionCalls: number; archiveSha256: string; models: ModelEvidence[];
    precisionExceedances: Record<string, number> };
  hardware: { name: string; n: number; slots: number; limbs: number; lanesPerLimb: number;
    nominalPLMHz: number; baseline: string; sourceId: string; clockNotice: string; architectureNotice: string;
    resources: { name: string; used: number; available: number; percent: number }[] };
  sensors: { provenance: 'checked-evidence'; scope: string; maxTempC: number; cpuFrequencyKHz: number;
    sessions: { index: number; powerW: number; temperatureC: number; samples: number }[] };
  energy: { scope: string; windowSeconds: number; hasCI: false; idleW: number;
    models: { modelId: string; recordsPerJRatio: number;
      cpu: { records_per_s: number; mean_W: number; records_per_J_total: number; records_per_J_incremental: number };
      pl: { records_per_s: number; mean_W: number; records_per_J_total: number; records_per_J_incremental: number } }[] };
  papers: { id: string; title: string; year: number; venue: string; doi: string; url: string;
    limit: string; pages: { first: number; last: number } }[];
  gates: { id: string; title: string; state: 'accepted' | 'partial' | 'pending'; detail: string }[];
  lan: Lan;
}
export interface JobEvent { requestId: string; kind: 'stage' | 'completed' | 'failed'; stage?: number;
  provenance: Provenance; message: string }
export interface ShowcaseAdapter {
  connection(): ConnectionState;
  models(): Promise<Model[]>;
  evidence(): Promise<Bundle>;
  referenceCase(id: string): Promise<PublicCase>;
  runInference(modelId: string): Promise<never>;
  subscribe(onEvent: (event: JobEvent) => void): () => void;
}
export interface LiveGatewayHello { type: 'hello'; protocol: 'edgefhe.live.v1'; boot_tag: string; session: string; fastpath: string;
  irqs: Record<string, number>; models: string[]; requests: number; session_dma: number; fixture: boolean }
export interface LiveStatus { connected: boolean; gateway: LiveGatewayHello | null; error: string | null; board: string; port: number;
  server_log: boolean; console: 'edgefhe.live.v1' }
export interface LiveBackendResult { score: number; abs_error: number; probability: number | null; prepare_ms: number; network_ms: number;
  decrypt_ms: number; request_ms: number; cold_context_and_TLS_ms: number; process_wall_ms: number; request_sha256: string; response_sha256: string;
  upload_bytes: number; response_bytes: number; ciphertexts: number; dma: number; device_calls: number; PL_hz: number; real_PL: boolean; fixture: boolean;
  cloud_log: { cloud_us: number; sequence: number; response_payload_sha256: string; response_digest_matches: boolean } | null;
  repeat?: number; score_spread?: number;
  samples?: { run: number; request_ms: number; prepare_ms: number; network_ms: number; decrypt_ms: number; request_sha256: string; response_sha256: string; dma: number }[] }
export interface LiveResult { type: 'result'; provenance: 'live'; request_id: string; index: number; model: string; normalized: number[];
  reference: { score: number; probability: number | null }; results: Partial<Record<Backend, LiveBackendResult>>;
  irq_before: Record<string, number>; irq_after: Record<string, number>; session_dma: number; boot_tag: string; console_round_trip_ms: number }
export interface LiveError { type: 'error'; code: string; detail?: string; request_id?: string | null }
