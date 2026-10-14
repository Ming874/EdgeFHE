// Reads the JSON files written by tools/prepare_datasets.py, training/train.py,
// training/ckks_check.py and the repository's tools/compare_results.py (copied under
// results/). Server-only: the page renders on each request, so re-running a script and
// refreshing the page shows the new numbers.
import { readdir, readFile, stat } from 'node:fs/promises';
import path from 'node:path';

const GRAD = path.resolve(process.cwd(), '..');

export type Confusion = { tn: number; fp: number; fn: number; tp: number };

export type Metrics = {
  rows: number;
  auc: number;
  accuracy: number;
  sensitivity: number;
  specificity: number;
  precision: number;
  f1: number;
  confusion: Confusion;
  roc: [number, number][];
};

export type TrainingReport = {
  generated_utc: string;
  model_id: string;
  environment: { python: string; numpy: string; scikit_learn: string; platform: string };
  method: Record<string, string>;
  single_feature: { feature: string; validation_auc: number }[];
  selection_path: { step: number; added: string; validation_auc: number; runner_up: string; runner_up_auc: number }[];
  features: string[];
  weights: number[];
  bias: number;
  mean: number[];
  std: number[];
  system_checks: {
    max_abs_weight: number;
    abs_bias: number;
    input_abs_max: number;
    error_bound: number;
    max_logit_error: number;
    clipped_rows: Record<string, number>;
    passes: boolean;
  };
  metrics: { validation: Metrics; test: Metrics };
  baseline_test_accuracy_all_negative: number;
  seconds: number;
};

export type CkksCheck = {
  generated_utc: string;
  split: string;
  records: number;
  batches: number;
  model_id: string;
  model_sha256: string;
  error_bound: number;
  max_abs_logit_diff: number;
  mean_abs_logit_diff: number;
  all_within_bound: boolean;
  decisions: { agree: number; disagree: number; uncertain: number; agreement_rate_certain: number | null };
  ckks_metrics: {
    auc: number;
    plain_auc: number;
    accuracy_certain: number;
    sensitivity: number;
    specificity: number;
    confusion: Confusion;
  };
  timing: { total_ms: number; phase_ms: Record<string, number>; records_per_second: number; note: string };
  environment: { python: string; machine: string; system: string };
  per_batch: { batch: number; records: number; max_abs_logit_diff: number; ms: Record<string, number> }[];
};

export type DatasetManifest = {
  dataset: string;
  page: string;
  license: string;
  seed: number;
  split_rule: string;
  generated_utc: string;
  splits: Record<string, { rows: number; positive_rate: number; full_2048_batches: number }>;
};

async function readJson<T>(relative: string): Promise<T | null> {
  try {
    return JSON.parse(await readFile(path.join(GRAD, relative), 'utf8')) as T;
  } catch {
    return null; // not generated yet: the page shows how to produce it
  }
}

// tools/compare_results.py output (comparison*.json), group A (cpu-*) vs group B (fpga-*).
export type LatencyRow = {
  backend: string;
  label: string;
  file: string;
  edge_p50_ms: number;
  edge_p95_ms: number;
  total_p50_ms: number;
  steps_p50_ms: Record<string, number>;
  stats: { jobs: number; pack_ms: number; device_ms: number; unpack_ms: number; driver_reported_ms: number };
  real_device?: boolean; // FPGA rows: true = KV260 DMA, false = simulated device
};

export type ThroughputRow = {
  backend: string;
  label: string;
  file: string;
  pipeline: string;
  capacity: number;
  batches: number;
  records: number;
  records_per_s: number;
  cpu_utilization: number;
  mean_w: number | null;
  records_per_j: number | null;
  sensor: string | null;
  real_device?: boolean;
};

export type Comparison = {
  real_fpga: boolean;
  // Newer files: where the FPGA rows came from, and whether group B is present at all.
  fpga_device?: 'real' | 'simulated' | 'mixed' | 'none';
  has_group_b?: boolean;
  fairness_problems: string[];
  files: string[];
  inputs: { records: number; model_id: string; data_sha256: string };
  environment: { machine: string; cpu_model: string; online_cpus: number; affinity: string; pinned: boolean };
  correctness?: { all_pass: boolean; bit_exact: boolean; backends?: string[] };
  latency?: { best_A: string; best_A_ms: number; best_B: string; best_B_ms: number; B_speedup_over_A: number };
  throughput?: {
    batches: number;
    best_A: string;
    best_A_records_per_s: number;
    best_B: string;
    best_B_records_per_s: number;
    B_over_A: number;
  };
  energy?: { best_A_records_per_j: number; best_B_records_per_j: number; B_over_A: number; sensor: string };
  latency_rows: LatencyRow[];
  throughput_rows: ThroughputRow[];
};

// The newest valid comparison*.json anywhere under results/ (one folder per board session).
// Unreadable or foreign files are skipped instead of breaking the page.
function isComparison(value: unknown): value is Comparison {
  const v = value as Comparison | null;
  return !!v && Array.isArray(v.latency_rows) && Array.isArray(v.throughput_rows) && !!v.inputs && !!v.environment;
}

async function newestComparison(): Promise<{ file: string; data: Comparison } | null> {
  const root = path.join(GRAD, 'results');
  let names: string[];
  try {
    names = (await readdir(root, { recursive: true })).map(String);
  } catch {
    return null;
  }
  const dated: { file: string; time: number }[] = [];
  for (const name of names.filter((n) => /(^|[\\/])comparison[^\\/]*\.json$/.test(n))) {
    try {
      dated.push({ file: name, time: (await stat(path.join(root, name))).mtimeMs });
    } catch {
      // vanished or unreadable: ignore
    }
  }
  dated.sort((a, b) => b.time - a.time);
  for (const { file } of dated) {
    const data = await readJson<unknown>(path.join('results', file));
    if (isComparison(data)) return { file: file.split(path.sep).join('/'), data };
  }
  return null;
}

export async function loadDashboard() {
  const [manifest, report, ckks, comparison] = await Promise.all([
    readJson<DatasetManifest>('data/cdc_diabetes/manifest.json'),
    readJson<TrainingReport>('training/outputs/training_report.json'),
    readJson<CkksCheck>('training/outputs/ckks_check.json'),
    newestComparison(),
  ]);
  return { manifest, report, ckks, comparison };
}

// Chinese names for the BRFSS columns (data/README.md).
export const FEATURE_NAMES: Record<string, string> = {
  HighBP: '高血壓',
  HighChol: '高膽固醇',
  CholCheck: '五年內驗膽固醇',
  BMI: 'BMI',
  Smoker: '吸菸',
  Stroke: '中風史',
  HeartDiseaseorAttack: '心臟病史',
  PhysActivity: '有運動',
  Fruits: '每天吃水果',
  Veggies: '每天吃蔬菜',
  HvyAlcoholConsump: '重度飲酒',
  AnyHealthcare: '有醫療保險',
  NoDocbcCost: '因費用未就醫',
  GenHlth: '自評健康',
  MentHlth: '心理不佳天數',
  PhysHlth: '身體不佳天數',
  DiffWalk: '行走困難',
  Sex: '性別',
  Age: '年齡層',
  Education: '教育程度',
  Income: '收入等級',
};

export const featureName = (column: string) => FEATURE_NAMES[column] ?? column;
