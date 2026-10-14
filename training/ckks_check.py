"""Run the test split through the real CKKS pipeline and compare it with plaintext.

Linux or WSL only; standard library only (from C:/GitHub/Grad, i.e. /mnt/c/GitHub/Grad):
    python3 training/ckks_check.py [--split test] [--limit N] [--workers 3]

Uses the repository's accepted V4 C++ CKKS code (software/native_ckks)
through its ctypes API, building training/build/libedgefhe_native.so with g++ when needed.
Per 2048-record batch: encode four features, fresh OS sampling, encrypt, the cloud step
(ciphertexts + public model only), decrypt and decode. Keys are generated once. No device
and no network are used, so timings are CPU timings on this machine, not KV260 timings.
Writes training/outputs/ckks_check.json for the dashboard.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NATIVE = ROOT / 'software' / 'native_ckks'
sys.dont_write_bytecode = True  # never leave __pycache__ inside the hash-locked repository
sys.path.insert(0, str(NATIVE))
from native_api import Native, N, SCALE, SLOTS, words  # noqa: E402
from model import read_model, encode_model  # noqa: E402


def build_library(target):
    sources = [NATIVE / 'core.cpp', NATIVE / 'c_api.cpp']
    newest = max(p.stat().st_mtime for p in sources + [NATIVE / 'core.hpp'])
    if target.exists() and target.stat().st_mtime >= newest:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['g++', '-std=c++17', '-O2', '-fno-fast-math', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                    '-pthread', *map(str, sources), '-o', str(target)], check=True)
    print(f'built {target}', flush=True)


def auc(labels, scores):
    """Rank-based ROC-AUC (Mann-Whitney), ties averaged; no third-party packages."""
    order = sorted(range(len(scores)), key=scores.__getitem__)
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    positives = sum(labels)
    negatives = len(labels) - positives
    rank_sum = sum(r for r, y in zip(ranks, labels) if y == 1)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--split', choices=('validation', 'test'), default='test')
    parser.add_argument('--limit', type=int, default=0, help='only the first N records (0 = all)')
    parser.add_argument('--workers', type=int, choices=(1, 3), default=3)
    args = parser.parse_args()
    if platform.system() != 'Linux':
        raise SystemExit('Run in Linux or WSL: the native CKKS library uses Linux APIs')
    library = HERE / 'build' / 'libedgefhe_native.so'
    build_library(library)

    model, model_sha = read_model(HERE / 'outputs' / 'model.json')  # the repo's own model validation
    edge = json.loads((HERE / 'outputs' / 'edge_preprocess.json').read_text(encoding='utf-8'))
    clip = model['input_abs_max']
    with open(ROOT / 'data' / 'cdc_diabetes' / 'splits' / f'{args.split}.csv', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        records, labels = [], []
        for row in reader:
            z = [min(clip, max(-clip, (float(row[name]) - m) / s))
                 for name, m, s in zip(edge['features'], edge['mean'], edge['std'])]
            records.append(z)
            labels.append(int(float(row[edge['label']])))
            if args.limit and len(records) == args.limit:
                break
    plain = [sum(w * v for w, v in zip(model['weights'], z)) + model['bias'] for z in records]

    phases = {'encode': 0, 'sample': 0, 'encrypt': 0, 'cloud_affine': 0, 'decrypt_decode': 0}
    per_batch, ckks, classes = [], [], []
    started = time.perf_counter_ns()
    with Native(library, args.workers) as native:
        weights, bias = encode_model(native, model)
        sampled, a = native.sample()
        secret = words(sampled[:N], 'b')
        public_b = native.keygen(sampled, a)
        bound = native.error_bound(model)
        if bound > model['max_logit_error']:
            raise SystemExit('Native error bound exceeds the model target')
        for begin in range(0, len(records), SLOTS):
            batch = records[begin:begin + SLOTS]
            times = {}
            t = time.perf_counter_ns()
            messages = sum((native.encode([z[f] for z in batch]) for f in range(4)), [])
            limit = native.no_wrap(messages, weights, bias)
            times['encode'] = time.perf_counter_ns() - t
            t = time.perf_counter_ns()
            samples = native.encryption_samples()
            times['sample'] = time.perf_counter_ns() - t
            t = time.perf_counter_ns()
            cipher = native.encrypt(public_b, a, messages, samples)
            times['encrypt'] = time.perf_counter_ns() - t
            t = time.perf_counter_ns()
            result = native.affine(cipher, weights, bias)  # cloud: ciphertexts + public model only
            times['cloud_affine'] = time.perf_counter_ns() - t
            t = time.perf_counter_ns()
            coefficients = native.decrypt(result, secret)
            if max(map(abs, coefficients)) > limit:
                raise SystemExit('Decrypted coefficients exceed the no-wrap bound')
            decoded = native.decode(coefficients, SCALE * SCALE)
            times['decrypt_decode'] = time.perf_counter_ns() - t
            logits = [v.real for v in decoded[:len(batch)]]
            _, batch_classes = native.decisions(logits, bound)
            ckks += logits
            classes += batch_classes
            for k, v in times.items():
                phases[k] += v
            worst = max(abs(c - p) for c, p in zip(logits, plain[begin:begin + len(batch)]))
            per_batch.append({'batch': len(per_batch), 'records': len(batch), 'max_abs_logit_diff': worst,
                              'ms': {k: round(v / 1e6, 3) for k, v in times.items()}})
            print(f'batch {len(per_batch):3d}: {len(batch)} records, max |diff| {worst:.2e}, '
                  f'{sum(times.values()) / 1e6:.1f} ms', flush=True)
    total_ns = time.perf_counter_ns() - started

    diffs = [abs(c - p) for c, p in zip(ckks, plain)]
    agree = disagree = uncertain = 0
    for c, p in zip(classes, plain):
        if c is None:
            uncertain += 1
        elif c == int(p > 0):
            agree += 1
        else:
            disagree += 1
    certain_true = [(c, y) for c, y in zip(classes, labels) if c is not None]
    tp = sum(c == 1 and y == 1 for c, y in certain_true)
    tn = sum(c == 0 and y == 0 for c, y in certain_true)
    fp = sum(c == 1 and y == 0 for c, y in certain_true)
    fn = sum(c == 0 and y == 1 for c, y in certain_true)
    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'split': args.split, 'records': len(records), 'batches': len(per_batch),
        'model_id': model['model_id'], 'model_sha256': model_sha, 'features': edge['features'],
        'error_bound': bound, 'max_abs_logit_diff': max(diffs), 'mean_abs_logit_diff': sum(diffs) / len(diffs),
        'all_within_bound': all(d <= bound for d in diffs),
        'decisions': {'agree': agree, 'disagree': disagree, 'uncertain': uncertain,
                      'agreement_rate_certain': agree / (agree + disagree) if agree + disagree else None},
        'ckks_metrics': {'auc': auc(labels, ckks), 'plain_auc': auc(labels, plain),
                         'accuracy_certain': (tp + tn) / len(certain_true),
                         'sensitivity': tp / (tp + fn), 'specificity': tn / (tn + fp),
                         'confusion': {'tn': tn, 'fp': fp, 'fn': fn, 'tp': tp}},
        'timing': {'total_ms': round(total_ns / 1e6, 1),
                   'phase_ms': {k: round(v / 1e6, 1) for k, v in phases.items()},
                   'records_per_second': round(len(records) / (total_ns / 1e9), 1),
                   'note': 'CPU-only on this machine through the V4 C++ library (workers=%d); '
                           'not KV260 and not FPGA timings' % args.workers},
        'environment': {'python': platform.python_version(), 'machine': platform.machine(),
                        'system': platform.platform()},
        'per_batch': per_batch,
    }
    (HERE / 'outputs' / 'ckks_check.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f"records={len(records)} max|diff|={max(diffs):.2e} bound={bound:.2e} "
          f"agree={agree} disagree={disagree} uncertain={uncertain} "
          f"AUC ckks={report['ckks_metrics']['auc']:.4f} plain={report['ckks_metrics']['plain_auc']:.4f}")
    print('CKKS_CHECK_DONE' if disagree == 0 and report['all_within_bound'] else 'CKKS_CHECK_MISMATCH')
    return 0 if disagree == 0 and report['all_within_bound'] else 1


if __name__ == '__main__':
    sys.exit(main())
