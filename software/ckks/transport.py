"""Pinned TLS transport. Verify certificate before sending the bearer token."""
import hashlib
import http.client
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit
from profile import require,CASES
from packets import MAX_BYTES


class Client:
    def __init__(self,config,certificate):
        self.config=config;self.certificate=Path(certificate)
        u=urlsplit(config['server_url'])
        require(u.scheme=='https' and u.path=='' and u.query=='' and u.fragment=='' and u.username is None and u.hostname is not None and u.port==18086,'Wrong TLS endpoint')
        require(config['protocol']=='edgefhe-ii1-affine-tls-v1' and len(config['token'])==64,'Wrong join protocol')
        self.host=u.hostname;self.port=u.port
        self.context=ssl.create_default_context(cafile=str(self.certificate))
        self.context.minimum_version=ssl.TLSVersion.TLSv1_2

    def call(self,path,body=None):
        c=http.client.HTTPSConnection(self.host,self.port,context=self.context,timeout=120)
        try:
            c.connect()
            actual=hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest()
            require(actual==self.config['certificate_der_sha256'],'TLS certificate pin mismatch before authorization')
            headers={'Authorization':'Bearer '+self.config['token'],'Connection':'close'}
            if body is not None:
                require(len(body)<=MAX_BYTES,'Outgoing body too large');headers['Content-Type']='application/octet-stream'
            c.request('GET' if body is None else 'POST',path,body=body,headers=headers)
            r=c.getresponse();length=r.getheader('Content-Length')
            require(length is not None and length.isdigit() and int(length)<=MAX_BYTES,'Invalid response length')
            data=r.read(int(length)+1)
            require(r.status==200 and len(data)==int(length),'TLS server rejected request or truncated response')
            return data
        finally:c.close()

    def ready(self,completed):
        data=json.loads(self.call('/ready'))
        require(data=={'protocol':'edgefhe-ii1-affine-tls-v1','server_id':self.config['server_id'],
                       'case_ids_completed':completed,'fixtures':CASES},'PC readiness/fixtures/history mismatch')
        return data

    def evaluate(self,request):return self.call('/evaluate',request)
