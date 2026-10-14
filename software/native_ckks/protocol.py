"""Versioned ciphertext-only framing for arbitrary requests, with strict limits."""
from array import array
import hashlib
import json
import re
import struct
import sys
from native_api import N, Q, SCALE, PROFILE_ID

MAGIC = b'EFCKKS4\0'
MAX_BYTES = 512*1024
REQUEST_ID = re.compile(r'[0-9a-f]{32}\Z')
MODEL_ID = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
COMMON = {'version', 'kind', 'request_id', 'model_id', 'profile_id', 'degree', 'primes',
          'scale', 'records', 'polynomial_count', 'payload_sha256'}
def require(ok, message):
    if not ok: raise ValueError(message)
def unique(pairs):
    out = {}
    for k, v in pairs:
        require(k not in out, 'Duplicate JSON key'); out[k] = v
    return out
def load_json(text):
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
def validate_words(w, count):
    require(len(w) == count*3*N, 'Wrong ciphertext word count')
    for p in range(count):
        for l, q in enumerate(Q):
            require(all(type(v) is int and 0 <= v < q for v in w[(p*3+l)*N:(p*3+l+1)*N]), 'Noncanonical ciphertext')
def encode(kind, request_id, model_id, records, values, request_sha=None):
    count = 8 if kind == 'request' else 2
    require(kind in ('request', 'response'), 'Invalid packet kind')
    require(type(request_id) is str and REQUEST_ID.fullmatch(request_id), 'Invalid request ID')
    require(type(model_id) is str and MODEL_ID.fullmatch(model_id), 'Invalid model ID')
    require(type(records) is int and 1 <= records <= 2048, 'Invalid record count')
    w = list(values); validate_words(w, count); payload = array('I', w)
    require(payload.itemsize == 4, 'Unsupported native word width')
    if sys.byteorder != 'little': payload.byteswap()
    payload = payload.tobytes()
    h = {'version': 4, 'kind': kind, 'request_id': request_id, 'model_id': model_id,
         'profile_id': PROFILE_ID, 'degree': N, 'primes': list(Q),
         'scale': SCALE if kind == 'request' else SCALE*SCALE, 'records': records,
         'polynomial_count': count, 'payload_sha256': hashlib.sha256(payload).hexdigest()}
    if kind == 'response':
        require(type(request_sha) is str and HASH.fullmatch(request_sha), 'Invalid request digest')
        h['request_sha256'] = request_sha
    header = json.dumps(h, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    raw = MAGIC+struct.pack('<I', len(header))+header+payload
    require(len(raw)+32 <= MAX_BYTES, 'Oversized packet')
    return raw+hashlib.sha256(raw).digest()

def decode(raw, kind, request_id=None, request_sha=None):
    require(type(raw) is bytes and 44 < len(raw) <= MAX_BYTES and raw[:8] == MAGIC, 'Bad framing')
    require(hashlib.sha256(raw[:-32]).digest() == raw[-32:], 'Integrity mismatch')
    length = struct.unpack_from('<I', raw, 8)[0]
    require(0 < length <= 4096 and 12+length < len(raw)-32, 'Bad header size')
    h = load_json(raw[12:12+length].decode('ascii'))
    wanted = COMMON | ({'request_sha256'} if kind == 'response' else set())
    require(type(h) is dict and set(h) == wanted, 'Private/unknown/missing header field')
    require(type(h['version']) is int and h['version'] == 4 and h['kind'] == kind, 'Wrong protocol version/kind')
    require(type(h['request_id']) is str and REQUEST_ID.fullmatch(h['request_id']), 'Invalid request identity')
    require(type(h['model_id']) is str and MODEL_ID.fullmatch(h['model_id']), 'Invalid model identity')
    require(h['profile_id'] == PROFILE_ID and type(h['degree']) is int and h['degree'] == N, 'Wrong math profile')
    require(type(h['primes']) is list and all(type(v) is int for v in h['primes']) and h['primes'] == list(Q), 'Wrong primes')
    require(type(h['scale']) is int and h['scale'] == (SCALE if kind == 'request' else SCALE*SCALE), 'Wrong scale')
    require(type(h['records']) is int and 1 <= h['records'] <= 2048, 'Wrong record count')
    count = 8 if kind == 'request' else 2
    require(type(h['polynomial_count']) is int and h['polynomial_count'] == count, 'Wrong ciphertext dimensions')
    if request_id is not None: require(h['request_id'] == request_id, 'Response request ID mismatch')
    if kind == 'response':
        require(type(h['request_sha256']) is str and HASH.fullmatch(h['request_sha256']), 'Invalid request binding')
        if request_sha is not None: require(h['request_sha256'] == request_sha, 'Response request digest mismatch')
    payload = raw[12+length:-32]
    require(len(payload) == count*3*N*4 and type(h['payload_sha256']) is str and HASH.fullmatch(h['payload_sha256']) and
            hashlib.sha256(payload).hexdigest() == h['payload_sha256'], 'Payload length/digest mismatch')
    w = array('I'); w.frombytes(payload)
    if sys.byteorder != 'little': w.byteswap()
    values = list(w); validate_words(values, count)
    return h, values
