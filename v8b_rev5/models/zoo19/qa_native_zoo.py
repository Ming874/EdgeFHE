"""LOCAL ONLY (WSL Ubuntu-24.04): run every zoo model through the reviewed C++ CKKS pipeline (CPU and bit-exact PL spec).
Builds native_pipeline_20261020/pipeline.cpp UNCHANGED (read-only source) against the reviewed g++-13 libraries with a
generated catalogue = the 3 frozen catalogue entries copied byte-for-byte + every admitted zoo model (same generator
format as native_pipeline_20261020/qa_local.py). Each model: cpu and spec, 2048 rows x 2 rounds of synthetic |z|<=1
inputs; the C++ oracle compares decrypted scores with the plaintext affine (pipeline.cpp hard limit 5e-4).
Not ARM, not PL hardware, not network. usage: python3 -B qa_native_zoo.py"""
import hashlib, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent; REC = HERE.parent
PIPE = REC / 'native_pipeline_20261020'; LIBS = REC / 'p1_application_20261019d/local_v4.c1yti07s/g++-13'
FROZEN = REC / 'r6_soak_100m_20261027/package_v2/candidate/models/model_catalog.hpp'
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    out = HERE / (sys.argv[1] if len(sys.argv) > 1 else 'native_qa'); out.mkdir(exist_ok=False); gen = out / 'generated'; gen.mkdir()
    training = json.loads((HERE / 'models_v1/ZOO_TRAINING.json').read_text(encoding='utf-8'))
    admitted = [m['model_id'] for m in training['models'] if m['status'] == 'TRAINED_ADMISSIBLE']
    frozen_lines = [l for l in FROZEN.read_text().splitlines() if l.startswith('Model{')]
    header = '#pragma once\ninline std::vector<Model> model_catalog(){return {\n' + ''.join(l + '\n' for l in frozen_lines)
    for mid in admitted:
        path = HERE / 'models_v1' / (mid + '.json'); m = json.loads(path.read_text(encoding='utf-8'))
        lit = lambda v: '{' + ','.join(float(x).hex() for x in v) + '}'
        parts = [json.dumps(mid), json.dumps(sha(path)), '{' + ','.join(json.dumps(x) for x in m['features']) + '}', lit(m['mean']), lit(m['std']), lit(m['weights']),
                 '{' + ','.join(str(x) + 'LL' for x in m['weights_int']) + '}', float(m['bias']).hex(), str(m['bias_int']) + 'LL', '0', '0']
        header += 'Model{' + ','.join(parts) + '},\n'
    header += '};}\n'; (gen / 'model_catalog.hpp').write_bytes(header.encode())
    log = lambda tag, r: (out / (tag + '.log')).write_text(r.stdout + r.stderr)
    build = out / 'build'
    r = subprocess.run(['cmake', '-S', str(PIPE), '-B', str(build), '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=g++-13',
                        '-DAPP_BUILD=' + str(LIBS), '-DMODEL_INCLUDE=' + str(gen), '-DSANITIZE=OFF'], capture_output=True, text=True); log('configure', r)
    assert r.returncode == 0, r.stderr[-2000:]
    r = subprocess.run(['cmake', '--build', str(build), '-j', '4'], capture_output=True, text=True); log('build', r)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    exe = build / 'edgefhe_native_pipeline'; plugin = LIBS / 'frozen_build/baseline/libedgefhe_os_prng.so'
    ids = [re.match(r'Model\{"([^"]+)"', l).group(1) for l in frozen_lines] + admitted; cases = []
    for mid in ids:
        for backend in ('cpu', 'spec'):
            p = subprocess.run([str(exe), '--model', mid, '--backend', backend, '--plugin', str(plugin), '--rows', '2048', '--rounds', '2', '--synthetic'],
                               capture_output=True, text=True); log(mid + '_' + backend, p)
            rep = json.loads(p.stdout) if p.returncode == 0 else {}
            ok = p.returncode == 0 and rep.get('status') == 'NATIVE_CPP_LOCAL_MATH_CPU_SPEC_PASS' and rep.get('real_PL') is False and rep['max_observed_error'] <= 5e-4
            cases.append(dict(model=mid, backend=backend, ok=ok, rc=p.returncode, max_observed_error=rep.get('max_observed_error'),
                              stderr_tail=p.stderr.strip()[-200:]))
            print(('PASS' if ok else 'FAIL'), mid, backend, rep.get('max_observed_error'), p.stderr.strip()[-120:], flush=True)
    res = dict(status='ZOO_NATIVE_CPP_CKKS_CPU_SPEC_PASS_NOT_ARM_NOT_PL' if all(c['ok'] for c in cases) else 'ZOO_NATIVE_CPP_FINDINGS',
               models=len(ids), cases=cases, pipeline_source_sha256=sha(PIPE / 'pipeline.cpp'), catalogue_sha256=sha(gen / 'model_catalog.hpp'),
               frozen_entries_copied=len(frozen_lines), binary_sha256=sha(exe), rows=2048, rounds=2, input='synthetic |z|<=1',
               oracle='C++ decrypted vs plaintext affine, hard limit 5e-4', new_ARM_or_PL_acceptance=False, assistant_board_operations=0)
    (out / 'ACCEPTANCE.json').write_text(json.dumps(res, indent=1) + '\n'); print(res['status'])


if __name__ == '__main__':
    main()
