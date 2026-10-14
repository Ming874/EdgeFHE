"""Read-only environment inventory. No device, networking or system configuration."""
import argparse
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True


def collect(target):
    linux = sys.platform.startswith('linux')
    machine = platform.machine()
    checks = {'python_3_10_or_newer': sys.version_info >= (3, 10)}
    command_names = ['g++', 'cmake', 'make']
    if target == 'kv260':
        command_names += ['dtc', 'fdtoverlay', 'xmutil', 'modinfo', 'openssl']
        checks['linux_aarch64'] = linux and machine == 'aarch64'
    paths = {name: shutil.which(name) for name in command_names}
    if linux or target == 'kv260':
        checks.update({name+'_available': bool(path) for name, path in paths.items()})
    versions = {}
    for name in ('g++', 'cmake', 'make'):
        if paths[name]:
            result = subprocess.run([paths[name], '--version'], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
            versions[name] = result.stdout.splitlines()[0] if result.stdout else ''
            checks[name+'_version_command'] = result.returncode == 0
    if paths['cmake']:
        match = re.search(r'cmake version (\d+)\.(\d+)', versions['cmake'])
        checks['cmake_3_20_or_newer'] = bool(match and tuple(map(int, match.groups())) >= (3, 20))
    memory = {}
    kernel_build = None
    if linux:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith(('MemTotal:', 'CmaTotal:', 'CmaFree:')):
                key, value = line.split(':', 1)
                memory[key] = value.strip()
    if target == 'kv260':
        headers = Path('/lib/modules') / platform.release() / 'build'
        kernel_build = str(headers.resolve())
        checks['running_kernel_headers_makefile'] = (headers / 'Makefile').is_file()
    passed = all(checks.values())
    return {'status': 'ENVIRONMENT_PREREQUISITES_PASS' if passed else 'ENVIRONMENT_PREREQUISITES_INCOMPLETE',
            'target': target, 'platform': platform.platform(), 'machine': machine,
            'python': sys.version, 'kernel': platform.release(), 'tools': paths,
            'versions': versions, 'kernel_build': kernel_build, 'memory': memory, 'checks': checks,
            'native_build_supported': linux,
            'limits': ['Prerequisites only; not a kernel module, FPGA, DMA, CMA allocation or security acceptance.',
                       'Windows host PASS covers Python checks; native C++ requires Linux/WSL.',
                       'Other tool/kernel versions require new result identities.'],
            'board_device_opens': 0, 'network_connections': 0, 'system_configuration_writes': 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=['host', 'kv260'], required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = collect(args.target)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8', newline='\n') as file:
            json.dump(result, file, indent=2)
            file.write('\n')
    print(result['status']+' target='+args.target+' no_DMA no_network')
    for name, passed in result['checks'].items():
        print(('OK ' if passed else 'MISSING ') + name)
    return 0 if all(result['checks'].values()) else 1


if __name__ == '__main__':
    sys.exit(main())
