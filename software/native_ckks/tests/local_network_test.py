"""Local loopback mTLS integration. No board endpoints or private-input logging."""
import argparse
import ctypes as C
import hashlib
import json
import math
from pathlib import Path
import random
import socket
import subprocess
import sys
import time

SRC=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(SRC))
from native_api import Native, N, Q, words
from dynamic_client import Session
from transport import Client
from protocol import encode, decode
from net_config import endpoint

def require(ok, message):
    if not ok: raise AssertionError(message)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--library',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    library=args.library.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    private=out/'private_tls'
    from local_tls_material import prepare
    prepare(private)
    report={'status':'RUNNING','board_operations':0,'network_scope':'127.0.0.1 only', 'failures':[]}
    rng=random.Random(90233); jobs=[]; passed=0
    try:
        for run in range(2):
            with socket.socket() as s: s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
            cfg=json.loads((private/'config.json').read_text());cfg['server_url']=f'https://127.0.0.1:{port}'
            path=private/f'config_{run}.json'; path.write_text(json.dumps(cfg))
            audit=out/f'audit_{run}.jsonl'; console=out/f'server_{run}.log'
            with console.open('w') as log:
                process=subprocess.Popen([sys.executable,str(SRC/'pc_server.py'),'--config',str(path),'--private-dir',str(private),
                                          '--model',str(SRC/'model.example.json'),'--library',str(library),'--audit',str(audit),
                                          '--max-requests','4'],stdout=log,stderr=subprocess.STDOUT)
                jobs.append(process)
                for _ in range(100):
                    if 'DYNAMIC_CIPHERTEXT_SERVER_READY' in console.read_text(): break
                    require(process.poll() is None,'Server startup failure');time.sleep(.05)
                else: raise AssertionError('Server readiness timeout')
                client=Client(cfg,private/'server_ca.pem',private)
                require('fixtures' not in client.ready(),'Fixture readiness leak')
                with Native(library,3) as native:
                    session=Session(native,client,SRC/'model.example.json')
                    # Includes full SIMD batch and fresh input values unknown to the service.
                    for size in (1,17,2048):
                        records=[[rng.uniform(-8,8) for _ in range(4)] for _ in range(size)]
                        result=session.run(records)
                        max_error=max(abs(r['logit']-(sum(x*w for x,w in zip(row,session.model['weights']))+session.model['bias']))
                                      for r,row in zip(result['results'],records))
                        require(max_error<=session.bound,'Dynamic full-slot affine bound failed')
                        passed+=1
                        report.setdefault('dynamic_batches',[]).append({'records':size,'max_logit_error':max_error,
                                                                       'bound':session.bound,'port':port})
                    # Fourth request is a public zero ciphertext; schema still contains no input fields.
                    rid='a'*32; raw=encode('request',rid,session.model['model_id'],1,[0]*(8*3*N))
                    response=client.evaluate(raw);decode(response,'response',rid,hashlib.sha256(raw).hexdigest())
                    try: client.evaluate(raw)
                    except ValueError: passed+=1
                    else: raise AssertionError('Replay accepted')
                    overflow=encode('request','b'*32,session.model['model_id'],1,[0]*(8*3*N))
                    try: client.evaluate(overflow)
                    except ValueError: passed+=1
                    else: raise AssertionError('Full replay ledger did not fail closed')
                bad=dict(cfg,token='0'*64)
                try: Client(bad,private/'server_ca.pem',private).ready()
                except ValueError: passed+=1
                else: raise AssertionError('Wrong Authorization accepted')
                bad=dict(cfg,server_certificate_sha256='0'*64)
                try: Client(bad,private/'server_ca.pem',private).ready()
                except ValueError: passed+=1
                else: raise AssertionError('Wrong certificate pin accepted')
                bad=dict(cfg,mtls=False)
                try: Client(bad,private/'server_ca.pem',private).ready()
                except (OSError,ValueError): passed+=1
                else: raise AssertionError('Missing mTLS client certificate accepted')
                try: client.evaluate(b'not a ciphertext packet'*3)
                except ValueError: passed+=1
                else: raise AssertionError('Malformed packet accepted')
                process.terminate(); process.wait(timeout=10)
                # Audit allowlist avoids accidental private records/request bodies in server logs.
                for line in audit.read_text().splitlines():
                    obj=json.loads(line)
                    require(set(obj)<= {'event','time_ns','bind','port','mtls','model_sha256','count','records'},'Unexpected server audit field')
                require('fixtures' not in console.read_text() and 'Authorization' not in console.read_text(),'Secret/fixture console leak')
        for valid in ('https://localhost:12345','https://localhost','https://[::1]:9443'):
            require(1<=endpoint(valid)[1]<=65535,'Valid alternate port rejected');passed+=1
        for bad in ('http://localhost:123','https://localhost:0','https://localhost:65536','https://u:p@localhost:123',
                    'https://localhost:','https://localhost:abc','https://localhost:123/path','https://localhost:123?x=y'):
            try: endpoint(bad)
            except ValueError: passed+=1
            else: raise AssertionError('Invalid endpoint accepted')
        report.update(status='DYNAMIC_PRIVATE_INPUT_LOCAL_MTLS_PASS',checks=passed,fixture_dependency=False,
                      private_server_body_logging=False,role_key_isolation='process/code/API boundary; same-host loopback QA only')
        print(report['status'],f'checks={passed} dynamic_batches=6 includes_full_slots=2 configurable_ports=2 board=0',flush=True)
    except Exception as e:
        report['status']='FAIL';report['failures'].append(repr(e));raise
    finally:
        for p in jobs:
            if p.poll() is None:p.terminate();p.wait(timeout=10)
        (out/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':main()
