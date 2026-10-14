"""Verify publication inventory, byte provenance, Python syntax and local doc links."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote
sys.dont_write_bytecode = True  # before the local import, so a fresh checkout stays clean
from inspect_prebuilt import identity as prebuilt_identity  # noqa: E402
from inventory import files as inventory_files  # noqa: E402
ROOT = Path(__file__).resolve().parents[1]

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    prebuilt, _ = prebuilt_identity()
    allowed_module='firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp/edgefhe_rns3_n4096_dma_proxy.ko'
    assert prebuilt['module_sha256']=='3590ae35109364c6d97bc7c8cb2220a005f2577ec96868d1741d3a336478d84e'
    manifest = ROOT/'release/SHA256SUMS'
    expected = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        value, name = line.split('  ',1)
        assert re.fullmatch('[a-f0-9]{64}',value) and name not in expected
        p = ROOT/name
        assert not p.is_symlink() and p.resolve().is_relative_to(ROOT)
        assert p.is_file() and sha(p)==value, 'SHA mismatch/missing: '+name
        expected[name]=value
    current={p.relative_to(ROOT).as_posix() for p in inventory_files(ROOT, exclude={manifest})}
    assert current==set(expected), 'Inventory mismatch: '+str(current.symmetric_difference(expected))
    origins=json.loads((ROOT/'release/PROVENANCE.json').read_text(encoding='utf-8'))
    for item in origins['copies']:
        p=ROOT/item['package_path']
        assert sha(p)==item['sha256'] and p.stat().st_size==item['bytes'], str(p)
    syntax=links=0
    for name in sorted(current):
        p=ROOT/name
        assert p.stat().st_size<100_000_000, 'GitHub oversized file: '+name
        assert p.name not in {'join_config.json','server_key.pem','id_rsa','id_ed25519'}
        assert p.suffix not in {'.o','.pfx','.p12'} and (p.suffix!='.ko' or name==allowed_module), 'Build/secret artifact: '+name
        data=p.read_bytes()
        assert not re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data), 'Private key: '+name
        if p.suffix=='.py':
            ast.parse(data.decode('utf-8-sig'), filename=name); syntax+=1
        if p.suffix=='.md' and not name.startswith('evidence/original/'):
            text=re.sub(r'```.*?```','',data.decode('utf-8'),flags=re.S)
            for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)',text):
                if link.startswith(('http:','https:','#','mailto:')):
                    continue
                path=unquote(link.split('#')[0].strip('<>'))
                assert (p.parent/path).exists(), f'Broken link in {name}: {link}'
                links+=1
    version=json.loads((ROOT/'release/VERSION.json').read_text())
    assert version['formal_release_accepted'] is False
    assert version['driver_source_sha256']==sha(ROOT/'software/driver/edgefhe_rns3_n4096_dma_proxy.c')
    assert version['bit_bin_sha256']==sha(ROOT/'firmware/ii1_n4096/edgefhe-ii1-n4096-kv260.bit.bin')
    runtime=json.loads((ROOT/'firmware/ii1_n4096/runtime_contract_v3.json').read_text())
    assert runtime['source_sha256']==version['driver_source_sha256'] and runtime['module_version']==version['driver_version']
    assert runtime['frozen_v2_contract_sha256']==sha(ROOT/'firmware/ii1_n4096/edgefhe-runtime-contract.txt')
    exported=json.loads((ROOT/'firmware/ii1_n4096/engineering_export_manifest.json').read_text())
    assert exported['artifact_sha256']['edgefhe-ii1-n4096-kv260.bit.bin']==version['bit_bin_sha256']
    ip=json.loads((ROOT/'evidence/original/h/4/fixed-bank-20261003/openfhe_validation/ip_export_acceptance.json').read_text())
    assert ip['package_sha256']==sha(ROOT/'hardware/ip/edgefhe_local_hls_rns3_ntt_parallel_accel_n4096_1_1.zip')
    for name,value in ip['hardware_source_sha256'].items():
        assert sha(ROOT/'hardware/hls/rns3_n4096'/name.replace('\\','/'))==value, name
    matrix=json.loads((ROOT/'evidence/ACCEPTANCE_MATRIX.json').read_text(encoding='utf-8'))
    assert matrix['formal_release_accepted'] is False
    candidate=json.loads((ROOT/'release/NATIVE_CANDIDATE.json').read_text(encoding='utf-8'))
    assert candidate['profile_id']=='edgefhe-functional-sparse-n4096-v4-s29'
    assert candidate['input_scale_bits']==29 and candidate['affine_output_scale_bits']==58
    assert candidate['security_bits'] is None and candidate['acceptance']['formal_release'] is False
    assert candidate['acceptance']['aarch64_native'] is False and candidate['acceptance']['V4_native_to_PL'] is False
    for name,digest in candidate['native_math_source_sha256'].items():
        assert sha(ROOT/'software/native_ckks'/name)==digest, 'Candidate source identity: '+name
    for name,digest in candidate['receipt_sha256'].items():
        assert sha(ROOT/'release/native_v4'/name)==digest, 'Candidate receipt identity: '+name
    math=json.loads((ROOT/'release/native_v4/math_acceptance.json').read_text())
    assert math['status']=='NATIVE_LOCAL_MATHEMATICAL_ACCEPTANCE_PASS'
    assert math['dense']['distinct_cases']==1000 and math['dense']['exact_coefficients']==12288000
    for name,digest in math['source_identities'].items():
        assert sha(ROOT/name)==digest, 'Math receipt/source: '+name
    standard=json.loads((ROOT/'release/native_v4/openfhe_acceptance.json').read_text())
    assert sha(ROOT/'validation/openfhe_client/baseline.cpp')==standard['source_sha256']
    assert standard['status']=='OPENFHE_STANDARD_CPU_1_3_4_WORKER_ACCEPTANCE_PASS'
    assert set(standard['benchmark'])=={'1','3','4'}
    assert all(item['max_slot_error']<=5e-4 for item in standard['benchmark'].values())
    # V5 host receipt: a PASS claim must match the current V5 sources and cover all of them.
    v5=json.loads((ROOT/'release/native_v5/local_acceptance.json').read_text(encoding='utf-8'))
    assert v5['status']=='V5_LOCAL_HOST_CHECKS_PASS' and v5['board_DMA_operations']==0 and v5['device_opened'] is False
    rerun='; rerun tools/run_v5_checks.py --receipt release/native_v5/local_acceptance.json'
    for name,digest in v5['source_sha256'].items():
        assert sha(ROOT/name)==digest, 'V5 source changed since its receipt: '+name+rerun
    v5_sources={p.relative_to(ROOT).as_posix() for p in (ROOT/'software/native_v5').rglob('*')
                if p.is_file() and p.suffix!='.md' and '__pycache__' not in p.parts}|{'tools/v5_sources.py','tools/run_v5_checks.py'}
    assert v5_sources<=set(v5['source_sha256']), 'V5 sources without a receipt: '+str(sorted(v5_sources-set(v5['source_sha256'])))+rerun
    print(f'REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS files={len(expected)} exact_copies={len(origins["copies"])} Python={syntax} links={links} v5_sources={len(v5_sources)}')

if __name__=='__main__':
    main()
