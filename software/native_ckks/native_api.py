"""Thin FFI: all encoder/RNS/keygen/encrypt/decrypt/affine arithmetic is C++.

CPU default never opens a device. A board FD may only be passed by an operator
after current source, firmware, runtime and board acceptance gates.
"""
from array import array
import ctypes as C
from pathlib import Path

N, LIMBS, FEATURES, SLOTS = 4096, 3, 4, 2048
Q = (998244353, 1004535809, 469762049)
SCALE = 1 << 29
PROFILE_ID = "edgefhe-functional-sparse-n4096-v4-s29"
PROFILE = {"id": PROFILE_ID, "degree": N, "primes": list(Q), "slots": SLOTS,
           "input_scale": SCALE, "output_scale": SCALE*SCALE,
           "sampler": "fixed-weight-ternary-s64-u64-e16", "security_bits": None}

class Wide(C.Structure):
    _fields_ = [("low", C.c_uint64), ("high", C.c_int64)]

def words(values, kind='I'):
    a = array(kind, values)
    wanted = {'I': 4, 'b': 1, 'q': 8}[kind]
    if a.itemsize != wanted:
        raise ValueError("Unsupported native word size")
    t = {'I': C.c_uint32, 'b': C.c_int8, 'q': C.c_int64}[kind]
    return (t*len(a)).from_buffer(a)  # ctypes keeps the array owner alive.

class Native:
    def __init__(self, library, workers=1, board_fd=-1):
        self.lib = C.CDLL(str(Path(library).resolve()))
        signatures = {
            'ef_create': ([C.c_uint, C.c_int], C.c_void_p),
            'ef_destroy': ([C.c_void_p], None), 'ef_error': ([], C.c_char_p),
            'ef_version': ([], C.c_char_p),
            'ef_round': ([C.c_double, C.POINTER(C.c_int64)], C.c_int),
            'ef_encode': ([C.POINTER(C.c_double), C.c_uint, C.c_uint64, C.POINTER(C.c_int64)], C.c_int),
            'ef_decode': ([C.POINTER(Wide), C.c_uint64, C.POINTER(C.c_double)], C.c_int),
            'ef_sample': ([C.POINTER(C.c_int8), C.POINTER(C.c_uint32)], C.c_int),
            'ef_sample_encryption': ([C.POINTER(C.c_int8)], C.c_int),
            'ef_multiply': ([C.c_void_p]+[C.POINTER(C.c_uint32)]*3, C.c_int),
            'ef_keygen': ([C.c_void_p, C.POINTER(C.c_int8)]+[C.POINTER(C.c_uint32)]*2, C.c_int),
            'ef_encrypt': ([C.c_void_p, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32),
                            C.POINTER(C.c_int64), C.POINTER(C.c_int8), C.POINTER(C.c_uint32)], C.c_int),
            'ef_decrypt': ([C.c_void_p, C.POINTER(C.c_uint32), C.POINTER(C.c_int8), C.POINTER(Wide)], C.c_int),
            'ef_affine': ([C.c_void_p, C.POINTER(C.c_uint32), C.POINTER(C.c_int64),
                           C.POINTER(C.c_int64), C.POINTER(C.c_uint32)], C.c_int),
            'ef_no_wrap': ([C.POINTER(C.c_int64),C.POINTER(C.c_int64),C.POINTER(C.c_int64),C.POINTER(Wide)],C.c_int),
            'ef_error_bound': ([C.POINTER(C.c_double),C.c_double,C.POINTER(C.c_double)],C.c_int),
            'ef_decide': ([C.POINTER(C.c_double),C.c_uint,C.c_double,C.POINTER(C.c_double),C.POINTER(C.c_int8)],C.c_int)}
        for name, (args, result) in signatures.items():
            f = getattr(self.lib, name); f.argtypes = args; f.restype = result
        self.context = self.lib.ef_create(workers, board_fd)
        if not self.context:
            raise ValueError(self.lib.ef_error().decode())
        self.workers = workers
    def close(self):
        if self.context:
            self.lib.ef_destroy(self.context); self.context = None
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def check(self, status):
        if status != 0: raise ValueError(self.lib.ef_error().decode())
    def encode(self, slots, scale=SCALE):
        values = [complex(v) for v in slots]
        x = (C.c_double*(2*len(values)))(*[v for z in values for v in (z.real, z.imag)])
        out = (C.c_int64*N)(); self.check(self.lib.ef_encode(x, len(values), scale, out)); return list(out)
    def decode(self, coefficients, scale=SCALE):
        if len(coefficients) != N: raise ValueError("Wrong decoded polynomial length")
        x = (Wide*N)()
        for i, v in enumerate(coefficients):
            if type(v) is not int or not -(1 << 127) <= v < (1 << 127): raise ValueError("Invalid wide integer")
            x[i].low = v & ((1 << 64)-1); x[i].high = v >> 64
        out = (C.c_double*(2*SLOTS))(); self.check(self.lib.ef_decode(x, scale, out))
        return [complex(out[2*i], out[2*i+1]) for i in range(SLOTS)]
    def sample(self):
        samples = (C.c_int8*(14*N))(); a = (C.c_uint32*(3*N))()
        self.check(self.lib.ef_sample(samples, a)); return samples, a
    def encryption_samples(self):
        samples = (C.c_int8*(12*N))(); self.check(self.lib.ef_sample_encryption(samples)); return samples
    def keygen(self, samples, a):
        b = (C.c_uint32*(3*N))(); self.check(self.lib.ef_keygen(self.context, samples, a, b)); return b
    def encrypt(self, b, a, messages, samples):
        if len(messages) != 4*N or len(samples) != 12*N: raise ValueError("Wrong encrypt dimensions")
        out = (C.c_uint32*(8*3*N))()
        self.check(self.lib.ef_encrypt(self.context, b, a, words(messages, 'q'), samples, out)); return out
    def decrypt(self, cipher, secret):
        if len(cipher) != 2*3*N or len(secret) != N: raise ValueError("Wrong decrypt dimensions")
        out = (Wide*N)(); self.check(self.lib.ef_decrypt(self.context, cipher, secret, out))
        return [(v.high << 64) | v.low for v in out]
    def affine(self, cipher, weights, bias):
        if len(cipher) != 8*3*N or len(weights) != 4*N or len(bias) != N: raise ValueError("Wrong affine dimensions")
        out = (C.c_uint32*(2*3*N))()
        self.check(self.lib.ef_affine(self.context, cipher, words(weights, 'q'), words(bias, 'q'), out)); return out
    def multiply(self, a, b):
        if len(a) != 3*N or len(b) != 3*N: raise ValueError("Wrong multiply dimensions")
        out = (C.c_uint32*(3*N))(); self.check(self.lib.ef_multiply(self.context, a, b, out)); return out
    def no_wrap(self,messages,weights,bias):
        if len(messages)!=4*N or len(weights)!=4*N or len(bias)!=N:raise ValueError('Wrong no-wrap dimensions')
        out=Wide();self.check(self.lib.ef_no_wrap(words(messages,'q'),words(weights,'q'),words(bias,'q'),C.byref(out)))
        return (out.high<<64)|out.low
    def error_bound(self,model):
        if len(model['weights'])!=4:raise ValueError('Wrong model weights')
        w=(C.c_double*4)(*model['weights']);out=C.c_double()
        self.check(self.lib.ef_error_bound(w,model['input_abs_max'],C.byref(out)));return out.value
    def decisions(self,logits,bound):
        n=len(logits);values=(C.c_double*n)(*logits);probability=(C.c_double*n)();decision=(C.c_int8*n)()
        self.check(self.lib.ef_decide(values,n,bound,probability,decision))
        return list(probability),[None if d==-1 else int(d) for d in decision]
