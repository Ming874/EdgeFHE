"""Offline CLI/slot acceptance. A CLI exit code and FPGA operating alone are insufficient."""
import argparse,json,re
from pathlib import Path

APP='edgefhe-ii1-n4096-kv260'
def require(ok,message):
    if not ok:raise ValueError(message)

def parse_inventory(text):
    rows={}
    for line in text.splitlines():
        fields=line.split()
        if len(fields)<6 or re.fullmatch(r'[A-Z][A-Z0-9_]+',fields[1]) is None:continue
        require(re.fullmatch(r'[A-Za-z0-9_.-]+',fields[0]) is not None,'Unrecognized app name')
        require(fields[0] not in rows,'Duplicate app row')
        slot=fields[-1]
        if slot=='-1':slots=[]
        else:
            require(re.fullmatch(r'\d+(?:,\d+)*,?',slot) is not None,'Unrecognized active slot format')
            slots=[int(value) for value in slot.rstrip(',').split(',')]
        rows[fields[0]]={'type':fields[1],'active_slots':slots}
    require(rows and APP in rows,'Candidate missing from recognized inventory')
    return rows

def require_empty_slot(text):
    rows=parse_inventory(text)
    active={name:row['active_slots'] for name,row in rows.items() if row['active_slots']}
    require(not active,'Slot occupied before load: '+repr(active))
    return {'status':'PASS_SLOT_EMPTY_BEFORE_LOAD','rows':rows}

def require_target_loaded(exit_code,console,inventory,fpga_state):
    require(exit_code==0,'xmutil returned nonzero '+str(exit_code))
    require(re.search(r'\b(?:error|failed|failure)\b',console,re.I) is None,'xmutil printed a load failure despite exit code')
    require(re.search(r'^\s*'+re.escape(APP)+r':\s*loaded to slot 0\s*$',console,re.M) is not None,'Explicit target loaded-to-slot-0 marker missing')
    rows=parse_inventory(inventory)
    active={name:row['active_slots'] for name,row in rows.items() if row['active_slots']}
    require(active=={APP:[0]} and rows[APP]['type']=='XRT_FLAT','Requested target is not the sole active slot0 app: '+repr(active))
    require(fpga_state.strip()=='operating','FPGA manager not operating')
    return {'status':'PASS_EXPLICIT_LOAD_MARKER_TARGET_SLOT_AND_FPGA_STATE','active_app':APP,'active_slot':0}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['empty','loaded']);parser.add_argument('--apps',required=True,type=Path)
    parser.add_argument('--console',type=Path);parser.add_argument('--fpga-state',type=Path);parser.add_argument('--exit-code',type=int)
    args=parser.parse_args()
    if args.mode=='empty':result=require_empty_slot(args.apps.read_text())
    else:result=require_target_loaded(args.exit_code,args.console.read_text(),args.apps.read_text(),args.fpga_state.read_text())
    print(result['status'])
if __name__=='__main__':main()
