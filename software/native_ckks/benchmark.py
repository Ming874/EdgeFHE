"""CPU-only, paired Python/C++ benchmark. No device opens, network or golden oracles inside timing."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import time
from native_api import Native, N, Q, SCALE, words
from python_baseline import Python, sample

def quantile(values,p):
    s=sorted(values);index=(len(s)-1)*p;lo=int(index);hi=math.ceil(index)
    return s[lo]*(hi-index)+s[hi]*(index-lo) if hi!=lo else s[lo]
def summary(values):return {'n':len(values),'p50_us':statistics.median(values),'p95_us':quantile(values,.95),'min_us':min(values)}
def require(ok,message):
    if not ok:raise AssertionError(message)
def main():
    p=argparse.ArgumentParser();p.add_argument('--library',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--rounds',type=int,default=30);p.add_argument('--warmups',type=int,default=3)
    p.add_argument('--cpu',type=int,help='optional single-core affinity for matched Python/C++1 phase')
    a=p.parse_args();require(a.rounds>=10 and a.warmups>=1,'Need >=10 pairs and warmup')
    require(not a.output.exists(),'Use a new benchmark report path')
    initial_affinity=sorted(os.sched_getaffinity(0))
    selected=a.cpu if a.cpu is not None else max(initial_affinity)
    require(selected in initial_affinity,'CPU outside allowed affinity');os.sched_setaffinity(0,{selected})
    affinity=sorted(os.sched_getaffinity(0));py=Python();rng=random.Random(20261007)
    mat,aa=sample();samples=words(mat,'b');ar=words(aa);encryption=words(mat[2*N:],'b');secret=words(mat[:N],'b')
    report={'status':'RUNNING','board_operations':0,'architecture':platform.machine(),'kernel':platform.release(),
            'python':platform.python_version(),'compiler':subprocess.check_output(['g++','--version'],text=True).splitlines()[0],
            'initial_cpu_affinity':initial_affinity,'single_comparison_cpu_affinity':affinity,
            'warmups':a.warmups,'rounds':a.rounds,'raw_latency_us':{},'summary':{},
            'limits':['CPU local only; no KV260 or FPGA performance claim','Functional custom CKKS profile; not full OpenFHE baseline',
                      'Both languages use the same scalar affine fast path or fused transform fallback; C++ also uses Shoup constants, fused untwist and Barrett point products',
                      'FFI/conversion included; public plan creation and correctness checks outside timed intervals',
                      'Crypto timings reuse identical material for paired public QA, not deployment randomness reuse',
                      'Network/TLS/cloud transport, key setup and OS sampling excluded from hot-path group; separate phases measured']}
    start=time.perf_counter_ns()
    try:
        with Native(a.library,1) as one,Native(a.library,3) as three:
            models=[1.1,-.7,.9,.4];weights=sum([one.encode([w]*2048) for w in models],[]);bias=one.encode([-.35]*2048,SCALE*SCALE)
            messages=sum([one.encode([rng.uniform(-8,8)]) for _ in range(4)],[])
            b=one.keygen(samples,ar);cipher=one.encrypt(b,ar,messages,encryption);result=one.affine(cipher,weights,bias)
            coeff=one.decrypt(result,secret)
            require(py.keygen(mat,aa)==list(b),'Python keygen preflight mismatch')
            require(py.encrypt(list(b),aa,messages,mat[2*N:])==list(cipher),'Python fused encryption mismatch')
            require(py.affine(list(cipher),weights,bias)==list(result),'Python fused affine mismatch')
            require(py.decrypt(list(result),mat[:N])==coeff,'Python CRT mismatch')
            def timed(function):
                t=time.perf_counter_ns();out=function();return out,(time.perf_counter_ns()-t)/1000
            # Encode/decode comparisons use the same operation and SIMD occupancy.
            operations={}
            for size in (1,128,2048):
                values=[rng.uniform(-4,4) for _ in range(size)];encoded=one.encode(values)
                operations[f'encode_slots_{size}']=(lambda v=values:py.encode(v),lambda v=values:one.encode(v))
                operations[f'decode_slots_{size}']=(lambda e=encoded:py.decode(e),lambda e=encoded:one.decode(e))
            operations['keygen']=(lambda:py.keygen(mat,aa),lambda:one.keygen(samples,ar))
            operations['encrypt4']=(lambda:py.encrypt(list(b),aa,messages,mat[2*N:]),lambda:one.encrypt(b,ar,messages,encryption))
            operations['cloud_affine']=(lambda:py.affine(list(cipher),weights,bias),lambda:one.affine(cipher,weights,bias))
            operations['decrypt']=(lambda:py.decrypt(list(result),mat[:N]),lambda:one.decrypt(result,secret))
            operations['OS_sampling_material']=(sample,one.sample)
            def pipeline(engine):
                mm=sum([engine.encode([v]) for v in (1.,-2.,.5,3.)],[])
                if engine is py:
                    ct=py.encrypt(list(b),aa,mm,mat[2*N:]);rr=py.affine(ct,weights,bias)
                    return py.decode(py.decrypt(rr,mat[:N]),SCALE*SCALE)
                ct=engine.encrypt(b,ar,mm,encryption);rr=engine.affine(ct,weights,bias)
                return engine.decode(engine.decrypt(rr,secret),SCALE*SCALE)
            operations['CPU_math_hot_path']=(lambda:pipeline(py),lambda:pipeline(one))
            for name,(pf,cf) in operations.items():
                raw={'python':[],'cpp1':[]}
                for r in range(-a.warmups,a.rounds):
                    order=('python','cpp1') if r%2==0 else ('cpp1','python')
                    for label in order:
                        _,duration=timed(pf if label=='python' else cf)
                        if r>=0:raw[label].append(duration)
                report['raw_latency_us'][name]=raw
                s={k:summary(v) for k,v in raw.items()};s['python_over_cpp1_p50']=s['python']['p50_us']/s['cpp1']['p50_us']
                report['summary'][name]=s
                print(f"CPU_BENCH_PHASE_PASS phase={name} Python_p50_us={s['python']['p50_us']:.3f} Cpp1_p50_us={s['cpp1']['p50_us']:.3f} ratio={s['python_over_cpp1_p50']:.3f}",flush=True)
            # Three worker CPU results use the same inherited allowed cores, clearly separate from core-pinned comparison.
            os.sched_setaffinity(0,set(initial_affinity));report['parallel_comparison_cpu_affinity']=sorted(os.sched_getaffinity(0))
            raw={'cpp1':[],'cpp3':[]}
            for r in range(-a.warmups,a.rounds):
                for label,engine in ([('cpp1',one),('cpp3',three)] if r%2==0 else [('cpp3',three),('cpp1',one)]):
                    out,duration=timed(lambda e=engine:pipeline(e))
                    require(max(abs(x-y) for x,y in zip(out,pipeline(one)))<=1e-8,'Parallel hot-path mismatch')
                    if r>=0:raw[label].append(duration)
            report['raw_latency_us']['CPU_cpp_parallel_hot_path']=raw
            s={k:summary(v) for k,v in raw.items()};s['cpp1_over_cpp3_p50']=s['cpp1']['p50_us']/s['cpp3']['p50_us']
            report['summary']['CPU_cpp_parallel_hot_path']=s
            full_data=[[rng.uniform(-4,4) for _ in range(2048)] for _ in range(4)]
            def full_pipeline(engine):
                mm=sum([engine.encode(v) for v in full_data],[])
                ct=engine.encrypt(b,ar,mm,encryption);rr=engine.affine(ct,weights,bias)
                return engine.decode(engine.decrypt(rr,secret),SCALE*SCALE)
            raw={'cpp1':[],'cpp3':[]}
            for r in range(-a.warmups,a.rounds):
                for label,engine in ([('cpp1',one),('cpp3',three)] if r%2==0 else [('cpp3',three),('cpp1',one)]):
                    decoded,duration=timed(lambda e=engine:full_pipeline(e))
                    # Outside timing: exact input semantics, broad analytic functional error bound.
                    expected=[sum(full_data[f][i]*models[f] for f in range(4))-.35 for i in range(2048)]
                    require(max(abs(z-v) for z,v in zip(decoded,expected))<5e-4,'Full-slot CPU pipeline failed')
                    if r>=0:raw[label].append(duration)
            report['raw_latency_us']['CPU_cpp_full_slot_hot_path']=raw
            report['summary']['CPU_cpp_full_slot_hot_path']={k:summary(v) for k,v in raw.items()}
            report['status']='PYTHON_CPP_PAIRED_CPU_BENCHMARK_PASS'
            print(report['status'],f"rounds={a.rounds} workers=1,3 cpp_parallel_ratio={s['cpp1_over_cpp3_p50']:.3f} board=0",flush=True)
    except Exception as e:report['status']='FAIL';report['failure']=repr(e);raise
    finally:
        os.sched_setaffinity(0,set(initial_affinity));report['duration_ns']=time.perf_counter_ns()-start
        report['source_sha256']={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in Path(__file__).parent.glob('*') if f.is_file()}
        report['library_sha256']=hashlib.sha256(a.library.read_bytes()).hexdigest()
        a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+'\n')
if __name__=='__main__':main()
