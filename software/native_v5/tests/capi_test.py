"""V5 C ABI through the unchanged V4 Python binding (native_api.Native).

Every V5 context (CPU with 1 or 4 workers, FPGA backend on the simulated device) must
give the same public key, ciphertexts, cloud result and decryption bit for bit, and with
--v4-library the same as the accepted V4 library. No device and no network: the FPGA
context uses fd -2 (SimulatedDmaDevice). Standard library only.
"""
import argparse
import ctypes as C
import random
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never leave __pycache__ inside the hash-locked repository
V4 = Path(__file__).resolve().parents[2] / 'native_ckks'
sys.path.insert(0, str(V4))
from native_api import Native, N, SCALE, SLOTS, words  # noqa: E402


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def round_trip(native, sampled, a, messages, samples, weights, bias):
    secret = words(list(sampled[:N]), 'b')
    b = native.keygen(sampled, a)
    cipher = native.encrypt(b, a, messages, samples)
    first = words(list(cipher[0:3 * N]) + list(cipher[12 * N:15 * N]))  # c0, c1 of feature 0
    plain = native.decrypt(first, secret)
    result = native.affine(cipher, weights, bias)
    logits = native.decrypt(result, secret)
    return list(b), list(cipher), plain, list(result), logits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True, help='libedgefhe_v5_capi.so')
    parser.add_argument('--v4-library', type=Path, help='accepted V4 libedgefhe_native.so (optional)')
    args = parser.parse_args()

    rng = random.Random(20261013)
    values = [[rng.uniform(-4, 4) for _ in range(SLOTS)] for _ in range(4)]
    contexts = [('cpu-w1', args.library, 1, -1), ('cpu-w4', args.library, 4, -1), ('fpga-simulated', args.library, 1, -2)]
    if args.v4_library:
        contexts.append(('v4-reference', args.v4_library, 1, -1))

    with Native(args.library, 1) as setup:
        sampled, a = setup.sample()
        samples = setup.encryption_samples()
        messages = sum((setup.encode(v) for v in values), [])
        weights = sum((setup.encode([w] * SLOTS) for w in (0.7, -0.45, 0.5, 0.55)), [])
        bias = setup.encode([-0.58] * SLOTS, SCALE * SCALE)
        require(setup.lib.ef_version().decode().startswith('edgefhe-native-ckks-v5'), 'V5 library version')

    reference = None
    for name, library, workers, fd in contexts:
        with Native(library, workers, fd) as native:
            if name != 'v4-reference':
                native.lib.ef_backend_name.argtypes = [C.c_void_p]
                native.lib.ef_backend_name.restype = C.c_char_p
                backend = native.lib.ef_backend_name(native.context).decode()
                require(backend == {'cpu-w1': 'cpu-w1', 'cpu-w4': 'cpu-w4',
                                    'fpga-simulated': 'fpga-simulated-dma-overlap'}[name], 'backend name ' + backend)
            outputs = round_trip(native, sampled, a, messages, samples, weights, bias)
        if reference is None:
            reference = outputs
            with Native(args.library, 1) as decoder:
                decoded = decoder.decode(outputs[2])
            worst = max(abs(decoded[i].real - values[0][i]) for i in range(SLOTS))
            require(worst < 4096 * 34 / SCALE, 'decrypted slots within the noise bound')
        require(outputs == reference, name + ' differs bit for bit')
        print('PASS', name, flush=True)

    with Native(args.library, 1) as native:
        try:
            native.multiply([0] * (3 * N - 1), [0] * (3 * N))
        except ValueError:
            pass
        else:
            raise AssertionError('wrong dimensions accepted')
        bad = [0] * (3 * N)
        bad[5] = 998244353
        try:
            native.multiply(words(bad), words([0] * (3 * N)))
        except ValueError as e:
            require('Noncanonical' in str(e), 'noncanonical residue error text')
        else:
            raise AssertionError('noncanonical residue accepted')
        lib = native.lib
        lib.ef_create.restype = C.c_void_p
        require(not lib.ef_create(1, -3) and b'descriptor' in lib.ef_error(), 'invalid descriptor rejected')
    print(f'V5_CAPI_PASS contexts={len(contexts)} bit_exact=1 device=0 network=0', flush=True)


if __name__ == '__main__':
    main()
