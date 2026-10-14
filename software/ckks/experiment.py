"""Functional initial CKKS embedding/RLWE affine acceptance, with exact oracles."""
import hashlib
import math
import time
from pathlib import Path
from ckks_canonical import CKKSEncoder
from profile import (N,Q,DELTA,FEATURES,NUMERIC_GUARD,CASES,require,material,to_rns,
    add,neg,centered,integer_sparse_product,noise_integer,direct_slot,
    conservative_affine_bound,sigmoid,classification_interval)
import packets


def encode(value,scale=DELTA):
    require(math.isfinite(value),'Nonfinite plaintext')
    return CKKSEncoder(N,scale).encode([complex(value)])


def sparse_rns_product(poly,mask):
    # Independent integer schoolbook per limb, without CPU/FPGA NTT reuse.
    return [[v%q for v in integer_sparse_product(limb,mask)] for limb,q in zip(poly,Q)]


def decode_check(coeff,scale,expected,bound):
    decoded=CKKSEncoder(N,scale).decode(coeff)
    require(len(decoded)==N//2,'Wrong decoded slot count')
    err=max(abs(z-(expected if i==0 else 0)) for i,z in enumerate(decoded))
    require(err<=bound,'CKKS slot error exceeds conservative analytic bound')
    disagreement=max(abs(decoded[k]-direct_slot(coeff,scale,k)) for k in (0,1,N//2-1))
    require(disagreement<=NUMERIC_GUARD,'FFT/direct embedding numerical disagreement')
    return {'decoded_slot0_real':decoded[0].real,'decoded_slot0_imag':decoded[0].imag,
            'max_error_all_2048_slots':err,'error_bound':bound,
            'direct_embedding_max_disagreement_3_slots':disagreement}


def run_case(case,backend,evaluator,output=None,test_seed=None):
    start=time.perf_counter_ns(); phases={}
    record_start=len(backend.records)
    t=time.perf_counter_ns();mat=material(test_seed);s=to_rns(mat['s']);e=to_rns(mat['e'])
    messages=[encode(x) for x in case['features']]
    phases['PS_sampling_and_encoding_ns']=time.perf_counter_ns()-t
    t=time.perf_counter_ns()
    product=backend.multiply(mat['a'],s,f"case{case['id']}/keygen/as")
    require(product==sparse_rns_product(mat['a'],mat['s']),'Keygen independent sparse oracle mismatch')
    public_b=add(neg(product),e)
    phases['keygen_with_exact_oracles_ns']=time.perf_counter_ns()-t
    c0=[];c1=[];feature_reports=[];feature_bounds=[];noisy_messages=[]
    t=time.perf_counter_ns()
    for i,message in enumerate(messages):
        u=to_rns(mat['u'][i]);label=f"case{case['id']}/feature{i}"
        bu=backend.multiply(public_b,u,label+'/bu')
        au=backend.multiply(mat['a'],u,label+'/au')
        require(bu==sparse_rns_product(public_b,mat['u'][i]),'Encryption b*u independent sparse oracle mismatch')
        require(au==sparse_rns_product(mat['a'],mat['u'][i]),'Encryption a*u independent sparse oracle mismatch')
        c0.append(add(bu,to_rns(mat['e0'][i]),to_rns(message)))
        c1.append(add(au,to_rns(mat['e1'][i])))
        # Diagnostic decryption counts as one DMA per feature; no hidden jobs.
        recovered=centered(add(c0[-1],backend.multiply(c1[-1],s,label+'/diagnostic_decrypt')))
        noise=noise_integer(mat,i)
        exact=[m+v for m,v in zip(message,noise)]
        upper=max(map(abs,message))+max(map(abs,noise))
        require(2*upper<math.prod(Q),'Message/noise may wrap centered CRT')
        require(recovered==exact,'Exact RLWE cancellation m+e*u+e0+e1*s failed')
        noise_l1=sum(map(abs,noise))
        require(noise_l1<=16*64+16+16*64,'Sparse noise L1 bound failed')
        bound=N/(2*DELTA)+noise_l1/DELTA+NUMERIC_GUARD
        decoded=decode_check(recovered,DELTA,case['features'][i],bound)
        decoded.update(exact_noise_identity=True,noise_l1=noise_l1,
                       no_wrap_coefficient_upper_bound=str(upper))
        feature_reports.append(decoded);feature_bounds.append(bound);noisy_messages.append(exact)
    phases['encrypt_and_diagnostic_decrypt_with_oracles_ns']=time.perf_counter_ns()-t
    t=time.perf_counter_ns();request=packets.encode('request',case['id'],c0+c1)
    if output is not None:(output/f"case{case['id']:02d}_request.bin").write_bytes(request)
    response=evaluator(request)
    request_sha=hashlib.sha256(request).hexdigest()
    _,result=packets.decode(response,'response',case['id'],request_sha)
    if output is not None:(output/f"case{case['id']:02d}_response.bin").write_bytes(response)
    phases['packet_TLS_and_PC_evaluation_ns']=time.perf_counter_ns()-t
    t=time.perf_counter_ns()
    weights=[encode(w) for w in case['weights']];bias=encode(case['bias'],DELTA*DELTA)
    gold0=add(to_rns(bias),*[backend.cpu(c0[i],to_rns(weights[i])) for i in range(FEATURES)])
    gold1=add(*[backend.cpu(c1[i],to_rns(weights[i])) for i in range(FEATURES)])
    require(result==[gold0,gold1],'Independent PC ciphertext affine disagrees with C++ oracle')
    # ||m*w||inf <= ||m||1*||w||1 is conservative but valid in the negacyclic ring.
    nowrap=sum(sum(map(abs,m))*sum(map(abs,w)) for m,w in zip(noisy_messages,weights))+max(map(abs,bias))
    require(2*nowrap<math.prod(Q),'Affine centered CRT no-wrap bound failed')
    recovered=centered(add(result[0],backend.multiply(result[1],s,f"case{case['id']}/affine_decrypt")))
    gold_recovered=centered(add(gold0,backend.cpu(gold1,s)))
    require(recovered==gold_recovered and max(map(abs,recovered))<=nowrap,'Final exact CRT decryption mismatch')
    expected=math.fsum(x*w for x,w in zip(case['features'],case['weights']))+case['bias']
    bound=conservative_affine_bound(case,feature_bounds)
    decoded=decode_check(recovered,DELTA*DELTA,expected,bound)
    decision=classification_interval(decoded['decoded_slot0_real'],bound)
    probability=sigmoid(decoded['decoded_slot0_real']);expected_probability=sigmoid(expected)
    require(abs(probability-expected_probability)<=bound/4+NUMERIC_GUARD,'Sigmoid Lipschitz error bound failed')
    if decision['class'] is not None:require(decision['class']==int(expected>0),'Certain classification mismatch')
    else:require(abs(expected)<=2*bound,'Unexpected indeterminate classification far from threshold')
    if case['name']=='at-boundary':require(decision['class'] is None,'Threshold case must retain uncertainty')
    phases['response_exact_oracle_and_decrypt_ns']=time.perf_counter_ns()-t
    records=backend.records[record_start:]
    require(backend.fd is None or len(records)==14,'Unexpected DMA count per case')
    return {'id':case['id'],'name':case['name'],'status':'PASS','randomness':mat['randomness'],
        'private_material_persisted':False,'features':feature_reports,'affine':decoded,
        'expected_logit':expected,'expected_probability':expected_probability,'decoded_probability':probability,
        'probability_error_bound':bound/4+NUMERIC_GUARD,'classification':decision,
        'affine_no_wrap_coefficient_upper_bound':str(nowrap),'modulus_product':str(math.prod(Q)),
        'decrypted_affine_coefficients':recovered,
        'exact_PC_response_coefficients':2*3*N,'DMA_records':records,'stage_times_ns':phases,
        'total_validation_inclusive_ns':time.perf_counter_ns()-start,
        'request_sha256':request_sha,'response_sha256':hashlib.sha256(response).hexdigest(),
        'limits':['Functional V1 sparse-sampler profile; no claimed security level',
                  'Canonical adjacent-odd-root slots; no OpenFHE ciphertext/context interoperability',
                  'Analytic rounding/noise bounds plus empirical 1e-8 floating arithmetic allowance',
                  'Validation-inclusive diagnostic latency; not an optimized deployment benchmark',
                  'Server evaluates affine only; client computes sigmoid after decryption']}
