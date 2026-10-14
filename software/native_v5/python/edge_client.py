"""Stage 3, two machines: the board encrypts records with a V5 backend, the PC service
(software/native_ckks/pc_server.py) computes on ciphertexts only, and the board decrypts.

Python handles files, TLS and framing through the accepted V4 modules, unchanged; the C++
library libedgefhe_v5_capi.so does all CKKS arithmetic on the chosen backend:
  --backend cpu             group A, CPU backend with --workers (1..64)
  --backend fpga --fd N     group B, operator-opened descriptor after the live gates
  --backend fpga-simulated  group B code path on the simulated device (host QA only)
Records come from a CSV with the model's feature columns (for example the CDC test
split); standardization uses training/outputs/edge_preprocess.json. Decrypted results stay
on this machine: the output file holds aggregates and timings only. Standard library only.

Board example (operator; FPGA only after docs/BOARD_GUIDE.md gates):
  exec 3<>/dev/edgefhe_rns3_n4096_dma
  python3 software/native_v5/python/edge_client.py --backend fpga --fd 3 \\
      --library build/v5/libedgefhe_v5_capi.so --config local/join_config.json \\
      --server-cert local/server_ca.pem --private-dir local \\
      --model training/outputs/model.json --preprocess training/outputs/edge_preprocess.json \\
      --data data/cdc_diabetes/splits/test.csv --output build/stage3/client_fpga.json
  exec 3>&-
"""
import argparse
import csv
import ctypes as C
import json
import math
import os
import platform
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # never leave __pycache__ inside the hash-locked repository
V4 = Path(__file__).resolve().parents[2] / 'native_ckks'
sys.path.insert(0, str(V4))
from dynamic_client import Session  # noqa: E402
from native_api import Native, SLOTS  # noqa: E402
from protocol import load_json, require  # noqa: E402
from transport import Client  # noqa: E402

PHASES = ('encode_and_bound_ns', 'fresh_OS_sampling_ns', 'native_encrypt_ns',
          'framing_TLS_cloud_roundtrip_ns', 'native_decrypt_decode_ns')


def read_records(path, preprocess, limit):
    """Standardized, clipped feature rows (model order) and labels when present."""
    features, label = preprocess['features'], preprocess['label']
    rows, labels = [], []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        missing = [name for name in features if name not in (reader.fieldnames or [])]
        require(not missing, 'CSV lacks columns: ' + ', '.join(missing))
        has_label = label in reader.fieldnames
        for row in reader:
            raw = [float(row[name]) for name in features]
            require(all(math.isfinite(v) for v in raw), 'Non-finite feature value')
            clip = preprocess['clip']
            rows.append([min(clip, max(-clip, (x - m) / s))
                         for x, m, s in zip(raw, preprocess['mean'], preprocess['std'])])
            if has_label:
                labels.append(int(float(row[label])))
            if limit and len(rows) == limit:
                break
    require(rows, 'No records in ' + str(path))
    return rows, labels


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1) + 0.5))]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--server-cert', type=Path, required=True)
    p.add_argument('--private-dir', type=Path, required=True)
    p.add_argument('--library', type=Path, required=True, help='libedgefhe_v5_capi.so')
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--preprocess', type=Path, required=True)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='new JSON file')
    p.add_argument('--backend', choices=('cpu', 'fpga', 'fpga-simulated'), required=True)
    p.add_argument('--workers', type=int, default=4, help='CPU backend workers (1..64)')
    p.add_argument('--fd', type=int, default=-1, help='operator-opened DMA descriptor (fpga only)')
    p.add_argument('--limit', type=int, default=0, help='first N records (0 = all)')
    p.add_argument('--batch-records', type=int, default=SLOTS)
    p.add_argument('--show', type=int, default=5, help='print this many local decisions')
    a = p.parse_args()
    require(not a.output.exists(), 'Use a new --output file')
    require(1 <= a.batch_records <= SLOTS, 'Batch size must be 1..2048 records')
    if a.backend == 'fpga':
        require(a.fd >= 0, '--backend fpga needs the operator-opened --fd')
        # Read-only check, as in tools/run_comparison.py: never open the device here.
        target = os.readlink(f'/proc/self/fd/{a.fd}')
        require(target == '/dev/edgefhe_rns3_n4096_dma', f'--fd {a.fd} points to {target}, not the EdgeFHE DMA device')
        fd = a.fd
    else:
        require(a.fd == -1, '--fd is only for --backend fpga')
        fd = -2 if a.backend == 'fpga-simulated' else -1
    workers = a.workers if a.backend == 'cpu' else 1

    preprocess = load_json(a.preprocess.read_text(encoding='utf-8'))
    model = load_json(a.model.read_text(encoding='utf-8'))
    require(preprocess['model_id'] == model['model_id'], 'Preprocessing belongs to a different model')
    require(0 < preprocess['clip'] <= model['input_abs_max'], 'Clip outside the model input bound')
    records, labels = read_records(a.data, preprocess, a.limit)
    plain = [sum(w * v for w, v in zip(model['weights'], z)) + model['bias'] for z in records]

    config = load_json(a.config.read_text(encoding='utf-8'))
    started = time.perf_counter_ns()
    with Native(a.library, workers, fd) as native:
        native.lib.ef_backend_name.argtypes = [C.c_void_p]
        native.lib.ef_backend_name.restype = C.c_char_p
        backend = native.lib.ef_backend_name(native.context).decode()
        version = native.lib.ef_version().decode()
        t = time.perf_counter_ns()
        session = Session(native, Client(config, a.server_cert, a.private_dir), a.model)
        setup_ns = time.perf_counter_ns() - t
        batches, logits, classes = [], [], []
        for begin in range(0, len(records), a.batch_records):
            batch = records[begin:begin + a.batch_records]
            result = session.run(batch)
            logits += [r['logit'] for r in result['results']]
            classes += [r['class'] for r in result['results']]
            batches.append({'records': len(batch), 'hot_path_ns': result['session_hot_path_ns'],
                            **{k: result['stage_times_ns'][k] for k in PHASES}})
            print(f'batch {len(batches):3d}: {len(batch)} records, '
                  f'{result["session_hot_path_ns"] / 1e6:.1f} ms', flush=True)
    total_ns = time.perf_counter_ns() - started

    diffs = [abs(c - q) for c, q in zip(logits, plain)]
    within = all(d <= session.bound for d in diffs)
    agree = sum(c is not None and c == int(q > 0) for c, q in zip(classes, plain))
    disagree = sum(c is not None and c != int(q > 0) for c, q in zip(classes, plain))
    uncertain = sum(c is None for c in classes)
    for i in range(min(a.show, len(records))):
        print(f'record {i}: logit {logits[i]:+.6f} class {classes[i]}'
              + (f' label {labels[i]}' if labels else ''), flush=True)
    hot = [b['hot_path_ns'] for b in batches]
    report = {
        'schema': 'edgefhe-v5-two-machine-1',
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'backend': backend, 'library_version': version,
        'server_id': config['server_id'], 'server_url': config['server_url'], 'mtls': config['mtls'],
        'records': len(records), 'batches': len(batches), 'batch_records': a.batch_records,
        'model_id': model['model_id'], 'error_bound': session.bound,
        'accuracy': {'max_abs_logit_diff': max(diffs), 'all_within_bound': within,
                     'agree': agree, 'disagree': disagree, 'uncertain': uncertain},
        'timing': {'setup_ms': setup_ns / 1e6, 'total_ms': total_ns / 1e6,
                   'records_per_s': len(records) / (total_ns / 1e9),
                   'hot_path_p50_ms': percentile(hot, 0.5) / 1e6, 'hot_path_p95_ms': percentile(hot, 0.95) / 1e6,
                   'phase_total_ms': {k[:-3] + '_ms': sum(b[k] for b in batches) / 1e6 for k in PHASES}},
        'per_batch': batches,
        'environment': {'machine': platform.machine(), 'system': platform.platform(), 'python': platform.python_version()},
        'privacy': 'aggregates and timings only; decrypted records are not written',
    }
    ok = within and disagree == 0
    report['status'] = 'V5_TWO_MACHINE_CLIENT_PASS' if ok else 'V5_TWO_MACHINE_CLIENT_FAIL'
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x', encoding='utf-8') as f:
        json.dump(report, f, indent=2, allow_nan=False)
        f.write('\n')
    print(f'{report["status"]} records={len(records)} backend={backend} '
          f'max_diff={max(diffs):.2e} records_per_s={report["timing"]["records_per_s"]:.1f}', flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
