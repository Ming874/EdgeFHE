"""Stage 3 rehearsal on one host: the accepted V4 service (pc_server.py) on 127.0.0.1 with
mutual TLS, and the V5 edge client with the CPU backend and with the FPGA code path on the
simulated device. Short-lived QA certificates come from the V4 helper (openssl CLI).
No board, no device, no external network. Standard library only.

  python3 software/native_v5/tests/two_machine_loopback.py --library build/.../libedgefhe_v5_capi.so \\
      --output build/v5_loopback_01
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
V5 = HERE.parent
V4 = V5.parent / 'native_ckks'
sys.path.insert(0, str(V4))
sys.path.insert(0, str(V4 / 'tests'))
from local_tls_material import prepare  # noqa: E402


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--library', type=Path, required=True, help='libedgefhe_v5_capi.so (client and server)')
    parser.add_argument('--output', type=Path, required=True, help='new directory')
    parser.add_argument('--data', type=Path, default=HERE / 'data' / 'sample_records.csv')
    parser.add_argument('--model', type=Path, default=HERE / 'data' / 'sample_model.json')
    parser.add_argument('--preprocess', type=Path, default=HERE / 'data' / 'sample_preprocess.json')
    parser.add_argument('--batch-records', type=int, default=128)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    private = out / 'private_tls'
    prepare(private)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    config = json.loads((private / 'config.json').read_text())
    config['server_url'] = f'https://127.0.0.1:{port}'
    config_path = private / 'config_loopback.json'
    config_path.write_text(json.dumps(config))
    console = out / 'server.log'
    library = args.library.resolve()
    server = None
    results = {}
    try:
        with console.open('w') as log:
            server = subprocess.Popen(
                [sys.executable, str(V4 / 'pc_server.py'), '--config', str(config_path), '--private-dir', str(private),
                 '--model', str(args.model.resolve()), '--library', str(library), '--audit', str(out / 'audit.jsonl'),
                 '--max-requests', '10000'],
                stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            for _ in range(200):
                if 'DYNAMIC_CIPHERTEXT_SERVER_READY' in console.read_text():
                    break
                require(server.poll() is None, 'server exited during start-up: ' + console.read_text()[-2000:])
                time.sleep(0.05)
            else:
                raise AssertionError('server readiness timeout')
            for backend in ('cpu', 'fpga-simulated'):
                report = out / f'client_{backend}.json'
                command = [sys.executable, str(V5 / 'python' / 'edge_client.py'), '--backend', backend,
                           '--library', str(library), '--config', str(config_path),
                           '--server-cert', str(private / 'server_ca.pem'), '--private-dir', str(private),
                           '--model', str(args.model), '--preprocess', str(args.preprocess), '--data', str(args.data),
                           '--batch-records', str(args.batch_records), '--output', str(report), '--show', '2']
                if backend == 'cpu':
                    command += ['--workers', '2']
                run = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
                (out / f'client_{backend}.log').write_text(run.stdout)
                require(run.returncode == 0 and 'V5_TWO_MACHINE_CLIENT_PASS' in run.stdout,
                        backend + ' client failed:\n' + run.stdout[-3000:])
                data = json.loads(report.read_text())
                require(data['accuracy']['all_within_bound'] and data['accuracy']['disagree'] == 0, backend + ' accuracy')
                results[backend] = {'records': data['records'], 'backend': data['backend'],
                                    'max_abs_logit_diff': data['accuracy']['max_abs_logit_diff']}
                print('PASS', backend, data['backend'], data['records'], 'records', flush=True)
        require(results['cpu']['records'] == results['fpga-simulated']['records'], 'both clients saw every record')
        for line in (out / 'audit.jsonl').read_text().splitlines():
            event = json.loads(line)
            require(set(event) <= {'event', 'time_ns', 'bind', 'port', 'mtls', 'model_sha256', 'count', 'records'},
                    'unexpected server audit field')
    finally:
        if server and server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
    (out / 'loopback_report.json').write_text(json.dumps({'status': 'V5_TWO_MACHINE_LOOPBACK_PASS', 'port': port,
                                                          'mtls': config['mtls'], 'clients': results}, indent=2) + '\n')
    print(f'V5_TWO_MACHINE_LOOPBACK_PASS clients=2 mtls={config["mtls"]} board=0', flush=True)


if __name__ == '__main__':
    main()
