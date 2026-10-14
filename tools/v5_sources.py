"""V5 source lists and a g++ build helper, shared by run_v5_checks.py (host checks) and
run_comparison.py (board stages). Linux/WSL only for building; importing is harmless.

Also a small CLI for a release build without CMake (for example the PC side of stage 3):
    python3 tools/v5_sources.py --output build/v5

LIBRARY must match software/native_v5/CMakeLists.txt; check_sync() enforces it.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
V5 = ROOT / 'software/native_v5'
V4 = ROOT / 'software/native_ckks'
LIBRARY = [V4 / 'core.cpp', *(V5 / name for name in (
    'common/json.cpp', 'common/platform.cpp', 'common/sha256.cpp',
    'runtime/thread_pool.cpp',
    'backend/backend.cpp', 'backend/factory.cpp', 'backend/cpu/cpu_backend.cpp',
    'backend/fpga/device.cpp', 'backend/fpga/fpga_backend.cpp',
    'ckks/client.cpp', 'ckks/model.cpp', 'ckks/cloud.cpp',
    'edge/dataset.cpp', 'edge/qa_random.cpp', 'edge/session.cpp', 'edge/runner.cpp'))]
PROGRAMS = {'v5_tests': V5 / 'tests/v5_tests.cpp', 'v5_parity': V5 / 'tools/v5_parity.cpp',
            'v5_bench': V5 / 'tools/v5_bench.cpp', 'v5_e2e': V5 / 'tools/v5_e2e.cpp'}
CAPI = V5 / 'capi/c_api.cpp'
V4_CAPI = [V4 / 'core.cpp', V4 / 'c_api.cpp']
SAMPLE = V5 / 'tests/data'
BASE_FLAGS = ['-std=c++17', '-Wall', '-Wextra', '-Werror', '-fno-fast-math', '-pthread']
CXX = os.environ.get('CXX', 'g++')  # one compiler and one flag set build both groups' code


def check_sync():
    """Every library .cpp under native_v5 is listed here and in CMakeLists.txt."""
    listed = {p.relative_to(V5).as_posix() for p in LIBRARY if V5 in p.parents}
    found = {p.relative_to(V5).as_posix() for p in V5.rglob('*.cpp')
             if p.relative_to(V5).parts[0] not in ('tests', 'tools', 'capi')}
    if listed != found:
        raise RuntimeError('tools/v5_sources.py LIBRARY is out of date: ' + str(sorted(listed ^ found)))
    cmake = (V5 / 'CMakeLists.txt').read_text(encoding='utf-8')
    missing = sorted(name for name in listed if name not in cmake)
    if missing:
        raise RuntimeError('CMakeLists.txt does not list ' + ', '.join(missing))


def _run(command, log):
    result = subprocess.run([str(c) for c in command], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write('$ ' + ' '.join(map(str, command)) + '\n' + result.stdout)
    if result.returncode != 0:
        raise RuntimeError('Build step failed (see log): ' + ' '.join(map(str, command[-3:])) + '\n' + result.stdout[-3000:])


def build(output, flags=(), jobs=4, capi=True, log_name='build.log', werror=True):
    """Compile the library once (in parallel), then link every program and, optionally,
    libedgefhe_v5_capi.so plus the V4 reference library. Returns {name: path}.
    werror=False drops only -Werror, for another compiler version's extra warnings."""
    check_sync()
    output = Path(output)
    objects = output / 'obj'
    objects.mkdir(parents=True, exist_ok=True)
    base = [flag for flag in BASE_FLAGS if werror or flag != '-Werror']
    include = ['-I', V5, '-I', V4]
    paths = {}
    with (output / log_name).open('w', encoding='utf-8') as log:
        def compile_one(source):
            target = objects / (source.relative_to(ROOT).as_posix().replace('/', '_')[:-4] + '.o')
            result = subprocess.run([CXX, *base, '-fPIC', *map(str, include), *flags, '-c', str(source),
                                     '-o', str(target)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return source, target, result
        with ThreadPoolExecutor(max(1, jobs)) as pool:
            compiled = list(pool.map(compile_one, LIBRARY))
        for source, _, result in compiled:
            log.write(f'$ {CXX} -c {source}\n{result.stdout}')
            if result.returncode != 0:
                raise RuntimeError(f'Compile failed: {source}\n{result.stdout[-3000:]}')
        archive = output / 'libedgefhe_v5.a'
        if archive.exists():
            archive.unlink()
        _run(['ar', 'rcs', archive, *[target for _, target, _ in compiled]], log)
        for name, source in PROGRAMS.items():
            _run([CXX, *base, *include, *flags, source, archive, '-o', output / name], log)
            paths[name] = output / name
        if capi:
            _run([CXX, *base, *include, *flags, '-shared', '-fPIC', CAPI, archive,
                  '-o', output / 'libedgefhe_v5_capi.so'], log)
            _run([CXX, *base, '-I', V4, *flags, '-shared', '-fPIC', *V4_CAPI,
                  '-o', output / 'libedgefhe_native_v4.so'], log)
            paths['capi'] = output / 'libedgefhe_v5_capi.so'
            paths['v4_capi'] = output / 'libedgefhe_native_v4.so'
    return paths


def main():
    parser = argparse.ArgumentParser(description='Release build of the V5 library, tools and C ABI '
                                                 '(compiler from $CXX, default g++).')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cxxflags', default='-O2')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--no-werror', action='store_true', help='keep warnings as warnings')
    args = parser.parse_args()
    paths = build(args.output, args.cxxflags.split(), jobs=args.jobs, capi=True, werror=not args.no_werror)
    for name, path in paths.items():
        print(f'{name}: {path}')
    print('V5_BUILD_DONE')


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
