"""Fixed, functional N4096 CKKS-embedding/RLWE affine experiment profile.

This preserves the V1 sparse sampler profile for functional acceptance; it does
not assert production security or OpenFHE ciphertext interoperability.
"""
import math
import random
import secrets

N=4096
Q=(998244353,1004535809,469762049)
DELTA=1<<24
FEATURES=4
SECRET_WEIGHT=64
MASK_WEIGHT=64
ERROR_WEIGHT=16
NUMERIC_GUARD=1e-8
CASES=[
 {'id':0,'name':'zero','features':[0.,0.,0.,0.],'weights':[1.1,-.7,.9,.4],'bias':0.},
 {'id':1,'name':'positive','features':[.8,-1.2,.5,2.],'weights':[1.1,-.7,.9,.4],'bias':-.35},
 {'id':2,'name':'negative','features':[-.8,1.2,-.5,-2.],'weights':[1.1,-.7,.9,.4],'bias':-.35},
 {'id':3,'name':'mixed-range','features':[-8.,.125,2.,-3.5],'weights':[.25,-2.,1.5,.125],'bias':.75},
 {'id':4,'name':'near-boundary','features':[.1,.2,-.3,.4],'weights':[.5,-1.,.25,.125],'bias':.15},
 {'id':5,'name':'at-boundary','features':[.1,.2,.3,.4],'weights':[.5,-1.,.25,.125],'bias':.025},
]
JOBS_PER_CASE=1+2*FEATURES+FEATURES+1
DMA_JOBS=len(CASES)*JOBS_PER_CASE
HISTORY_BEFORE=873
HISTORY_AFTER=HISTORY_BEFORE+DMA_JOBS


def require(ok,message):
    if not ok:raise ValueError(message)


def sparse(rng,weight):
    require(type(weight) is int and 0<weight<=N,'Invalid sampler weight')
    out=[0]*N
    for i in rng.sample(range(N),weight):out[i]=1 if rng.randrange(2) else -1
    return out


def to_rns(coeff):
    require(len(coeff)==N and all(type(x) is int for x in coeff),'Wrong integer polynomial')
    return [[v%q for v in coeff] for q in Q]


def validate(poly):
    require(len(poly)==3 and all(len(l)==N for l in poly),'Wrong RNS dimensions')
    for l,q in zip(poly,Q):require(all(type(v) is int and 0<=v<q for v in l),'Noncanonical residue')


def add(*polys):
    for p in polys:validate(p)
    require(bool(polys),'Empty RNS sum')
    return [[sum(p[l][i] for p in polys)%q for i in range(N)] for l,q in enumerate(Q)]


def neg(poly):
    validate(poly)
    return [[(-v)%q for v in l] for l,q in zip(poly,Q)]


def centered(poly):
    validate(poly)
    product=math.prod(Q)
    bases=[(product//q)*pow(product//q,-1,q) for q in Q]
    out=[]
    for i in range(N):
        v=sum(poly[l][i]*bases[l] for l in range(3))%product
        out.append(v-product if v>product//2 else v)
    return out


def integer_sparse_product(dense,ternary):
    """Independent integer schoolbook in Z[X]/(X^N+1), no NTT or mod q."""
    require(len(dense)==len(ternary)==N and all(v in (-1,0,1) for v in ternary),'Invalid sparse operand')
    output=[0]*N
    for j,v in enumerate(ternary):
        if not v:continue
        cut=N-j
        for i in range(cut):output[i+j]+=dense[i]*v
        for i in range(cut,N):output[i+j-N]-=dense[i]*v
    return output


def material(test_seed=None):
    # Board caller always omits test_seed. Deterministic mode is host QA only.
    rng=secrets.SystemRandom() if test_seed is None else random.Random(test_seed)
    s=sparse(rng,SECRET_WEIGHT);e=sparse(rng,ERROR_WEIGHT)
    us=[sparse(rng,MASK_WEIGHT) for _ in range(FEATURES)]
    e0=[sparse(rng,ERROR_WEIGHT) for _ in range(FEATURES)]
    e1=[sparse(rng,ERROR_WEIGHT) for _ in range(FEATURES)]
    a=[[rng.randrange(q) for _ in range(N)] for q in Q]
    return {'s':s,'e':e,'u':us,'e0':e0,'e1':e1,'a':a,
            'randomness':'OS secrets.SystemRandom' if test_seed is None else 'deterministic host QA only'}


def noise_integer(mat,i):
    eu=integer_sparse_product(mat['e'],mat['u'][i])
    e1s=integer_sparse_product(mat['e1'][i],mat['s'])
    return [a+b+c for a,b,c in zip(eu,mat['e0'][i],e1s)]


def direct_slot(coeff,scale,k=0):
    """Independent direct complex evaluation, rather than FFT decode."""
    require(len(coeff)==N and type(scale) is int and scale>0,'Invalid direct embedding')
    angle=(2*k+1)*math.pi/N
    return complex(math.fsum((v/scale)*math.cos(angle*j) for j,v in enumerate(coeff)),
                   math.fsum((v/scale)*math.sin(angle*j) for j,v in enumerate(coeff)))


def quantization_bound(coeff,scale,expected):
    # Actual numeric FFT error is cross-checked independently; the analytic
    # rounding bound is ||rounding||_1/scale <= N/(2*scale).
    require(all(math.isfinite(x.real) and math.isfinite(x.imag) for x in expected),'Nonfinite slots')
    return N/(2*scale)+NUMERIC_GUARD


def conservative_affine_bound(case,feature_bounds):
    require(len(feature_bounds)==FEATURES,'Missing feature error bound')
    ew=N/(2*DELTA)+NUMERIC_GUARD
    eb=N/(2*DELTA*DELTA)+NUMERIC_GUARD
    return math.fsum(abs(w)*ex+abs(x)*ew+ex*ew for x,w,ex in zip(case['features'],case['weights'],feature_bounds))+eb+NUMERIC_GUARD


def sigmoid(x):
    if x>=0:return 1/(1+math.exp(-x))
    v=math.exp(x);return v/(1+v)


def classification_interval(logit,error_bound):
    require(math.isfinite(logit) and math.isfinite(error_bound) and error_bound>=0,'Invalid classification interval')
    low,high=logit-error_bound,logit+error_bound
    return {'lower_logit':low,'upper_logit':high,
            'class':1 if low>0 else 0 if high<0 else None,
            'status':'certain' if low>0 or high<0 else 'indeterminate-at-threshold'}
