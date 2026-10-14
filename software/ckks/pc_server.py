"""PC ciphertext-only affine TLS server. No board connections are initiated."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
import secrets
import ssl
import sys
import time
from profile import CASES,require
from packets import MAX_BYTES,decode
from cloud import evaluate


def serve(config_path,private_dir,evidence,bind):
    config=json.loads(config_path.read_text(encoding='utf-8'))
    require(evidence.is_dir() and not any(evidence.iterdir()),'Fresh PC evidence directory required')
    completed=[]
    def event(value):
        with (evidence/'server_events.jsonl').open('a',encoding='utf-8',newline='\n') as f:
            f.write(json.dumps(value,sort_keys=True,allow_nan=False)+'\n');f.flush()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass  # Never log Authorization headers or packet bodies.
        def send(self,status,body,content='application/json'):
            self.send_response(status);self.send_header('Content-Type',content)
            self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
            self.end_headers();self.wfile.write(body);self.close_connection=True
        def auth(self):
            ok=secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+config['token'])
            if not ok:self.send(401,b'{"error":"authentication required"}')
            return ok
        def do_GET(self):
            if not self.auth():return
            if self.path!='/ready':self.send(404,b'{}');return
            self.send(200,json.dumps({'protocol':config['protocol'],'server_id':config['server_id'],
                                     'case_ids_completed':completed,'fixtures':CASES},allow_nan=False).encode('ascii'))
        def do_POST(self):
            if not self.auth():return
            if self.path!='/evaluate':self.send(404,b'{}');return
            try:
                lengths=self.headers.get_all('Content-Length',[])
                require(len(lengths)==1 and lengths[0].isdigit() and 44<int(lengths[0])<=MAX_BYTES,'Invalid request length')
                require(self.headers.get('Transfer-Encoding') is None and self.headers.get('Content-Type')=='application/octet-stream','Wrong request framing')
                self.connection.settimeout(30)
                request=self.rfile.read(int(lengths[0]));require(len(request)==int(lengths[0]),'Truncated request')
                header,_=decode(request,'request');i=header['case_id']
                require(i==len(completed) and len(completed)<len(CASES),'Out-of-order/replayed case')
                response,result=evaluate(request)
                # Preserve original packet bytes; ciphertexts only, no keys/plaintexts.
                with (evidence/f'case{i:02d}_request.bin').open('xb') as f:f.write(request)
                with (evidence/f'case{i:02d}_response.bin').open('xb') as f:f.write(response)
                completed.append(i);event(result)
                self.send(200,response,'application/octet-stream')
                print(f'PC_AFFINE_CASE_PASS case={i} ciphertext_products=8',flush=True)
                if len(completed)==len(CASES):
                    summary={'status':'PASS_PC_SIX_CIPHERTEXT_AFFINE_RESPONSES_ONLY',
                             'server_id':config['server_id'],'cases':completed,
                             'limits':['Board decryption/IRQ/identity acceptance is separate'],
                             'evidence_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(evidence.iterdir()) if p.is_file()}}
                    with (evidence/'pc_report.json').open('x',encoding='utf-8',newline='\n') as f:json.dump(summary,f,indent=2);f.write('\n')
                    print('PC_SIX_CIPHERTEXT_AFFINE_RESPONSES_PASS',flush=True)
            except (ValueError,KeyError,UnicodeError,json.JSONDecodeError,TimeoutError) as exc:
                event({'status':'REJECTED','error_type':type(exc).__name__})
                self.send(400,b'{"error":"packet or sequence rejected"}')
    server=HTTPServer((bind,18086),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(private_dir/'server_cert.pem'),str(private_dir/'server_key.pem'))
    server.socket=context.wrap_socket(server.socket,server_side=True)
    event({'status':'SERVER_READY','server_id':config['server_id'],'bind':bind,'port':18086,
           'initiated_board_connections':0,'timestamp_unix':time.time()})
    print(f'PC_TLS_SERVER_READY bind={bind} port=18086 server_id={config["server_id"]}',flush=True)
    try:server.serve_forever(poll_interval=.5)
    finally:server.server_close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True)
    p.add_argument('--private-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--bind',required=True);a=p.parse_args()
    serve(a.config,a.private_dir,a.evidence,a.bind)
