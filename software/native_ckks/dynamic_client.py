"""Interactive/CSV private-input client. Python handles I/O; C++ handles CKKS math."""
import argparse
import csv
import ctypes as C
import hashlib
import json
import math
from pathlib import Path
import secrets
import time
from model import read_model, encode_model, analytic_bound, assert_no_wrap
from native_api import Native, N, Q, SCALE, PROFILE, words
from protocol import encode, decode, require, load_json
from transport import Client

class Session:
    def __init__(self, native, transport, model_path):
        self.native = native; self.transport = transport
        self.model, self.model_sha = read_model(model_path)
        ready = transport.ready()
        require(ready['model_sha256'] == self.model_sha and ready['model'] == self.model and ready['profile'] == PROFILE,
                'Trusted local model/profile disagrees with service')
        self.weights, self.bias = encode_model(native, self.model)
        sampled, self.a = native.sample()
        self.secret = words(sampled[:N], 'b'); self.b = native.keygen(sampled, self.a)
        # Release sampled arrays after setup; OS/process memory is not a verified secure enclave.
        self.bound = native.error_bound(self.model)
        require(self.bound <= self.model['max_logit_error'], 'Native error bound exceeds model target')

    def run(self, records):
        require(type(records) is list and 1 <= len(records) <= 2048, 'Need 1..2048 records')
        for row in records:
            require(type(row) is list and len(row) == 4 and all(type(v) in (float, int) and math.isfinite(v) and
                    abs(v) <= self.model['input_abs_max'] for v in row), 'Need four finite admissible values per record')
        start = time.perf_counter_ns(); phases = {}
        t = time.perf_counter_ns(); messages = [self.native.encode([row[f] for row in records]) for f in range(4)]
        bound_int = self.native.no_wrap(sum(messages, []), self.weights, self.bias)
        phases['encode_and_bound_ns'] = time.perf_counter_ns()-t
        t = time.perf_counter_ns(); samples = self.native.encryption_samples()
        phases['fresh_OS_sampling_ns'] = time.perf_counter_ns()-t
        t = time.perf_counter_ns(); cipher = self.native.encrypt(self.b, self.a, sum(messages, []), samples)
        phases['native_encrypt_ns'] = time.perf_counter_ns()-t
        request_id = secrets.token_hex(16)
        t = time.perf_counter_ns(); raw = encode('request', request_id, self.model['model_id'], len(records), cipher)
        response = self.transport.evaluate(raw)
        header, result = decode(response, 'response', request_id, hashlib.sha256(raw).hexdigest())
        require(header['model_id'] == self.model['model_id'] and header['records'] == len(records), 'Response model/record binding failed')
        phases['framing_TLS_cloud_roundtrip_ns'] = time.perf_counter_ns()-t
        t = time.perf_counter_ns(); coefficients = self.native.decrypt(words(result), self.secret)
        require(max(map(abs, coefficients)) <= bound_int, 'Returned ciphertext outside local no-wrap bound')
        decoded = self.native.decode(coefficients, SCALE*SCALE)
        phases['native_decrypt_decode_ns'] = time.perf_counter_ns()-t
        decisions = []
        probability, classification = self.native.decisions([v.real for v in decoded[:len(records)]], self.bound)
        for i, slot in enumerate(decoded[:len(records)]):
            require(math.isfinite(slot.real) and math.isfinite(slot.imag) and abs(slot.imag) <= self.bound, 'Invalid real output')
            v = slot.real
            decisions.append({'logit': v, 'probability': probability[i], 'logit_error_bound': self.bound,
                              'class': classification[i]})
        # Display decrypted results locally; never submit these objects to the service.
        return {'status': 'DYNAMIC_NATIVE_CLIENT_COMPLETE', 'records': len(records),
                'results': decisions, 'stage_times_ns': phases,
                'session_hot_path_ns': time.perf_counter_ns()-start,
                'key_reused': True, 'diagnostic_decrypt_calls': 0,
                'security_bits_claimed': None}

def main():
    p = argparse.ArgumentParser(); p.add_argument('--config', type=Path, required=True)
    p.add_argument('--server-cert', type=Path, required=True); p.add_argument('--private-dir', type=Path, required=True)
    p.add_argument('--library', type=Path, required=True); p.add_argument('--model', type=Path, required=True)
    p.add_argument('--input-csv', type=Path); p.add_argument('--workers', type=int, choices=(1, 3), default=3)
    a = p.parse_args()
    if a.input_csv:
        with a.input_csv.open(newline='', encoding='utf-8') as f: records = [[float(v) for v in row] for row in csv.reader(f)]
    else:
        print('在本機輸入四個特徵，以空白分隔 / Enter four private features:')
        records = [[float(v) for v in input().split()]]
    config = load_json(a.config.read_text(encoding='utf-8'))
    with Native(a.library, a.workers) as native:
        session = Session(native, Client(config, a.server_cert, a.private_dir), a.model)
        print(json.dumps(session.run(records), ensure_ascii=False, indent=2, allow_nan=False))

if __name__ == '__main__': main()
