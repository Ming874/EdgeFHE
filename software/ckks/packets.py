"""Fixed binary ciphertext-only packet schema; no secret/plaintext fields."""
from array import array
import hashlib
import json
import struct
import sys
from profile import N,Q,DELTA,FEATURES,require,validate

MAGIC=b'EFII1C1\0'
MAX_BYTES=512*1024
COMMON={'schema','version','kind','case_id','degree','primes','scale','feature_count','polynomial_count','payload_sha256'}


def digest(data):return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result={}
    for k,v in pairs:
        require(k not in result,'Duplicate JSON key');result[k]=v
    return result


def encode(kind,case_id,polys,request_sha=None):
    require(kind in ('request','response') and type(case_id) is int and 0<=case_id<6,'Invalid packet scope')
    require(len(polys)==(2*FEATURES if kind=='request' else 2),'Wrong ciphertext count')
    words=array('I')
    require(words.itemsize==4,'Expected uint32 array')
    for p in polys:
        validate(p)
        for l in p:words.extend(l)
    if sys.byteorder!='little':words.byteswap()
    payload=words.tobytes()
    header={'schema':'edgefhe.ii1.ckks.affine','version':1,'kind':kind,'case_id':case_id,
            'degree':N,'primes':list(Q),'scale':DELTA if kind=='request' else DELTA*DELTA,
            'feature_count':FEATURES,'polynomial_count':len(polys),'payload_sha256':digest(payload)}
    if kind=='response':
        require(isinstance(request_sha,str) and len(request_sha)==64 and all(c in '0123456789abcdef' for c in request_sha),'Invalid request binding')
        header['request_sha256']=request_sha
    h=json.dumps(header,sort_keys=True,separators=(',',':'),allow_nan=False).encode('ascii')
    raw=MAGIC+struct.pack('<I',len(h))+h+payload
    require(len(raw)+32<=MAX_BYTES,'Packet too large')
    return raw+hashlib.sha256(raw).digest()


def decode(raw,kind,case_id=None,request_sha=None):
    require(kind in ('request','response'),'Invalid expected packet kind')
    require(isinstance(raw,bytes) and 44<len(raw)<=MAX_BYTES and raw[:8]==MAGIC,'Invalid packet framing')
    require(hashlib.sha256(raw[:-32]).digest()==raw[-32:],'Packet integrity failure')
    length=struct.unpack_from('<I',raw,8)[0]
    require(0<length<=4096 and 12+length<len(raw)-32,'Invalid header length')
    header=json.loads(raw[12:12+length].decode('ascii'),object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    require(type(header) is dict and set(header)==COMMON|({'request_sha256'} if kind=='response' else set()),'Unknown/private/missing header field')
    require(header['schema']=='edgefhe.ii1.ckks.affine' and type(header['version']) is int and header['version']==1 and header['kind']==kind,'Wrong packet schema/version/kind')
    require(type(header['case_id']) is int and 0<=header['case_id']<6,'Invalid case')
    if case_id is not None:require(header['case_id']==case_id,'Wrong response case')
    require(type(header['degree']) is int and header['degree']==N and header['primes']==list(Q),'Wrong ring profile')
    require(type(header['scale']) is int and header['scale']==(DELTA if kind=='request' else DELTA*DELTA),'Wrong scale')
    require(type(header['feature_count']) is int and header['feature_count']==FEATURES and type(header['polynomial_count']) is int and header['polynomial_count']==(8 if kind=='request' else 2),'Wrong counts')
    if kind=='response':require(header['request_sha256']==request_sha and request_sha is not None,'Response belongs to another request')
    payload=raw[12+length:-32]
    require(len(payload)==header['polynomial_count']*3*N*4 and digest(payload)==header['payload_sha256'],'Wrong payload size/hash')
    words=array('I');words.frombytes(payload)
    if sys.byteorder!='little':words.byteswap()
    polys=[];pos=0
    for _ in range(header['polynomial_count']):
        p=[]
        for _ in range(3):p.append(list(words[pos:pos+N]));pos+=N
        validate(p);polys.append(p)
    require(pos==len(words),'Trailing payload')
    return header,polys
