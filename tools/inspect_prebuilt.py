"""Offline ELF/modinfo identity; optional read-only target match. Never insmod/modprobe."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import struct
ROOT=Path(__file__).resolve().parents[1]
DIR=ROOT/'firmware/prebuilt/ubuntu-22.04-5.15.0-1027-xilinx-zynqmp'
def identity():
    contract=json.loads((DIR/'identity.json').read_text());p=DIR/contract['module_path'];b=p.read_bytes()
    if hashlib.sha256(b).hexdigest()!=contract['module_sha256']:raise ValueError('Prebuilt module SHA mismatch')
    if b[:6]!=b'\x7fELF\x02\x01' or struct.unpack_from('<HH',b,16)!=(1,183):raise ValueError('Wrong module ELF architecture/type')
    offset=struct.unpack_from('<Q',b,40)[0];size,count,names=struct.unpack_from('<HHH',b,58)
    if size!=64 or not 0<=names<count or offset+size*count>len(b):raise ValueError('Invalid ELF section table')
    sections=[struct.unpack_from('<IIQQQQIIQQ',b,offset+i*size) for i in range(count)]
    table=sections[names];text=b[table[4]:table[4]+table[5]];modinfo=None
    for s in sections:
        if s[0]>=len(text) or s[4]+s[5]>len(b) and s[1]!=8:raise ValueError('Invalid ELF section span')
        name=text[s[0]:].split(b'\0',1)[0]
        if name==b'.modinfo':modinfo=b[s[4]:s[4]+s[5]]
    if modinfo is None:raise ValueError('Missing module metadata')
    fields={}
    for item in modinfo.split(b'\0'):
        if b'=' in item:
            key,value=item.split(b'=',1);fields.setdefault(key.decode('ascii'),[]).append(value.decode('utf-8'))
    for field,key in [('version','module_version'),('vermagic','vermagic'),('srcversion','srcversion')]:
        if fields.get(field)!=[contract[key]]:raise ValueError('Wrong prebuilt '+field)
    source=(DIR/contract['source_path']).resolve()
    if not source.is_relative_to(ROOT) or hashlib.sha256(source.read_bytes()).hexdigest()!=contract['source_sha256']:
        raise ValueError('Corresponding V3 source mismatch')
    return contract,fields
def main():
    p=argparse.ArgumentParser();p.add_argument('--target',action='store_true');a=p.parse_args()
    contract,fields=identity()
    if a.target and (platform.system()!='Linux' or platform.machine()!='aarch64' or platform.release()!=contract['kernel']):
        raise ValueError('This optional prebuilt module does not match the current target kernel/architecture')
    print('PREBUILT_V3_EXACT_ELF_METADATA_PASS target_match='+str(a.target)+' no_load no_DMA')
    print(json.dumps({'version':fields['version'][0],'kernel':contract['kernel'],'vermagic':fields['vermagic'][0],
                      'module_sha256':contract['module_sha256'],'live_kernel_ABI_acceptance':False},indent=2))
if __name__=='__main__':main()
