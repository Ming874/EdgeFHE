"""Turn v5_e2e result files into the group A vs group B comparison (docs/COMPARISON.md).

  python3 tools/compare_results.py build/comparison/stage2_01/e2e_all.json [more.json ...] \\
      --output build/comparison/stage2_01/comparison

Writes <output>.md (tables for the report and slides) and <output>.json (the same numbers
for the dashboard). Checks the fairness rules first: same records, model, preprocessing,
CKKS profile, machine, CPU pinning, randomness source and measurement settings. Rows are
labelled "<folder>/<file>" whenever several files contain the same backend. FPGA rows from
the simulated device are never mixed into "best B" when real-board rows exist.
Standard library only; inputs are read-only.
"""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
SAME_INPUTS = ('data_sha256', 'model_sha256', 'preprocess_sha256', 'records')
SAME_SETTINGS = ('batch_records', 'rounds', 'warmup', 'repeats', 'cloud_workers', 'encode_workers')
SETTING_DEFAULTS = {'encode_workers': 1}  # files written before the option existed encoded serially


def setting(run, key):
    return run['settings'].get(key, SETTING_DEFAULTS.get(key))


def group(backend):
    return 'A' if backend.startswith('cpu-') else 'B' if backend.startswith('fpga-') else '?'


def ms(us):
    return None if us is None else us / 1e3


def fmt(value, digits=2):
    if value is None:
        return '–'
    if isinstance(value, bool):
        return 'yes' if value else 'NO'
    if isinstance(value, int):
        return f'{value:,}'
    return f'{value:,.{digits}f}'


def table(headers, rows):
    lines = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join('---' for _ in headers) + '|']
    lines += ['| ' + ' | '.join(c if isinstance(c, str) else fmt(c) for c in row) + ' |' for row in rows]
    return '\n'.join(lines)


def load(paths):
    runs = []
    for path in paths:
        path = Path(path)
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema') != 'edgefhe-v5-e2e-1':
            raise SystemExit(f'{path}: not a v5_e2e result')
        if data.get('final_status') != 'V5_E2E_PASS':
            raise SystemExit(f'{path}: run did not pass ({data.get("final_status")}): {data.get("failure", data.get("notes"))}')
        data['_file'] = f'{path.resolve().parent.name}/{path.name}'
        data['_real'] = bool(data['device']['device_times_are_fpga'])
        runs.append(data)
    return runs


def governors(run):
    return sorted({f['governor'] for f in run['environment'].get('cpufreq', [])})


def fairness(runs):
    problems, first = [], runs[0]
    for run in runs[1:]:
        for key in SAME_INPUTS:
            if run['inputs'][key] != first['inputs'][key]:
                problems.append(f'inputs.{key} differs: {run["_file"]}')
        for key in SAME_SETTINGS:
            if setting(run, key) != setting(first, key):
                problems.append(f'settings.{key} differs: {run["_file"]}')
        if run['profile'] != first['profile']:
            problems.append(f'CKKS profile differs: {run["_file"]}')
        for key in ('machine', 'cpu_model', 'online_cpus', 'affinity', 'pinned'):
            if run['environment'][key] != first['environment'][key]:
                problems.append(f'environment.{key} differs: {run["_file"]}')
        if run['randomness'].split(':')[0] != first['randomness'].split(':')[0]:
            problems.append(f'randomness source differs (OS vs QA seed): {run["_file"]}')
        if governors(run) != governors(first):
            problems.append(f'cpufreq governor differs: {run["_file"]}')
    return problems


def collect(runs, sensor=None):
    correctness, latency, throughput = [], [], []
    for run in runs:
        source, real = run['_file'], run['_real']
        if 'correctness' in run:
            for item in run['correctness']['backends']:
                correctness.append({**item, 'file': source, 'real_device': real,
                                    'bit_exact_in_file': run['correctness']['bit_exact_across_backends']})
        if 'latency' in run:
            for item in run['latency']['results']:
                steps = {s['step']: s['time'] for s in item['steps']}
                latency.append({
                    'backend': item['backend'], 'file': source, 'name': item['name'], 'real_device': real,
                    'total_p50_ms': ms(item['total']['p50_us']), 'total_p95_ms': ms(item['total']['p95_us']),
                    'edge_p50_ms': ms(item['edge_only']['p50_us']), 'edge_p95_ms': ms(item['edge_only']['p95_us']),
                    'steps_p50_ms': {k: ms(v['p50_us']) for k, v in steps.items()},
                    'stats': item['backend_stats']})
        if 'throughput' in run:
            for item in run['throughput']['results']:
                readings = [r for r in item['power'] if (sensor is None or r['sensor'] == sensor)
                            and r.get('records_per_j') and r['records_per_j'] > 0]
                power = readings[0] if readings else None
                throughput.append({'backend': item['backend'], 'file': source, 'name': item['name'], 'real_device': real,
                                   'pipeline': item['pipeline'], 'capacity': item['capacity'],
                                   'batches': item['batches'], 'records': item['records'],
                                   'records_per_s': item['records_per_s'], 'cpu_utilization': item['cpu_utilization'],
                                   'mean_w': power['mean_w'] if power else None,
                                   'records_per_j': power['records_per_j'] if power else None,
                                   'sensor': power['sensor'] if power else None, 'stages': item['stages']})
    # Label rows by backend, adding the folder/file only when one backend appears in several files.
    for rows in (latency, throughput):
        files = {}
        for row in rows:
            files.setdefault(row['backend'], set()).add(row['file'])
        for row in rows:
            row['label'] = row['backend'] + (f' ({row["file"]})' if len(files[row['backend']]) > 1 else '')
    return correctness, latency, throughput


def eligible(rows, wanted):
    """Rows of one group; for group B only real-device rows when any exist."""
    chosen = [r for r in rows if group(r['backend']) == wanted]
    if wanted == 'B' and any(r['real_device'] for r in chosen):
        chosen = [r for r in chosen if r['real_device']]
    return chosen


def pick(rows, key, wanted, highest):
    candidates = [r for r in eligible(rows, wanted) if r.get(key) is not None and r[key] > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r[key]) if highest else min(candidates, key=lambda r: r[key])


def device_note(row):
    return '' if group(row['backend']) != 'B' or row['real_device'] else ' (simulated)'


def latency_section(latency, summary):
    out = ['## 2. Latency of one batch (ms, p50 unless noted)', '',
           'Edge = steps 1-5 and 9-10 on the board; the in-process cloud step is shown separately.', '']
    rows = []
    for item in latency:
        base = next((r['edge_p50_ms'] for r in latency if r['file'] == item['file'] and r['backend'] == 'cpu-w1'), None)
        steps = item['steps_p50_ms']
        rows.append([item['label'] + device_note(item), group(item['backend']), item['edge_p50_ms'], item['edge_p95_ms'],
                     steps.get('encrypt'), steps.get('decrypt'), steps.get('cloud'), item['total_p50_ms'],
                     base / item['edge_p50_ms'] if base and item['edge_p50_ms'] else None])
    out += [table(['Backend', 'Group', 'Edge p50', 'Edge p95', 'Encrypt', 'Decrypt', 'Cloud', 'Total',
                   'Speedup vs cpu-w1 (same file)'], rows), '']
    for source in dict.fromkeys(r['file'] for r in latency):
        cpu = sorted(((int(r['backend'][5:]), r) for r in latency if r['file'] == source and r['backend'].startswith('cpu-w')),
                     key=lambda x: x[0])
        if len(cpu) > 1 and cpu[0][0] == 1:
            one = cpu[0][1]['steps_p50_ms']['encrypt']
            out += [f'### Group A scaling, encrypt step ({source})', '',
                    table(['Workers', 'Encrypt p50 ms', 'Speedup', 'Efficiency'],
                          [[n, r['steps_p50_ms']['encrypt'], one / r['steps_p50_ms']['encrypt'],
                            one / r['steps_p50_ms']['encrypt'] / n] for n, r in cpu]), '']
    fpga = [r for r in latency if r['backend'].startswith('fpga-')]
    if fpga:
        rows = []
        for item in fpga:
            s, jobs = item['stats'], max(1, item['stats']['jobs'])
            rows.append([item['label'] + device_note(item), s['pack_ms'] / jobs, s['device_ms'] / jobs, s['unpack_ms'] / jobs,
                         s['driver_reported_ms'] / jobs, (s['bytes_to_device'] + s['bytes_from_device']) / jobs / 1024])
        out += ['### Group B breakdown per product (ms)', '',
                'Device call = the whole ioctl seen by the program; driver-reported = the driver\'s own DMA+PL window.', '',
                table(['Backend', 'Pack', 'Device call', 'Unpack', 'Driver-reported', 'KiB moved'], rows), '']
    a, b = pick(latency, 'edge_p50_ms', 'A', False), pick(latency, 'edge_p50_ms', 'B', False)
    if a and b:
        ratio = a['edge_p50_ms'] / b['edge_p50_ms']
        summary['latency'] = {'best_A': a['label'], 'best_A_ms': a['edge_p50_ms'], 'best_B': b['label'] + device_note(b),
                              'best_B_ms': b['edge_p50_ms'], 'B_speedup_over_A': ratio}
        verdict = 'faster' if ratio > 1 else 'slower'
        out += [f'**Latency, best A vs best B:** {a["label"]} {a["edge_p50_ms"]:.2f} ms vs {b["label"]}{device_note(b)} '
                f'{b["edge_p50_ms"]:.2f} ms → B/A speed = **{ratio:.2f}×** (B is {verdict}).', '']
    return out


def throughput_section(throughput, summary):
    def key(t):
        return (t['label'], t['batches'], t['capacity'])
    serial = {(t['label'], t['batches']): t['records_per_s'] for t in throughput if t['pipeline'] == 'serial'}
    rows = []
    for t in throughput:
        gain = (t['records_per_s'] / serial[(t['label'], t['batches'])]
                if t['pipeline'] != 'serial' and (t['label'], t['batches']) in serial else None)
        rows.append([t['label'] + device_note(t), group(t['backend']),
                     t['pipeline'] + (f' ({t["capacity"]})' if t['capacity'] else ''),
                     t['batches'], t['records_per_s'], gain, t['cpu_utilization'], t['mean_w'], t['records_per_j']])
    out = ['## 3. Throughput (stream of batches)', '',
           table(['Backend', 'Group', 'Pipeline', 'Batches', 'Records/s', 'Gain vs serial', 'CPU util', 'Mean W',
                  'Records/J'], rows), '']
    # Compare A and B at the longest stream that BOTH groups measured.
    lengths_a = {t['batches'] for t in eligible(throughput, 'A')}
    lengths_b = {t['batches'] for t in eligible(throughput, 'B')}
    common = lengths_a & lengths_b
    longest = max(common) if common else max(t['batches'] for t in throughput)
    full = [t for t in throughput if t['batches'] == longest]
    a, b = pick(full, 'records_per_s', 'A', True), pick(full, 'records_per_s', 'B', True)
    if a and b:
        ratio = b['records_per_s'] / a['records_per_s']
        summary['throughput'] = {'batches': longest, 'best_A': f'{a["label"]} {a["pipeline"]}',
                                 'best_A_records_per_s': a['records_per_s'],
                                 'best_B': f'{b["label"]}{device_note(b)} {b["pipeline"]}',
                                 'best_B_records_per_s': b['records_per_s'], 'B_over_A': ratio}
        out += [f'**Throughput, best A vs best B ({longest} batches):** {a["label"]} {a["pipeline"]} '
                f'{a["records_per_s"]:,.0f} vs {b["label"]}{device_note(b)} {b["pipeline"]} {b["records_per_s"]:,.0f} '
                f'records/s → B/A = **{ratio:.2f}×**.', '']
    ea, eb = pick(full, 'records_per_j', 'A', True), pick(full, 'records_per_j', 'B', True)
    if ea and eb:
        summary['energy'] = {'best_A_records_per_j': ea['records_per_j'], 'best_B_records_per_j': eb['records_per_j'],
                             'B_over_A': eb['records_per_j'] / ea['records_per_j'], 'sensor': ea['sensor']}
        out += [f'**Energy:** A {ea["records_per_j"]:,.1f} vs B {eb["records_per_j"]:,.1f} records/J '
                f'(sensor {ea["sensor"]}; hwmon power of that rail or the whole board, not only the CPU or FPGA).', '']
    elif not any(t['records_per_j'] for t in throughput):
        out += ['Power: no usable hwmon power readings; energy is not reported.', '']
    lengths = sorted({t['batches'] for t in throughput})
    if len(lengths) > 1:
        series = list(dict.fromkeys((t['label'], t['pipeline'], t['capacity']) for t in throughput))
        out += ['### Stream length (when does pipelining pay off?)', '',
                table(['Backend', 'Pipeline'] + [f'{k} batches' for k in lengths],
                      [[label, pipeline + (f' ({capacity})' if capacity else '')] +
                       [next((t['records_per_s'] for t in throughput if key(t) == (label, k, capacity)), None)
                        for k in lengths]
                       for label, pipeline, capacity in series]), '']
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('results', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='path prefix for .md and .json (new files)')
    parser.add_argument('--allow-unfair', action='store_true', help='report even if the fairness checks fail')
    parser.add_argument('--sensor', help='hwmon power sensor label for energy (default: the first usable one)')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # e.g. a cp1252 Windows console; files are always UTF-8
    md_path, json_path = args.output.with_suffix('.md'), args.output.with_suffix('.json')
    for path in (md_path, json_path):
        if path.exists():
            raise SystemExit(f'{path} exists: use a new --output')
    runs = load(args.results)
    problems = fairness(runs)
    if problems and not args.allow_unfair:
        raise SystemExit('Fairness check failed:\n  ' + '\n  '.join(problems))
    correctness, latency, throughput = collect(runs, args.sensor)
    has_b = any(group(r['backend']) == 'B' for r in latency + throughput + correctness)
    real = [r['_real'] for r in runs if any(group(b) == 'B' for b in r['settings']['backends'].split(','))]
    device = 'none' if not has_b else 'real' if real and all(real) else 'simulated' if not any(real) else 'mixed'
    env, inputs = runs[0]['environment'], runs[0]['inputs']

    out = ['# A/B comparison: pure software (A) vs hardware/software (B)', '']
    if not has_b:
        out += ['> **Group A only: these files contain no FPGA (group B) rows.**', '']
    elif device == 'simulated':
        out += ['> **FPGA rows come from the simulated device: their times are CPU times, not FPGA times.**', '']
    elif device == 'mixed':
        out += ['> **Real-board and simulated FPGA rows are mixed; simulated rows are marked and never chosen as best B.**', '']
    out += [f'- Records: {inputs["records"]:,} ({inputs["data"]}), model `{inputs["model_id"]}`',
            f'- Machine: {env["machine"]}, {env["cpu_model"]}, {env["online_cpus"]} CPUs, affinity {env["affinity"]}'
            f' ({"pinned" if env["pinned"] else "not pinned"}), cpufreq governor {", ".join(governors(runs[0])) or "n/a"}',
            f'- Randomness: {runs[0]["randomness"]}; cloud: {runs[0]["settings"]["cloud"]}; '
            f'step 2 encode threads: {setting(runs[0], "encode_workers")}',
            f'- Files: {", ".join(r["_file"] + ("" if r["_real"] else " [simulated device]") for r in runs)}',
            f'- Fairness checks: {"pass" if not problems else "FAILED: " + "; ".join(problems)}', '']
    summary = {'real_fpga': device == 'real', 'fpga_device': device, 'has_group_b': has_b,
               'fairness_problems': problems, 'inputs': inputs, 'environment': env,
               'encode_workers': setting(runs[0], 'encode_workers'),
               'files': [r['_file'] for r in runs]}
    if correctness:
        out += ['## 1. Correctness (gate)', '',
                table(['Backend', 'Group', 'Pass', 'Max logit diff', 'Disagree', 'Uncertain', 'Cipher SHA-256', 'File'],
                      [[c['backend'] + device_note(c), group(c['backend']), c['pass'], f'{c["max_abs_logit_diff"]:.2e}',
                        c['decisions']['disagree'], c['decisions']['uncertain'], c['cipher_sha256'][:16], c['file']]
                       for c in correctness]), '']
        exact = all(c['bit_exact_in_file'] for c in correctness)
        out += [f'Bit-exact across all backends within each run: **{"yes" if exact else "NO"}** '
                '(different runs use fresh OS randomness, so their digests differ by design).', '']
        summary['correctness'] = {'all_pass': all(c['pass'] for c in correctness), 'bit_exact': exact,
                                  'backends': sorted({c['backend'] for c in correctness})}
    if latency:
        out += latency_section(latency, summary)
    if throughput:
        out += throughput_section(throughput, summary)

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text('\n'.join(out) + '\n', encoding='utf-8')
    summary.update(correctness_rows=correctness, latency_rows=latency, throughput_rows=throughput)
    json_path.write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('\n'.join(out))
    print(f'COMPARISON_WRITTEN {md_path} {json_path}')


if __name__ == '__main__':
    main()
