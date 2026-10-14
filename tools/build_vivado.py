"""Local Vivado launcher; isolate generated logs and require explicit completion."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['bd','implement'])
    p.add_argument('output',type=Path);p.add_argument('ip_repository',type=Path)
    p.add_argument('--tool',default='vivado');a=p.parse_args()
    output=a.output.resolve();ip=a.ip_repository.resolve()
    assert not output.exists(), 'Output exists; choose a new directory'
    assert (ip/'component.xml').is_file(), 'Extract accepted IP first, or provide new HLS impl/ip directory'
    tool=shutil.which(a.tool) or (str(Path(a.tool).resolve()) if Path(a.tool).is_file() else None)
    assert tool, 'Load Vivado 2022.1 environment or specify --tool'
    build=ROOT/'build';build.mkdir(exist_ok=True)
    launcher=Path(tempfile.mkdtemp(prefix='vivado-launch-',dir=build))
    marker=('PORTABLE_BD_STRUCTURE_PASS_NOT_ROUTED_OR_BOARD_ACCEPTANCE' if a.mode=='bd'
            else 'PORTABLE_IMPLEMENTATION_EXPORTED_COMPLETE_REPORT_AND_BOARD_REVIEW_PENDING')
    seen=False
    with (launcher/'console.log').open('w',encoding='utf-8',newline='\n') as log:
        cmd=[tool,'-mode','batch','-notrace','-source',str(ROOT/'hardware/vivado/build.tcl'),
             '-tclargs',str(output),str(ip),a.mode]
        child=subprocess.Popen(cmd,cwd=launcher,env=os.environ.copy(),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                               text=True,encoding='utf-8',errors='replace')
        for line in child.stdout:
            log.write(line);log.flush();print(line,end='',flush=True)
            seen=seen or line.strip()==marker
        code=child.wait()
    print('VIVADO_LAUNCH_LOG '+str(launcher/'console.log'),flush=True)
    assert code==0 and seen, 'Vivado script did not complete; inspect retained log'
    if a.mode=='bd':
        assert (output/'edgefhe_ii1.xpr').is_file()

if __name__=='__main__':
    main()
