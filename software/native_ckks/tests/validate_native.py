"""Local Linux numerical acceptance; never opens a device or network endpoint."""
import argparse
from array import array
import ctypes as C
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform
import random
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT/'software/native_ckks'
sys.path.insert(0, str(SRC)); sys.path.insert(0, str(ROOT/'software/ckks'))
from native_api import Native, N, Q, SCALE, words
from ckks_canonical import CKKSEncoder
import profile as reference

def require(ok, message):
    if not ok: raise AssertionError(message)

def flatten(poly): return [v for limb in poly for v in limb]
def split(poly): return [list(poly[l*N:(l+1)*N]) for l in range(3)]

def integer_convolution(left, right, q):
    # Kronecker substitution: base 2^80 > N*(q-1)^2. No carries between
    # convolution coefficients; Python's exact big-integer multiply uses no NTT here.
    stride = 10
    aa = int.from_bytes(b''.join(int(v).to_bytes(stride, 'little') for v in left), 'little')
    bb = int.from_bytes(b''.join(int(v).to_bytes(stride, 'little') for v in right), 'little')
    raw = (aa*bb).to_bytes(2*N*stride, 'little')
    c = [int.from_bytes(raw[i*stride:(i+1)*stride], 'little') for i in range(2*N)]
    return [(c[i]-c[i+N])%q for i in range(N)]

def fixed_material(seed):
    m = reference.material(seed)
    samples = words(m['s']+m['e']+sum(m['u'], [])+sum(m['e0'], [])+sum(m['e1'], []), 'b')
    return m, samples, words(flatten(m['a']))

def main():
    p = argparse.ArgumentParser(); p.add_argument('--build', type=Path, required=True)
    p.add_argument('--dense-cases', type=int, default=1000); a = p.parse_args()
    build = a.build; build.mkdir(parents=True, exist_ok=True)
    backend = build/'libindependent.so'
    subprocess.run(['g++', '-O3', '-std=c++17', '-shared', '-fPIC', '-pthread',
                    str(ROOT/'software/ckks/backend.cpp'), '-o', str(backend)], check=True)
    independent = C.CDLL(str(backend)); pointer = C.POINTER(C.c_uint32)
    independent.edgefhe_cpu.argtypes = [C.c_uint32, pointer, pointer, pointer]
    def cpu(left, right):
        out = (C.c_uint32*(3*N))()
        require(independent.edgefhe_cpu(2, left, right, out) == 0, 'Independent CPU status')
        return out
    report = {'status': 'RUNNING', 'board_operations': 0, 'architecture': platform.machine(),
              'profile_security_accepted': False, 'start_ns': time.perf_counter_ns(), 'failures': []}
    try:
        with Native(build/'libedgefhe_native.so', 1) as native, Native(build/'libedgefhe_native.so', 3) as multi:
            for v in (-3.5, -2.5, -1.5, -.5, .5, 1.5, 2.5, 3.5):
                out = C.c_int64(); native.check(native.lib.ef_round(v, C.byref(out)))
                require(out.value == round(v), 'Ties-to-even rounding mismatch')
            rng = random.Random(0x20261007)
            vectors = [[], [0.], [16.], [-16.], [1.5, -.2, 3.14],
                       [complex(rng.uniform(-2, 2), rng.uniform(-2, 2)) for _ in range(2048)]]
            vectors += [[rng.uniform(-16, 16) for _ in range(rng.randrange(1, 2049))] for _ in range(18)]
            encoder_exact = 0; coefficient_max_delta = 0; max_error = 0.
            for value in vectors:
                gold = CKKSEncoder(N, SCALE).encode(value); actual = native.encode(value)
                difference = max(abs(x-y) for x, y in zip(gold, actual))
                coefficient_max_delta = max(coefficient_max_delta, difference)
                require(difference == 0, 'Native/Python encoder coefficient parity mismatch')
                encoder_exact += N
                decoded = native.decode(actual); expected = list(map(complex, value))+[0j]*(2048-len(value))
                err = max(abs(x-y) for x, y in zip(decoded, expected)); max_error = max(max_error, err)
                require(err <= N/(2*SCALE)+1e-8, 'Encoder/decode rounding bound failed')
                pydecoded = CKKSEncoder(N, SCALE).decode(actual)
                require(max(abs(x-y) for x, y in zip(decoded, pydecoded)) <= 1e-8, 'Independent decoder mismatch')
                for k in (0, 1, 2047):
                    require(abs(decoded[k]-reference.direct_slot(actual, SCALE, k)) <= 1e-8, 'Direct embedding disagrees')
            report['encoder'] = {'cases': len(vectors), 'exact_coefficients': encoder_exact,
                'coefficient_max_delta': coefficient_max_delta, 'max_slot_error': max_error, 'ties': 8}
            print('NATIVE_ENCODER_EXACT_AND_FULL_SLOT_BOUND_PASS', report['encoder'], flush=True)
            digest = hashlib.sha256(); start = time.perf_counter()
            for case in range(a.dense_cases):
                left = words(rng.randrange(q) for q in Q for _ in range(N))
                right = words(rng.randrange(q) for q in Q for _ in range(N))
                gold = cpu(left, right); actual = native.multiply(left, right)
                require(bytes(gold) == bytes(actual), f'Dense independent oracle mismatch {case}')
                for l, q in enumerate(Q):
                    schoolbook = integer_convolution(left[l*N:(l+1)*N], right[l*N:(l+1)*N], q)
                    require(schoolbook == list(actual[l*N:(l+1)*N]), f'Exact integer convolution mismatch {case}/{l}')
                if case < 16: require(bytes(multi.multiply(left, right)) == bytes(gold), 'Three-worker exact mismatch')
                digest.update(bytes(left)); digest.update(bytes(right)); digest.update(bytes(actual))
                if (case+1)%250 == 0: print(f'DENSE_PROGRESS distinct_inputs={case+1}', flush=True)
            left = [0]*(3*N); right = [0]*(3*N)
            for l in range(3): left[l*N+N-1] = 1; right[l*N+1] = 1
            product = native.multiply(words(left), words(right))
            require(all(product[l*N] == Q[l]-1 and not any(product[l*N+1:(l+1)*N]) for l in range(3)), 'X^N=-1 boundary failed')
            report['dense'] = {'distinct_cases': a.dense_cases, 'exact_coefficients': a.dense_cases*3*N,
                'independent_oracle': 'exact integer Kronecker convolution + frozen uint64-modulo NTT vs new Shoup NTT',
                'seconds': time.perf_counter()-start, 'stream_sha256': digest.hexdigest(), 'negacyclic_boundary': True}
            print('NATIVE_DISTINCT_DENSE_ORACLE_PASS', report['dense'], flush=True)
            max_affine_error = 0.; exact = 0
            for case in range(12):
                mat, samples, ar = fixed_material(4000+case)
                x = [rng.uniform(-8, 8) for _ in range(4)]; w = [rng.uniform(-2, 2) for _ in range(4)]; bias = rng.uniform(-1, 1)
                if case == 0: x = [0.]*4
                if case == 1: x = [16., -16., 16., -16.]
                messages = [native.encode([v]) for v in x]
                weights = [native.encode([v]) for v in w]; bm = native.encode([bias], SCALE*SCALE)
                b = native.keygen(samples, ar)
                as_gold = cpu(ar, words(flatten(reference.to_rns(mat['s']))))
                b_gold = reference.add(reference.neg(split(as_gold)), reference.to_rns(mat['e']))
                require(list(b) == flatten(b_gold), 'Keygen exact mismatch')
                ciphertext = native.encrypt(b, ar, sum(messages, []), words(samples[2*N:], 'b'))
                multi_cipher = multi.encrypt(b, ar, sum(messages, []), words(samples[2*N:], 'b'))
                require(bytes(ciphertext) == bytes(multi_cipher), 'Parallel encryption mismatch')
                c0 = []; c1 = []; noisy = []; bounds = []
                for f in range(4):
                    u = words(flatten(reference.to_rns(mat['u'][f])))
                    bu, au = split(cpu(b, u)), split(cpu(ar, u))
                    c0.append(reference.add(bu, reference.to_rns(mat['e0'][f]), reference.to_rns(messages[f])))
                    c1.append(reference.add(au, reference.to_rns(mat['e1'][f])))
                    one = words(flatten(c0[-1])+flatten(c1[-1]))
                    decoded_coeff = native.decrypt(one, words(mat['s'], 'b'))
                    noise = reference.noise_integer(mat, f)
                    expected_coeff = [m+e for m, e in zip(messages[f], noise)]; noisy.append(expected_coeff)
                    require(decoded_coeff == expected_coeff, 'Independent integer RLWE cancellation failed')
                    require(2*max(map(abs, expected_coeff)) < math.prod(Q), 'Input centered CRT wrap')
                    bounds.append(N/(2*SCALE)+sum(map(abs, noise))/SCALE+1e-8)
                    exact += N
                require(list(ciphertext) == sum([flatten(p) for p in c0+c1], []), 'Encryption exact mismatch')
                evaluated = native.affine(ciphertext, sum(weights, []), bm)
                gold0 = reference.add(reference.to_rns(bm), *[split(cpu(words(flatten(c0[f])), words(flatten(reference.to_rns(weights[f]))))) for f in range(4)])
                gold1 = reference.add(*[split(cpu(words(flatten(c1[f])), words(flatten(reference.to_rns(weights[f]))))) for f in range(4)])
                require(list(evaluated) == flatten(gold0)+flatten(gold1), 'NTT-domain fused affine exact mismatch')
                require(bytes(multi.affine(ciphertext, sum(weights, []), bm)) == bytes(evaluated), 'Parallel affine mismatch')
                coeff = native.decrypt(evaluated, words(mat['s'], 'b'))
                gold = reference.centered(reference.add(gold0, split(cpu(words(flatten(gold1)), words(flatten(reference.to_rns(mat['s'])))))))
                require(coeff == gold, 'Affine centered CRT exact mismatch')
                bound_int = sum(sum(map(abs, m))*sum(map(abs, wt)) for m, wt in zip(noisy, weights))+max(map(abs, bm))
                require(2*bound_int < math.prod(Q) and max(map(abs, coeff)) <= bound_int, 'Affine no-wrap bound')
                decoded = native.decode(coeff, SCALE*SCALE); wanted = math.fsum(v*t for v, t in zip(x, w))+bias
                ew=N/(2*SCALE)+1e-8;eb=N/(2*SCALE*SCALE)+1e-8
                bound=sum(abs(wi)*ex+abs(xi)*ew+ex*ew for xi,wi,ex in zip(x,w,bounds))+eb+1e-8
                error = max(abs(v-(wanted if i==0 else 0.)) for i, v in enumerate(decoded))
                max_affine_error = max(max_affine_error, error); require(error <= bound, 'Affine slot bound')
                exact += 3*N+8*3*N+2*3*N+N
            report['pipeline'] = {'cases': 12, 'exact_integer_and_RNS_comparisons': exact,
                                  'max_affine_slot_error': max_affine_error, 'independent_sparse_noise': True}
            print('NATIVE_KEYGEN_ENCRYPT_AFFINE_DECRYPT_EXACT_PASS', report['pipeline'], flush=True)
            rejects = 0
            for bad in ([float('nan')], [float('inf')], [16.01], [0.] * 2049):
                try: native.encode(bad)
                except ValueError: rejects += 1
                else: raise AssertionError('Invalid encoder input accepted')
            bad = words([Q[0]]+[0]*(3*N-1))
            try: native.multiply(bad, words([0]*(3*N)))
            except ValueError: rejects += 1
            else: raise AssertionError('Noncanonical RNS accepted')
            # Production sampling has no seed API; verify structure/freshness, not statistical security.
            m1, a1 = native.sample(); m2, a2 = native.sample()
            for mat in (m1, m2):
                for i in range(14):
                    values = mat[i*N:(i+1)*N]
                    require(all(v in (-1, 0, 1) for v in values), 'OS sample alphabet')
                    require(sum(v != 0 for v in values) == (64 if i==0 or 2<=i<6 else 16), 'OS sample weight')
            require(bytes(m1) != bytes(m2) and bytes(a1) != bytes(a2), 'OS sample freshness failure')
            report['negative_tests'] = rejects; report['os_sampling_structure_and_freshness'] = True
            model={'weights':[1.1,-.7,.9,.4],'input_abs_max':16}
            expected=4*(16*(N/(2*SCALE)+1e-8))+(sum(map(abs,model['weights'])))*(N/(2*SCALE)+2064/SCALE+1e-8)+4*(N/(2*SCALE)+2064/SCALE+1e-8)*(N/(2*SCALE)+1e-8)+N/(2*SCALE*SCALE)+2e-8
            require(abs(native.error_bound(model)-expected)<=1e-15 and native.error_bound(model)<5e-4,'Native affine model bound mismatch')
            probability,decisions=native.decisions([-1000.,0.,1000.],.001)
            require(probability==[0.,.5,1.] and decisions==[0,None,1],'Stable sigmoid and uncertain boundary mismatch')
            require(native.no_wrap(sum(messages,[]),sum(weights,[]),bm)==sum((sum(map(abs,m))+2064)*sum(map(abs,wt)) for m,wt in zip(messages,weights))+max(map(abs,bm)), 'Native exact no-wrap bound mismatch')
        report['status'] = 'NATIVE_LOCAL_MATHEMATICAL_ACCEPTANCE_PASS'
        print(report['status'], flush=True)
    except Exception as error:
        report['status'] = 'FAIL'; report['failures'].append(repr(error)); raise
    finally:
        report['duration_ns'] = time.perf_counter_ns()-report.pop('start_ns')
        report['source_identities'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sorted(SRC.glob('*')) if p.is_file()}
        (build/'native_acceptance.json').write_text(json.dumps(report, indent=2)+'\n')

if __name__ == '__main__': main()
