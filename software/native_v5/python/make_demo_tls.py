"""Mutual-TLS material for the two-machine demo (stage 3), split by machine.

    python3 software/native_v5/python/make_demo_tls.py --server-host 192.168.1.20 --output local/demo_tls_01

Creates, with the openssl command line tool:
  server/  for the PC (cloud): server_cert.pem, server_key.pem, client_ca.pem, config.json
  client/  for the KV260 (edge): server_ca.pem, client_cert.pem, client_key.pem, config.json
  ca/      the two CA keys; keep them off both machines' working folders
Both configs share a fresh 256-bit token and pin the server certificate's DER SHA-256; the
server certificate's SAN lists every --server-host. Keys are created with umask 077 and
are short-lived (--days). Keep everything under local/ (never committed or packed).
Standard library only; no network.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess

PROTOCOL = 'edgefhe-dynamic-affine-tls-v4'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--server-host', action='append', required=True, help='IP or DNS name of the PC (repeatable)')
    p.add_argument('--port', type=int, default=18087)
    p.add_argument('--output', type=Path, required=True, help='new directory, for example local/demo_tls_01')
    p.add_argument('--server-id', default='edgefhe-pc-demo')
    p.add_argument('--days', type=int, default=30)
    a = p.parse_args()
    if a.output.exists():
        raise SystemExit('Use a new --output directory')
    if not shutil.which('openssl'):
        raise SystemExit('The openssl command line tool is required')
    if not 1 <= a.port <= 65535 or not 1 <= a.days <= 365:
        raise SystemExit('Port must be 1..65535 and --days 1..365')
    san = []
    for host in a.server_host:
        try:
            san.append('IP:' + str(ipaddress.ip_address(host)))
        except ValueError:
            if not host.replace('-', '').replace('.', '').isalnum():
                raise SystemExit('Invalid host name: ' + host)
            san.append('DNS:' + host)
    url_host = a.server_host[0]
    if ':' in url_host:
        url_host = f'[{url_host}]'

    old_mask = os.umask(0o077)
    try:
        root = a.output.resolve()
        for name in ('ca', 'server', 'client', 'work'):
            (root / name).mkdir(parents=True)
        work = root / 'work'

        def run(*args):
            result = subprocess.run(['openssl', *map(str, args)], cwd=work, capture_output=True, text=True)
            if result.returncode:
                raise SystemExit('openssl failed: ' + result.stderr)

        for role in ('server', 'client'):
            ca_key, ca_cert = root / 'ca' / f'{role}_ca_key.pem', root / 'ca' / f'{role}_ca.pem'
            run('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', ca_key)
            run('req', '-new', '-x509', '-key', ca_key, '-days', a.days, '-subj', f'/CN=EdgeFHE demo {role} CA',
                '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0', '-out', ca_cert)
            key, csr, cert = root / role / f'{role}_key.pem', work / f'{role}.csr', root / role / f'{role}_cert.pem'
            if role == 'client':
                key, cert = root / 'client' / 'client_key.pem', root / 'client' / 'client_cert.pem'
            run('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', key)
            subject = f'/CN={a.server_host[0]}' if role == 'server' else '/CN=EdgeFHE KV260 client'
            run('req', '-new', '-key', key, '-subj', subject, '-out', csr)
            ext = 'basicConstraints=critical,CA:FALSE\nextendedKeyUsage=' + ('serverAuth' if role == 'server' else 'clientAuth') + '\n'
            if role == 'server':
                ext += 'subjectAltName=' + ','.join(san) + '\n'
            (work / f'{role}.ext').write_text(ext)
            run('x509', '-req', '-in', csr, '-CA', ca_cert, '-CAkey', ca_key, '-CAcreateserial', '-days', a.days,
                '-sha256', '-extfile', work / f'{role}.ext', '-out', cert)
        shutil.copy(root / 'ca' / 'client_ca.pem', root / 'server' / 'client_ca.pem')
        shutil.copy(root / 'ca' / 'server_ca.pem', root / 'client' / 'server_ca.pem')
        shutil.rmtree(work)

        der = ssl.PEM_cert_to_DER_cert((root / 'server' / 'server_cert.pem').read_text())
        common = {'protocol': PROTOCOL, 'server_url': f'https://{url_host}:{a.port}', 'server_id': a.server_id,
                  'token': secrets.token_hex(32), 'server_certificate_sha256': hashlib.sha256(der).hexdigest(),
                  'mtls': True}
        (root / 'server' / 'config.json').write_text(json.dumps({**common, 'client_ca': 'client_ca.pem'}, indent=2) + '\n')
        (root / 'client' / 'config.json').write_text(json.dumps(
            {**common, 'client_certificate': 'client_cert.pem', 'client_key': 'client_key.pem'}, indent=2) + '\n')
    finally:
        os.umask(old_mask)
    print(f'DEMO_TLS_READY {root}')
    print(f'  PC (cloud):      --config {root}/server/config.json --private-dir {root}/server')
    print(f'  KV260 (edge):    copy {root}/client to the board, then --config <dir>/config.json '
          '--server-cert <dir>/server_ca.pem --private-dir <dir>')
    print(f'  Keep {root}/ca private; delete the folder after the demo.')


if __name__ == '__main__':
    main()
