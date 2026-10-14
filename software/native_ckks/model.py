"""Public affine model only. No private features, keys, or acceptance fixtures."""
import hashlib
import json
import math
from pathlib import Path
from native_api import SCALE, SLOTS
from protocol import load_json, MODEL_ID

def read_model(path):
    m = load_json(Path(path).read_text(encoding='utf-8'))
    if type(m) is not dict or set(m) != {'model_id', 'weights', 'bias', 'input_abs_max', 'max_logit_error'}:
        raise ValueError('Wrong public model fields')
    if not isinstance(m['model_id'], str) or not MODEL_ID.fullmatch(m['model_id']): raise ValueError('Wrong model ID')
    if type(m['weights']) is not list or len(m['weights']) != 4: raise ValueError('Four model weights required')
    for v in m['weights']+[m['bias'], m['input_abs_max']]:
        if type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 16: raise ValueError('Invalid model value')
    if not 0 < m['input_abs_max'] <= 16: raise ValueError('Invalid private input bound')
    if type(m['max_logit_error']) not in (int,float) or not math.isfinite(m['max_logit_error']) or not 0 < m['max_logit_error'] <= 0.1:
        raise ValueError('Invalid public numerical error target')
    if analytic_bound(m)>m['max_logit_error']:raise ValueError('Model/range exceeds conservative CKKS error target')
    canonical = json.dumps(m, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return m, hashlib.sha256(canonical).hexdigest()

def encode_model(native, m):
    # The same public affine model is evaluated in each occupied SIMD slot.
    weights = sum((native.encode([w]*SLOTS) for w in m['weights']), [])
    bias = native.encode([m['bias']]*SLOTS, SCALE*SCALE)
    return weights, bias

def analytic_bound(model):
    # Uniform in the number of occupied slots. No actual private/noise values exported.
    ex = 4096/(2*SCALE)+2064/SCALE+1e-8
    ew = 4096/(2*SCALE)+1e-8
    eb = 4096/(2*SCALE*SCALE)+1e-8
    return sum(abs(w)*ex+model['input_abs_max']*ew+ex*ew for w in model['weights'])+eb+1e-8

def assert_no_wrap(messages, weights, bias):
    q = 998244353*1004535809*469762049
    # Negacyclic convolution infinity norm <= ||a||1 ||b||1.
    bound = sum((sum(map(abs, m))+2064)*sum(map(abs, w))
                for m, w in zip(messages, [weights[i*4096:(i+1)*4096] for i in range(4)]))+max(map(abs, bias))
    if 2*bound >= q: raise ValueError('Conservative affine centered-CRT no-wrap bound failed')
    return bound
