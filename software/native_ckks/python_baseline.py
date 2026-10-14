"""CPU benchmark reference with cached plans and the same fused arithmetic schedule.

This is a fairer pure-Python comparison than counting validation-only oracles.
It has no fixture, network or board dependencies. OS sampling is measured separately.
"""
import importlib.util
import math
from pathlib import Path
import secrets
from native_api import N, Q, SCALE

path = Path(__file__).resolve().parents[1]/'ckks/ckks_canonical.py'
spec = importlib.util.spec_from_file_location('edgefhe_frozen_embedding_reference', path)
embedding = importlib.util.module_from_spec(spec); spec.loader.exec_module(embedding)

class Plan:
    def __init__(self, q, psi):
        self.q=q; self.ninv=pow(N,-1,q)
        self.twist=[pow(psi,i,q) for i in range(N)]; self.untwist=[pow(psi,-i,q) for i in range(N)]
        self.reverse=[int(f'{i:012b}'[::-1],2) for i in range(N)]
        omega=psi*psi%q
        self.roots=[[pow(omega,j*N//(2*h),q) for j in range(h)] for h in (1<<s for s in range(12))]
        self.inverse_roots=[[pow(omega,-j*N//(2*h),q) for j in range(h)] for h in (1<<s for s in range(12))]
    def transform(self, values, inverse=False):
        q=self.q
        twisted=list(values) if inverse else [v*w%q for v,w in zip(values,self.twist)]
        out=[twisted[i] for i in self.reverse]
        for s, roots in enumerate(self.inverse_roots if inverse else self.roots):
            h=1<<s
            for base in range(0,N,2*h):
                for j,w in enumerate(roots):
                    a,b=out[base+j],out[base+j+h]*w%q
                    out[base+j]=(a+b)%q;out[base+j+h]=(a-b)%q
        if inverse:out=[v*self.ninv%q*w%q for v,w in zip(out,self.untwist)]
        return out

def split(a):return [list(a[i*N:(i+1)*N]) for i in range(3)]
def flat(a):return sum(a,[])
def rns(m):return [[v%q for v in m] for q in Q]
def sparse(rng,weight):
    out=[0]*N
    for i in rng.sample(range(N),weight):out[i]=1 if rng.randrange(2) else -1
    return out
def sample():
    rng=secrets.SystemRandom(); secret=sparse(rng,64);error=sparse(rng,16)
    masks=[sparse(rng,64) for _ in range(4)];e0=[sparse(rng,16) for _ in range(4)];e1=[sparse(rng,16) for _ in range(4)]
    a=[rng.randrange(q) for q in Q for _ in range(N)]
    return secret+error+sum(masks,[])+sum(e0,[])+sum(e1,[]),a

class Python:
    def __init__(self):
        self.plans=[Plan(q,psi) for q,psi in zip(Q,(350007156,2062645,62025685))]
        product=math.prod(Q);self.product=product
        self.bases=[product//q*pow(product//q,-1,q) for q in Q]
    def encode(self,values,scale=SCALE):return embedding.CKKSEncoder(N,scale).encode(values)
    def decode(self,coefficients,scale=SCALE):return embedding.CKKSEncoder(N,scale).decode(coefficients)
    def multiply(self,a,b):
        result=[]
        for l,plan in enumerate(self.plans):
            aa=plan.transform(a[l*N:(l+1)*N]);bb=plan.transform(b[l*N:(l+1)*N])
            result.extend(plan.transform([x*y%Q[l] for x,y in zip(aa,bb)],True))
        return result
    def keygen(self,samples,a):
        prod=split(self.multiply(a,flat(rns(samples[:N]))));error=rns(samples[N:2*N])
        return [(-v+e)%q for l,q in enumerate(Q) for v,e in zip(prod[l],error[l])]
    def encrypt(self,b,a,messages,samples):
        c0=[None]*4;c1=[None]*4;aa=split(a);bb=split(b)
        for l,(plan,q) in enumerate(zip(self.plans,Q)):
            ap=plan.transform(aa[l]);bp=plan.transform(bb[l])
            for f in range(4):
                up=plan.transform([v%q for v in samples[f*N:(f+1)*N]])
                bu=plan.transform([x*y%q for x,y in zip(bp,up)],True);au=plan.transform([x*y%q for x,y in zip(ap,up)],True)
                if l==0:c0[f]=[];c1[f]=[]
                c0[f].extend((v+e+m)%q for v,e,m in zip(bu,samples[(4+f)*N:(5+f)*N],messages[f*N:(f+1)*N]))
                c1[f].extend((v+e)%q for v,e in zip(au,samples[(8+f)*N:(9+f)*N]))
        return sum(c0+c1,[])
    def affine(self,cipher,weights,bias):
        out0=[];out1=[]
        constant=all(not any(weights[f*N+1:(f+1)*N]) for f in range(4))
        for l,(plan,q) in enumerate(zip(self.plans,Q)):
            if constant:
                a=[v%q for v in bias];b=[0]*N
                for f in range(4):
                    w=weights[f*N]%q
                    a=[(x+y*w)%q for x,y in zip(a,cipher[(f*3+l)*N:(f*3+l+1)*N])]
                    b=[(x+y*w)%q for x,y in zip(b,cipher[((4+f)*3+l)*N:((4+f)*3+l+1)*N])]
                out0.extend(a);out1.extend(b);continue
            s0=[0]*N;s1=[0]*N
            for f in range(4):
                w=plan.transform([v%q for v in weights[f*N:(f+1)*N]])
                a=plan.transform(cipher[(f*3+l)*N:(f*3+l+1)*N]);b=plan.transform(cipher[((4+f)*3+l)*N:((4+f)*3+l+1)*N])
                s0=[(x+y*z)%q for x,y,z in zip(s0,a,w)];s1=[(x+y*z)%q for x,y,z in zip(s1,b,w)]
            out0.extend((v+b)%q for v,b in zip(plan.transform(s0,True),bias));out1.extend(plan.transform(s1,True))
        return out0+out1
    def decrypt(self,cipher,secret):
        p=self.multiply(cipher[3*N:],flat(rns(secret)));a=cipher[:3*N]
        residue=[(x+y)%Q[l] for l in range(3) for x,y in zip(a[l*N:(l+1)*N],p[l*N:(l+1)*N])]
        values=[sum(residue[l*N+i]*self.bases[l] for l in range(3))%self.product for i in range(N)]
        return [v-self.product if v>self.product//2 else v for v in values]
