"""Pinned HTTPS transport, optional mandatory client-certificate policy via config."""
import hashlib
import http.client
import json
from pathlib import Path
import ssl
from net_config import endpoint, validate_config
from protocol import MAX_BYTES, load_json, require

class Client:
    def __init__(self, config, server_certificate, private_dir):
        self.config = validate_config(config); self.host, self.port = endpoint(config['server_url'])
        self.context = ssl.create_default_context(cafile=str(server_certificate))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        if config['mtls']:
            for name in ('client_certificate', 'client_key'):
                require(name in config, 'mTLS requires client certificate and key')
            self.context.load_cert_chain(str(Path(private_dir)/config['client_certificate']),
                                         str(Path(private_dir)/config['client_key']))
    def call(self, path, body=None):
        require(path in ('/ready', '/evaluate'), 'Unsupported endpoint')
        c = http.client.HTTPSConnection(self.host, self.port, timeout=30, context=self.context)
        try:
            c.connect()
            cert = c.sock.getpeercert(binary_form=True)
            require(hashlib.sha256(cert).hexdigest() == self.config['server_certificate_sha256'], 'Certificate pin mismatch before Authorization')
            headers = {'Authorization': 'Bearer '+self.config['token'], 'Connection': 'close'}
            if body is not None:
                require(type(body) is bytes and len(body) <= MAX_BYTES, 'Invalid transport body')
                headers['Content-Type'] = 'application/octet-stream'
            c.request('GET' if body is None else 'POST', path, body=body, headers=headers)
            r = c.getresponse(); lengths = r.headers.get_all('Content-Length', [])
            require(len(lengths) == 1 and lengths[0].isdigit() and int(lengths[0]) <= MAX_BYTES and
                    not r.headers.get_all('Transfer-Encoding', []), 'Invalid HTTP response framing')
            data = r.read(int(lengths[0])+1)
            require(len(data) == int(lengths[0]), 'Truncated HTTP response')
            require(r.status == 200, 'Service refused request with HTTP '+str(r.status))
            return data
        finally: c.close()
    def ready(self):
        result = load_json(self.call('/ready'))
        require(type(result) is dict and set(result) == {'protocol', 'server_id', 'profile', 'model', 'model_sha256', 'mtls'}, 'Wrong readiness schema')
        require(result['protocol'] == self.config['protocol'] and result['server_id'] == self.config['server_id'] and
                result['mtls'] == self.config['mtls'], 'Service identity mismatch')
        return result
    def evaluate(self, raw): return self.call('/evaluate', raw)
