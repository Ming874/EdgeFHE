"""Board runner for the two-group comparison (docs/COMPARISON.md), stages 1 and 2.

  Stage 1, CPU only (any Linux, including the KV260; no device):
    python3 tools/run_comparison.py --stage 1 --output build/comparison/stage1_01 \\
        --data data/cdc_diabetes/splits/test.csv --model training/outputs/model.json \\
        --preprocess training/outputs/edge_preprocess.json --cpus 0-3
  Stage 2, after the operator's live gates (docs/BOARD_GUIDE.md); the operator opens
  the device and this script only inherits the descriptor:
    exec 3<>/dev/edgefhe_rns3_n4096_dma
    python3 tools/run_comparison.py --stage 2 --fd 3 --output build/comparison/stage2_01 ...
    exec 3>&-
  Rehearsal of stage 2 on any host: --stage 2 --simulated (FPGA rows on the simulated device).

Each stage builds the V5 tools with the same flags for both groups, runs the host sanity
checks, then v5_e2e on the dataset, then compare_results.py. Everything is kept in the new
output directory: logs, binaries' SHA-256, environment inventory, results and the report.
This script never opens, resets or retries the device. With --fd it checks through
/proc/self/fd that the inherited descriptor names the EdgeFHE DMA device, then passes it on.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import time
sys.dont_write_bytecode = True
from v5_sources import CAPI, LIBRARY, PROGRAMS, ROOT, SAMPLE, V4_CAPI, build  # noqa: E402

DEVICE = '/dev/edgefhe_rns3_n4096_dma'
CPU_BACKENDS = 'cpu-w1,cpu-w2,cpu-w3,cpu-w4'
FPGA_BACKENDS = 'fpga-serial,fpga-overlap'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_descriptor(fd):
    """Read-only: the inherited descriptor must be the gated EdgeFHE character device."""
    target = os.readlink(f'/proc/self/fd/{fd}')
    if target != DEVICE:
        raise SystemExit(f'--fd {fd} points to {target}, not {DEVICE}; open it with: exec {fd}<>{DEVICE}')
    if not stat.S_ISCHR(os.fstat(fd).st_mode):
        raise SystemExit(f'--fd {fd} is not a character device')
    return target


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--stage', type=int, choices=(1, 2), required=True)
    p.add_argument('--output', type=Path, required=True, help='new directory')
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--preprocess', type=Path, required=True)
    p.add_argument('--fd', type=int, help='stage 2: operator-opened descriptor of ' + DEVICE)
    p.add_argument('--simulated', action='store_true', help='stage 2 rehearsal without a device')
    p.add_argument('--cpus', default='', help='pin every measurement to these CPUs, e.g. 0-3')
    p.add_argument('--rounds', type=int, default=30)
    p.add_argument('--warmup', type=int, default=3)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--capacity', default='0,2', help='pipeline queue capacities; 0 = serial')
    p.add_argument('--encode-workers', type=int, choices=(1, 2, 3, 4), default=1,
                   help='step 2 threads, the same for both groups (1 = features one after another)')
    p.add_argument('--stream-batches', default='1,2,4,8,all')
    p.add_argument('--limit', type=int, default=0, help='first N records only (quick runs)')
    p.add_argument('--cxxflags', default='-O2', help='same compiler flags for both groups')
    p.add_argument('--jobs', type=int, default=4)
    p.add_argument('--no-werror', action='store_true',
                   help='only if this compiler version turns a new warning into a build error')
    p.add_argument('--bin', type=Path, help='reuse binaries from an earlier run of this script (same sources)')
    p.add_argument('--label', default='')
    a = p.parse_args()
    if platform.system() != 'Linux':
        raise SystemExit('Run on Linux (the KV260) or WSL')
    if a.stage == 2 and (a.fd is None) == (not a.simulated):
        raise SystemExit('Stage 2 needs exactly one of --fd N (board) or --simulated (rehearsal)')
    if a.stage == 1 and (a.fd is not None or a.simulated):
        raise SystemExit('Stage 1 is CPU-only: no --fd / --simulated')
    out = a.output.resolve()
    if out.exists():
        raise SystemExit('Use a new --output directory; earlier results are evidence')
    descriptor = check_descriptor(a.fd) if a.fd is not None else None
    out.mkdir(parents=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    report = {'schema': 'edgefhe-v5-stage-1', 'stage': a.stage, 'label': a.label, 'status': 'RUNNING',
              'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'device': descriptor or ('simulated' if a.simulated else None), 'device_opened_by_script': False,
              'inputs': {k: {'path': str(v.resolve()), 'sha256': sha(v)}
                         for k, v in (('data', a.data), ('model', a.model), ('preprocess', a.preprocess))},
              'settings': {k: getattr(a, k) for k in ('cpus', 'rounds', 'warmup', 'repeats', 'capacity',
                                                     'stream_batches', 'encode_workers', 'limit', 'cxxflags',
                                                     'no_werror')},
              'source_sha256': {Path(f).relative_to(ROOT).as_posix(): sha(f)
                                for f in sorted({*LIBRARY, *PROGRAMS.values(), CAPI, *V4_CAPI})},
              'dma_policy': {'timeout_ms_per_request': 5000, 'retry': False, 'reset': False,
                             'stop_on_first_failure': True, 'requests_in_flight': 1},
              'steps': []}

    def run(name, command, marker=None, keep_fd=False, capture=None):
        started = time.monotonic()
        result = subprocess.run([str(c) for c in command], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, pass_fds=(a.fd,) if keep_fd and a.fd is not None else ())
        (out / f'{name}.log').write_text(result.stdout, encoding='utf-8')
        ok = result.returncode == 0 and (marker is None or marker in result.stdout)
        report['steps'].append({'name': name, 'exit': result.returncode, 'marker': marker, 'pass': ok,
                                'seconds': round(time.monotonic() - started, 1)})
        print(('PASS ' if ok else 'FAIL ') + name, flush=True)
        if not ok:
            print(result.stdout[-4000:])
            raise SystemExit(f'Step {name} failed; see {out / (name + ".log")}')
        return result.stdout

    try:
        target = 'kv260' if platform.machine() == 'aarch64' else 'host'
        subprocess.run([sys.executable, ROOT / 'tools/check_environment.py', '--target', target,
                        '--output', out / 'environment.json'], env=env, stdout=subprocess.DEVNULL)
        if a.bin:
            names = [*PROGRAMS, 'libedgefhe_v5_capi.so', 'libedgefhe_native_v4.so']
            paths = {n: a.bin.resolve() / n for n in names}
            missing = [n for n, path in paths.items() if not path.is_file()]
            if missing:
                raise SystemExit('--bin lacks ' + ', '.join(missing))
            paths['capi'], paths['v4_capi'] = paths.pop('libedgefhe_v5_capi.so'), paths.pop('libedgefhe_native_v4.so')
            report['binaries_reused_from'] = str(a.bin.resolve())
        else:
            print('building V5 tools ...', flush=True)
            paths = build(out / 'bin', a.cxxflags.split(), jobs=a.jobs, capi=True, werror=not a.no_werror)
        report['binaries_sha256'] = {k: sha(v) for k, v in paths.items()}
        run('unit_tests', [paths['v5_tests'], SAMPLE], 'V5_TESTS_PASS')
        run('parity_simulated', [paths['v5_parity'], '--cases', '16'], 'V5_PARITY_PASS device=simulated-dma')
        if a.fd is not None:
            run('parity_board', [paths['v5_parity'], '--fd', a.fd, '--cases', '64'], 'V5_PARITY_PASS device=linux-dma',
                keep_fd=True)
        backends = CPU_BACKENDS if a.stage == 1 else CPU_BACKENDS + ',' + FPGA_BACKENDS
        result = out / ('e2e_cpu.json' if a.stage == 1 else 'e2e_all.json')
        command = [paths['v5_e2e'], '--data', a.data, '--model', a.model, '--preprocess', a.preprocess,
                   '--output', result, '--backends', backends, '--rounds', a.rounds, '--warmup', a.warmup,
                   '--repeats', a.repeats, '--capacity', a.capacity, '--stream-batches', a.stream_batches,
                   '--encode-workers', a.encode_workers, '--label', f'stage{a.stage} {a.label}'.strip()]
        if a.cpus:
            command += ['--cpus', a.cpus]
        if a.limit:
            command += ['--limit', a.limit]
        if a.fd is not None:
            command += ['--fd', a.fd]
        run('e2e', command, 'V5_E2E_PASS', keep_fd=True)
        run('compare', [sys.executable, ROOT / 'tools/compare_results.py', result, '--output', out / 'comparison'],
            'COMPARISON_WRITTEN')
        report['results'] = {'e2e': str(result), 'comparison': str(out / 'comparison.md')}
        report['status'] = f'V5_STAGE{a.stage}_COMPLETE' + ('_SIMULATED_DEVICE' if a.simulated else '')
    except BaseException as e:
        report['status'] = 'FAIL'
        report['failure'] = repr(e)
        raise
    finally:
        report['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        (out / 'stage_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(report['status'], 'output=' + str(out), flush=True)


if __name__ == '__main__':
    main()
