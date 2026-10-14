"""Host checks for V5 (backends, pipeline, edge harness, C ABI). Linux or WSL; never opens
the DMA device and uses no network beyond 127.0.0.1.

Builds with g++ directly (CMake not required) in three configurations: optimized,
AddressSanitizer+UBSan and ThreadSanitizer. Each runs the unit tests, the simulated-device
parity tool and the end-to-end harness on the synthetic sample. The optimized build also
runs the C ABI test against the V4 library and the two-machine loopback rehearsal (mutual
TLS on 127.0.0.1; skipped and recorded when the openssl CLI is missing). With --receipt, a
passing run writes the source identities so verify_repository.py rejects stale V5 claims.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
sys.dont_write_bytecode = True
from v5_sources import CXX, ROOT, V4, V5, SAMPLE, build  # noqa: E402

BUILDS = {'release': ['-O2'],
          'asan_ubsan': ['-O1', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer'],
          'tsan': ['-O1', '-g', '-fsanitize=thread']}
TEST_MARKER = 'V5_TESTS_PASS tests=25'
PARITY_MARKER = 'V5_PARITY_PASS device=simulated-dma'
E2E_MARKER = 'V5_E2E_PASS'
CAPI_MARKER = 'V5_CAPI_PASS contexts=4'
LOOPBACK_MARKER = 'V5_TWO_MACHINE_LOOPBACK_PASS'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identities():
    """Every V5 source, script and sample file (not the .md docs), the V4 code it reuses and these tools."""
    files = [p for p in V5.rglob('*') if p.is_file() and p.suffix != '.md' and '__pycache__' not in p.parts]
    files += [V4 / 'core.cpp', V4 / 'core.hpp', V4 / 'c_api.cpp', Path(__file__).resolve(),
              ROOT / 'tools/v5_sources.py']
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(files)}


def e2e_sample(program, output):
    return [program, '--data', SAMPLE / 'sample_records.csv', '--preprocess', SAMPLE / 'sample_preprocess.json',
            '--model', SAMPLE / 'sample_model.json', '--output', output,
            '--backends', 'cpu-w1,cpu-w3,fpga-serial,fpga-overlap', '--batch-records', '128',
            '--rounds', '2', '--warmup', '1', '--repeats', '1', '--capacity', '0,2', '--qa-seed', '7', '--power', 'off']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True, help='new directory for logs and binaries')
    parser.add_argument('--receipt', type=Path, help='write the repository receipt here when every check passes')
    parser.add_argument('--skip-tsan', action='store_true', help='only if the kernel cannot run ThreadSanitizer')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if platform.system() != 'Linux':
        raise RuntimeError('Run in Linux or WSL')
    if not shutil.which(CXX) or not shutil.which('ar'):
        raise RuntimeError(f'{CXX} and ar (binutils) are required')
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Use a new output directory')
    output.mkdir(parents=True)
    before = identities()
    report = {'status': 'RUNNING', 'environment': platform.uname()._asdict(), 'board_DMA_operations': 0,
              'device_opened': False, 'network': '127.0.0.1 loopback only', 'started_ns': time.time_ns(), 'steps': []}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')

    def run(name, command, marker=None, prefix=()):
        result = subprocess.run([*map(str, prefix), *map(str, command)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)
        (output / (name + '.log')).write_text(result.stdout, encoding='utf-8')
        report['steps'].append({'name': name, 'exit': result.returncode, 'marker': marker})
        if result.returncode != 0 or (marker and marker not in result.stdout):
            print(result.stdout[-4000:], end='')
            raise RuntimeError('Step failed: ' + name + ' (log kept in ' + str(output) + ')')
        print('PASS', name, flush=True)
        return result.stdout

    try:
        report['compiler'] = run('compiler_version', [CXX, '--version']).splitlines()[0]
        # TSan needs a low mmap randomization; setarch -R disables ASLR for that process only.
        tsan_prefix = ['setarch', platform.machine(), '-R'] if shutil.which('setarch') else []
        for name, flags in BUILDS.items():
            if name == 'tsan' and args.skip_tsan:
                report['steps'].append({'name': 'tsan', 'skipped': True})
                continue
            paths = build(output / name, flags, jobs=args.jobs, capi=name == 'release')
            report['steps'].append({'name': name + '_build', 'exit': 0})
            print('PASS', name + '_build', flush=True)
            prefix = tsan_prefix if name == 'tsan' else []
            run(f'{name}_tests', [paths['v5_tests'], SAMPLE], TEST_MARKER, prefix)
            run(f'{name}_parity', [paths['v5_parity'], '--cases', '32'], PARITY_MARKER, prefix)
            run(f'{name}_parity_serial', [paths['v5_parity'], '--cases', '8', '--no-overlap'], PARITY_MARKER, prefix)
            run(f'{name}_e2e_sample', e2e_sample(paths['v5_e2e'], '-'), E2E_MARKER, prefix)
            if name == 'release':
                run('release_capi', [sys.executable, V5 / 'tests/capi_test.py', '--library', paths['capi'],
                                     '--v4-library', paths['v4_capi']], CAPI_MARKER)
                if shutil.which('openssl'):
                    run('release_two_machine_loopback', [sys.executable, V5 / 'tests/two_machine_loopback.py',
                                                         '--library', paths['capi'], '--output', output / 'loopback'],
                        LOOPBACK_MARKER)
                else:
                    report['steps'].append({'name': 'release_two_machine_loopback', 'skipped': 'no openssl CLI'})
                bench = run('release_bench_simulated', [paths['v5_bench'], '--rounds', '10', '--warmup', '2'])
                report['bench_simulated'] = json.loads(bench)
        if identities() != before:
            raise RuntimeError('Sources changed while checks were running')
        report['status'] = 'V5_LOCAL_HOST_CHECKS_PASS'
        report['source_sha256'] = before
        report['scope'] = ('x86/aarch64 host: unit tests, simulated-device parity, end-to-end harness on synthetic '
                           'data, C ABI against V4, mTLS loopback, ASan/UBSan, TSan. Not board acceptance: no real '
                           'DMA, no PL parity, simulated timings are not FPGA timings.')
        print(report['status'] + ' board_DMA=0', flush=True)
    except Exception as e:
        report['status'] = 'FAIL'
        report['failure'] = repr(e)
        raise
    finally:
        report['finished_ns'] = time.time_ns()
        (output / 'v5_check_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if args.receipt:
        receipt = {k: report[k] for k in ('status', 'scope', 'compiler', 'board_DMA_operations', 'device_opened',
                                          'source_sha256')}
        receipt['environment'] = {k: report['environment'][k] for k in ('system', 'machine', 'release')}
        receipt['checks'] = [s['name'] for s in report['steps'] if not s.get('skipped')]
        receipt['skipped'] = [s['name'] for s in report['steps'] if s.get('skipped')]
        receipt['tsan'] = 'skipped' if args.skip_tsan else 'passed'
        receipt['date_utc'] = time.strftime('%Y-%m-%d', time.gmtime())
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8', newline='\n')
        print('V5_RECEIPT_WRITTEN', args.receipt)


if __name__ == '__main__':
    main()
