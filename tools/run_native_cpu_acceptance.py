"""Operator-run CPU acceptance on Linux host or KV260. Never opens the DMA device."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=30);parser.add_argument('--dense-cases',type=int,default=1000)
    parser.add_argument('--openfhe-dir',type=Path,help='optional locally installed OpenFHE CONFIG directory')
    args=parser.parse_args()
    if platform.system()!='Linux':raise RuntimeError('Run in Linux or WSL; Windows binaries are not board binaries')
    if args.rounds<10 or args.dense_cases<1000:raise ValueError('Acceptance needs >=10 benchmark pairs and >=1000 distinct dense cases')
    output=args.output.resolve()
    if output.exists():raise ValueError('Use a new evidence directory')
    output.mkdir(parents=True,exist_ok=False);build=output/'build';src=ROOT/'software/native_ckks'
    report={'status':'RUNNING','environment':platform.uname()._asdict(),'board_DMA_operations':0,
            'module_insertions':0,'FPGA_loads':0,'network_connections':0,'started_ns':time.time_ns(),
            'candidate_version':json.loads((ROOT/'release/NATIVE_CANDIDATE.json').read_text()),'commands':[]}
    def run(name,command,marker=None):
        report['commands'].append({'name':name,'command':list(map(str,command))})
        result=subprocess.run(list(map(str,command)),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                              env=None)
        (output/(name+'.log')).write_text(result.stdout,encoding='utf-8')
        print(result.stdout,end='',flush=True)
        if result.returncode!=0 or marker and marker not in result.stdout:
            raise RuntimeError('Step failed: '+name+'; preserve logs; no automatic retry')
    try:
        run('inventory',[sys.executable,ROOT/'tools/verify_repository.py'],'REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS')
        run('configure',['cmake','-S',src,'-B',build,'-DCMAKE_BUILD_TYPE=Release'])
        run('build',['cmake','--build',build,'-j','4'])
        run('ctest',['ctest','--test-dir',build,'--output-on-failure'],'100% tests passed')
        run('math',[sys.executable,src/'tests/validate_native.py','--build',build,'--dense-cases',str(args.dense_cases)],
            'NATIVE_LOCAL_MATHEMATICAL_ACCEPTANCE_PASS')
        run('benchmark',[sys.executable,src/'benchmark.py','--library',build/'libedgefhe_native.so',
                         '--output',output/'benchmark_cpu.json','--rounds',str(args.rounds),'--warmups','3'],
            'PYTHON_CPP_PAIRED_CPU_BENCHMARK_PASS')
        if args.openfhe_dir:
            ob=output/'openfhe_build'
            run('openfhe_configure',['cmake','-S',ROOT/'validation/openfhe_client','-B',ob,'-DCMAKE_BUILD_TYPE=Release',
                                    '-DOpenFHE_DIR='+str(args.openfhe_dir.resolve())])
            run('openfhe_build',['cmake','--build',ob,'-j','4'])
            for workers in (1,3,4):
                run('openfhe_'+str(workers),[ob/'openfhe_client_baseline',str(workers),str(args.rounds),output/f'openfhe_{workers}.tsv'],
                    'OPENFHE_STANDARD_CLIENT_BENCHMARK_PASS')
        report['status']='NATIVE_V4_CPU_ONLY_OPERATOR_ACCEPTANCE_PASS'
        print(report['status']+' DMA=0 FPGA_load=0 no_kernel_module_changes',flush=True)
    except Exception as e:report['status']='FAIL';report['failure']=repr(e);raise
    finally:
        report['finished_ns']=time.time_ns()
        report['evidence_sha256']={p.relative_to(output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in output.rglob('*') if p.is_file() and 'CMakeFiles' not in p.relative_to(output).parts}
        (output/'cpu_acceptance_report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
