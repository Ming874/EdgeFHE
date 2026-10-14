"""Offline protocol/config negative tests; no sockets, device opens or key material."""
import hashlib
import json
from pathlib import Path
import struct
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from protocol import encode,decode,MAGIC,N,Q
from net_config import endpoint

def reframed(header,payload):
    h=json.dumps(header,separators=(',',':')).encode('ascii')
    header_bytes=MAGIC+struct.pack('<I',len(h))+h+payload
    return header_bytes+hashlib.sha256(header_bytes).digest()

def main():
    raw=encode('request','a'*32,'model-public',1,[0]*(8*3*N));header,words=decode(raw,'request')
    length=struct.unpack_from('<I',raw,8)[0];payload=raw[12+length:-32]
    rejected=0
    def reject(value,kind='request',**kwargs):
        nonlocal rejected
        try:decode(value,kind,**kwargs)
        except (ValueError,UnicodeError):rejected+=1
        else:raise AssertionError('Malformed packet accepted')
    for field,value in [('version',True),('degree',True),('records',0),('records',2049),('records',True),
                        ('scale',2**25),('profile_id','other-profile'),('request_id','fixture-00'),
                        ('model_id','../../file'),('primes',[True]+list(Q[1:])),('polynomial_count',2)]:
        changed=dict(header);changed[field]=value;reject(reframed(changed,payload))
    for field in ('plaintext','features','private_key','decoded_logit'):
        changed=dict(header);changed[field]=0;reject(reframed(changed,payload))
    changed=bytearray(payload);struct.pack_into('<I',changed,0,Q[0])
    h=dict(header,payload_sha256=hashlib.sha256(changed).hexdigest());reject(reframed(h,bytes(changed)))
    for altered in (raw[:-1],raw+b'\0',b'wrongmagic'+raw[10:],raw[:44],raw[:12]+b'x'+raw[13:]):reject(altered)
    h=dict(header,payload_sha256=hashlib.sha256(payload[:-4]).hexdigest());reject(reframed(h,payload[:-4]))
    response=encode('response','a'*32,'model-public',1,[0]*(2*3*N),hashlib.sha256(raw).hexdigest())
    reject(response,'response',request_id='b'*32);reject(response,'response',request_sha='0'*64)
    # Deliberately duplicate a key while maintaining framing and digest validity.
    duplicate=json.dumps(header,separators=(',',':'))[:-1]+',"records":1}'
    b=MAGIC+struct.pack('<I',len(duplicate))+duplicate.encode()+payload;reject(b+hashlib.sha256(b).digest())
    for value in ('https://localhost:0','https://localhost:65536','https://localhost:','http://localhost:123',
                  'https://user:secret@localhost:123','https://localhost:123?','https://localhost:123#',' https://localhost:123'):
        try:endpoint(value)
        except ValueError:rejected+=1
        else:raise AssertionError('Malformed endpoint accepted')
    for value,port in [('https://localhost:18087',18087),('https://localhost:25443',25443),('https://localhost',443),('https://[::1]:9443',9443)]:
        assert endpoint(value)[1]==port
    print(f'V4_PROTOCOL_OFFLINE_NEGATIVE_PASS rejected={rejected} valid_alternate_ports=4 no_DMA no_network')
if __name__=='__main__':main()
