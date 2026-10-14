"""CPU-only six-case CKKS functional check; no device or transport connections."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from native import Backend
from profile import CASES, require
from experiment import run_case
from cloud import evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', type=Path, required=True)
    args = parser.parse_args()
    backend = Backend(args.library.resolve(), fd=None)
    for case in CASES:
        result = run_case(case, backend, lambda request: evaluate(request)[0], test_seed=20261007 + case['id'])
        require(result['status'] == 'PASS' and not result['DMA_records'], 'Host CPU path failed')
        print(f"HOST_CKKS_CASE_PASS case={case['id']} max_error={result['affine']['max_error_all_2048_slots']:.9g}", flush=True)
    require(backend.fd is None and not backend.records, 'Unexpected hardware backend')
    print('HOST_CKKS_AFFINE_REFERENCE_PASS cases=6 no_DMA no_network deterministic_host_QA_only', flush=True)


if __name__ == '__main__':
    main()
