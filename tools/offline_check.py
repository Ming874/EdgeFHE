"""CPU-only functional verification; no network, device open or FPGA operations."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.dont_write_bytecode=True
from verify_repository import main as verify
ROOT=Path(__file__).resolve().parents[1]
BUILD=ROOT/'build/offline_checks'

def run(args, marker=None):
    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    result=subprocess.run([str(x) for x in args], cwd=ROOT, env=env, text=True,
                          encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout,end='',flush=True)
    assert result.returncode==0, 'Command failed: '+str(args)
    if marker:
        assert marker in result.stdout, 'Explicit PASS marker missing: '+marker

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--native',action='store_true');args=parser.parse_args()
    print('Python '+sys.version.replace('\n',' '),flush=True)
    verify()
    BUILD.mkdir(parents=True,exist_ok=True)
    # Original auditors write their JSON beside the script: give them private copies.
    hls=BUILD/'bank_audit'; shutil.copytree(ROOT/'hardware/hls/rns3_n4096',hls,dirs_exist_ok=True)
    run([sys.executable,hls/'audit_addresses.py'],'Address/math audit: PASS')
    bank=json.loads((hls/'address_math_audit.json').read_text())
    assert bank['passed'] and bank['batches']==6144
    print('ADDRESS_CONSTANTS_AND_BANK_AUDIT_PASS',flush=True)
    vec=BUILD/'vector_audit';shutil.copytree(ROOT/'validation/vectors',vec,dirs_exist_ok=True)
    run([sys.executable,vec/'verify_vectors.py'],'Independent OpenFHE V1 vector verification: PASS')
    report=json.loads((vec/'vector_manifest.json').read_text())
    assert report['cases']==16 and report['independent_coefficient_comparisons']==196608
    frozen=json.loads((ROOT/'validation/vectors/vector_manifest.json').read_text())
    assert report==frozen, 'Independent vector manifest changed'
    print('OPENFHE_PUBLIC_PYTHON_ORACLE_PASS cases=16 coefficients=196608',flush=True)
    print('OFFLINE_PYTHON_CHECKS_PASS no_DMA',flush=True)
    if not args.native:
        return
    assert sys.platform.startswith('linux') and shutil.which('g++'), '--native requires Linux/WSL g++'
    run(['g++','--version'])
    flags=['g++','-std=c++17','-O3','-Wall','-Wextra','-Werror','-pthread']
    sources=[('software/reference/paired_public_vectors.cpp','paired_public_vectors'),
             ('software/reference/mixed_runner.cpp','mixed_runner'),
             ('software/board_tests/soak_runner.cpp','soak_runner'),
             ('software/board_tests/concurrent_runner.cpp','concurrent_runner'),
             ('software/board_tests/usercopy_runner.cpp','usercopy_runner')]
    for source,name in sources:
        run(flags+[ROOT/source,'-o',BUILD/name]); print('NATIVE_COMPILE_PASS '+name,flush=True)
    run([BUILD/'paired_public_vectors',ROOT/'validation/vectors/vectors.bin','--cpu-check'], 'CPU_OPENFHE_EXACT_PASS cases=16 coefficients=196608')
    run([BUILD/'mixed_runner',ROOT/'validation/vectors/mixed_vectors.bin','--cpu-check'],'MIXED_CPU_ORACLE_PASS cases=25 coefficients=307200')
    lib=BUILD/'libedgefhe_backend.so'
    run(flags+['-shared','-fPIC',ROOT/'software/ckks/backend.cpp','-o',lib])
    run([sys.executable,ROOT/'software/ckks/local_reference.py','--library',lib], 'HOST_CKKS_AFFINE_REFERENCE_PASS cases=6')
    print('OFFLINE_NATIVE_CHECKS_PASS no_DMA no_network board_runners_compile_only',flush=True)

if __name__=='__main__':
    main()
