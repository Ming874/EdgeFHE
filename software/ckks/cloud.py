"""PC-only pure Python NTT oracle for ciphertext affine; no decryption keys."""
import hashlib
import time
from ntt_ref import ntt_negacyclic_mul
from profile import N,Q,DELTA,FEATURES,CASES,to_rns,add
from experiment import encode
import packets


def multiply(a,b):
    return [ntt_negacyclic_mul(x,y,q) for x,y,q in zip(a,b,Q)]


def evaluate(request):
    start=time.perf_counter_ns()
    h,polys=packets.decode(request,'request');case=CASES[h['case_id']]
    weights=[to_rns(encode(w)) for w in case['weights']]
    bias=to_rns(encode(case['bias'],DELTA*DELTA))
    c0=add(bias,*[multiply(polys[i],weights[i]) for i in range(FEATURES)])
    c1=add(*[multiply(polys[FEATURES+i],weights[i]) for i in range(FEATURES)])
    digest=hashlib.sha256(request).hexdigest()
    response=packets.encode('response',case['id'],[c0,c1],digest)
    return response,{'case_id':case['id'],'status':'PASS_PC_CIPHERTEXT_AFFINE',
                     'evaluation_inclusive_ns':time.perf_counter_ns()-start,
                     'request_sha256':digest,'response_sha256':hashlib.sha256(response).hexdigest(),
                     'private_key_fields_received':False,'ciphertext_polynomials_received':8,
                     'ciphertext_polynomials_returned':2,'pure_CPU_products':8}
