"""Dynamic affine service: public model + ciphertext, no acceptance fixtures.

Default audit contains event types/counts only. No request/response body, plaintext,
secret key, Authorization value, or decrypted result is persisted.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import ssl
import threading
import time
from model import read_model, encode_model
from native_api import Native, PROFILE, words
from net_config import validate_config, endpoint
from protocol import MAX_BYTES, encode, decode, require, load_json

def serve(config_path, private_dir, model_path, library, audit, bind=None, max_requests=10000):
    config = validate_config(load_json(Path(config_path).read_text()))
    public_model, model_sha = read_model(model_path)
    host, port = endpoint(config['server_url']); address = bind or host
    private_dir = Path(private_dir)
    # A bounded replay ledger fails closed when exhausted, rather than evicting old IDs.
    require(1 <= max_requests <= 100000, 'Invalid server request capacity')
    audit = Path(audit)
    require(not audit.exists(), 'Use a new audit path; preserve prior evidence')
    audit.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock(); used = set(); count = 0
    native = Native(library, 3); weight, bias = encode_model(native, public_model)
    def event(kind, **extra):
        with lock:
            with audit.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'event': kind, 'time_ns': time.time_ns(), **extra}, sort_keys=True)+'\n')
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_): pass
        def send(self, status, body=b''):
            self.send_response(status); self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close'); self.end_headers(); self.wfile.write(body); self.close_connection = True
        def auth(self):
            values = self.headers.get_all('Authorization', [])
            return len(values) == 1 and secrets.compare_digest(values[0], 'Bearer '+config['token'])
        def do_GET(self):
            if not self.auth(): event('auth_rejected'); self.send(401); return
            if self.path != '/ready': self.send(404); return
            body = {'protocol': config['protocol'], 'server_id': config['server_id'], 'profile': PROFILE,
                    'model': public_model, 'model_sha256': model_sha, 'mtls': config['mtls']}
            self.send(200, json.dumps(body, sort_keys=True, allow_nan=False).encode())
        def do_POST(self):
            nonlocal count
            if not self.auth(): event('auth_rejected'); self.send(401); return
            if self.path != '/evaluate': self.send(404); return
            try:
                sizes = self.headers.get_all('Content-Length', [])
                require(len(sizes) == 1 and sizes[0].isdigit() and 44 < int(sizes[0]) <= MAX_BYTES and
                        not self.headers.get_all('Transfer-Encoding', []) and
                        self.headers.get_all('Content-Type', []) == ['application/octet-stream'], 'Bad HTTP framing')
                self.connection.settimeout(30); raw = self.rfile.read(int(sizes[0])); require(len(raw) == int(sizes[0]), 'Truncated HTTP body')
                header, cipher = decode(raw, 'request')
                require(header['model_id'] == public_model['model_id'], 'Wrong public model')
                with lock:
                    require(header['request_id'] not in used, 'Replayed request')
                    require(len(used) < max_requests, 'Replay ledger capacity exhausted')
                    used.add(header['request_id'])
                out = native.affine(words(cipher), weight, bias)
                response = encode('response', header['request_id'], header['model_id'], header['records'], out,
                                  hashlib.sha256(raw).hexdigest())
                with lock: count += 1; current = count
                event('ciphertext_affine_completed', count=current, records=header['records'])
                self.send(200, response)
            except (ValueError, KeyError, UnicodeError, TimeoutError, ConnectionError):
                event('request_rejected'); self.send(400)
    class Server(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True
        # Bound both backlog and active request work; avoid unbounded thread creation.
        request_queue_size = 8
        def __init__(self, *args):
            self.capacity = threading.BoundedSemaphore(4); super().__init__(*args)
        def process_request(self, request, address):
            if not self.capacity.acquire(blocking=False): self.shutdown_request(request); return
            try: super().process_request(request, address)
            except BaseException: self.capacity.release(); raise
        def process_request_thread(self, request, address):
            try:
                request.settimeout(30)
                secure = self.tls.wrap_socket(request, server_side=True)
                super().process_request_thread(secure, address)
            except (ssl.SSLError, TimeoutError, ConnectionError):
                request.close()
            finally: self.capacity.release()
    server = Server((address, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(private_dir/'server_cert.pem'), str(private_dir/'server_key.pem'))
    if config['mtls']:
        require('client_ca' in config, 'mTLS service requires a client CA')
        context.load_verify_locations(cafile=str(private_dir/config['client_ca'])); context.verify_mode = ssl.CERT_REQUIRED
    # Perform TLS handshakes inside bounded workers, with a timeout, not in accept().
    server.tls = context
    event('server_ready', bind=address, port=port, mtls=config['mtls'], model_sha256=model_sha)
    print('DYNAMIC_CIPHERTEXT_SERVER_READY port='+str(port), flush=True)
    try: server.serve_forever(poll_interval=.25)
    finally: server.server_close(); native.close()

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--config', required=True); p.add_argument('--private-dir', required=True)
    p.add_argument('--model', required=True); p.add_argument('--library', required=True); p.add_argument('--audit', required=True)
    p.add_argument('--bind'); p.add_argument('--max-requests', type=int, default=10000)
    a = p.parse_args(); serve(a.config, a.private_dir, a.model, a.library, a.audit, a.bind, a.max_requests)
