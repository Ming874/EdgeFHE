"""Private, short-lived loopback QA TLS material generated with the OS openssl CLI."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess

def prepare(directory):
    directory=Path(directory)
    if directory.exists():raise ValueError('Use a fresh private TLS directory')
    if not shutil.which('openssl'):raise RuntimeError('Local TLS QA needs the openssl executable')
    directory.mkdir(mode=0o700)
    old_mask=os.umask(0o077)
    def run(*args):
        result=subprocess.run(['openssl']+list(args),cwd=directory,capture_output=True,text=True)
        if result.returncode:raise RuntimeError('openssl QA material generation failed: '+result.stderr)
    try:
        for role in ('server','client'):
            run('genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',role+'_ca_key.pem')
            run('req','-new','-x509','-key',role+'_ca_key.pem','-days','7','-subj','/CN=EdgeFHE-local-'+role+'-QA-CA',
                '-addext','basicConstraints=critical,CA:TRUE,pathlen:0','-out',role+'_ca.pem')
            run('genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',role+'_key.pem')
            run('req','-new','-key',role+'_key.pem','-subj','/CN='+('localhost' if role=='server' else 'EdgeFHE-QA-client'),'-out',role+'.csr')
            ext='basicConstraints=critical,CA:FALSE\nextendedKeyUsage='+('serverAuth' if role=='server' else 'clientAuth')+'\n'
            if role=='server':ext+='subjectAltName=DNS:localhost,IP:127.0.0.1\n'
            (directory/(role+'.ext')).write_text(ext)
            run('x509','-req','-in',role+'.csr','-CA',role+'_ca.pem','-CAkey',role+'_ca_key.pem','-CAcreateserial',
                '-days','7','-sha256','-extfile',role+'.ext','-out',role+'_cert.pem')
        der=ssl.PEM_cert_to_DER_cert((directory/'server_cert.pem').read_text())
        config={'protocol':'edgefhe-dynamic-affine-tls-v4','server_url':'https://127.0.0.1:18087',
                'server_id':'local-private-dynamic-qa','token':secrets.token_hex(32),'server_certificate_sha256':hashlib.sha256(der).hexdigest(),
                'mtls':True,'client_certificate':'client_cert.pem','client_key':'client_key.pem','client_ca':'client_ca.pem'}
        (directory/'config.json').write_text(json.dumps(config,indent=2)+'\n')
    finally:os.umask(old_mask)
