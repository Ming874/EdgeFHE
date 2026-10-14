"""Compile a fresh kernel module in a private output; never load/bind the module."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def validate_arm64_module(path):
    data = path.read_bytes()
    if len(data) < 64 or data[:6] != b'\x7fELF\x02\x01':
        raise RuntimeError('Expected a 64-bit little-endian ELF kernel module.')
    elf_type, machine = struct.unpack_from('<HH', data, 16)
    if elf_type != 1 or machine != 183:
        raise RuntimeError('Expected an aarch64 relocatable module; refuse a different ELF architecture.')
    return {'ELF_class': 'ELF64', 'ELF_byte_order': 'little-endian', 'ELF_machine': machine, 'ELF_type': elf_type}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--kernel-build', type=Path,
                        default=Path('/lib/modules') / platform.release() / 'build')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Build natively on the target Linux board; this script does not cross-compile.')
    if platform.machine() != 'aarch64':
        raise RuntimeError('KV260 native build requires aarch64; an x86 module cannot be deployed to KV260.')
    headers = args.kernel_build.resolve()
    if not (headers / 'Makefile').is_file():
        raise RuntimeError('Matching running-kernel headers missing: '+str(headers))
    for name in ('make', 'modinfo'):
        if not shutil.which(name):
            raise RuntimeError('Required OS tool missing: '+name)
    frozen = json.loads((ROOT / 'release/VERSION.json').read_text())
    source = ROOT / 'software/driver/edgefhe_rns3_n4096_dma_proxy.c'
    if hashlib.sha256(source.read_bytes()).hexdigest() != frozen['driver_source_sha256']:
        raise RuntimeError('Frozen driver source identity mismatch')
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(ROOT / 'software'):
        raise RuntimeError('Choose a new output outside software/; never overwrite a previous build.')
    output.mkdir(parents=True, exist_ok=False)
    for name in ('Makefile', source.name):
        shutil.copy2(ROOT / 'software/driver' / name, output / name)
    command = ['make', '-C', str(output), 'KDIR='+str(headers)]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (output / 'compile.log').write_text(result.stdout, encoding='utf-8')
    print(result.stdout, end='')
    if result.returncode != 0:
        raise RuntimeError('Module compilation failed; preserve output/compile.log')
    module = output / 'edgefhe_rns3_n4096_dma_proxy.ko'
    identity = {'source_sha256': frozen['driver_source_sha256'], 'module_sha256': hashlib.sha256(module.read_bytes()).hexdigest(),
                'kernel_build': str(headers), 'running_kernel': platform.release(), 'architecture': platform.machine(),
                'command': command, 'module_insertions': 0, 'DMA_jobs': 0}
    identity.update(validate_arm64_module(module))
    for field in ('vermagic', 'version', 'srcversion'):
        result = subprocess.run(['modinfo', '-F', field, str(module)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        identity[field] = result.stdout.strip()
    (output / 'module_identity.json').write_text(json.dumps(identity, indent=2)+'\n', encoding='utf-8')
    if identity['vermagic'].split()[0] != platform.release() or identity['version'] != frozen['driver_version']:
        raise RuntimeError('Module metadata mismatch; this build is not accepted for loading.')
    print('DRIVER_NATIVE_COMPILE_METADATA_PASS no_insmod no_DMA; live identity/probe acceptance still required')


if __name__ == '__main__':
    main()
