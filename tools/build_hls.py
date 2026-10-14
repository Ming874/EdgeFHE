"""Vitis HLS 2022.1 launcher: env arguments, isolated logs and explicit completion."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser()
    p.add_argument('mode',choices=['public','fixed']);p.add_argument('output',type=Path)
    p.add_argument('--cosim',action='store_true');p.add_argument('--tool',default='vitis_hls')
    a=p.parse_args();output=a.output.resolve()
    assert not output.exists(), 'Output exists; choose a new directory'
    assert not any(c.isspace() for c in str(ROOT)+str(output)), 'HLS 2022.1 include-path adapter requires checkout/output paths without whitespace'
    tool=shutil.which(a.tool) or (str(Path(a.tool).resolve()) if Path(a.tool).is_file() else None)
    assert tool, 'Load Vitis HLS 2022.1 environment or pass --tool /path/to/vitis_hls'
    build=ROOT/'build';build.mkdir(exist_ok=True)
    launcher=Path(tempfile.mkdtemp(prefix='hls-launch-',dir=build))
    env=dict(os.environ, EDGEFHE_HLS_MODE=a.mode, EDGEFHE_HLS_OUTPUT=str(output), EDGEFHE_HLS_COSIM=str(int(a.cosim)))
    marker='LOCAL_HLS_EXPORT_COMPLETE_REPORT_REVIEW_REQUIRED';seen=False
    with (launcher/'console.log').open('w',encoding='utf-8',newline='\n') as log:
        child=subprocess.Popen([tool,'-f',str(ROOT/'hardware/hls/build.tcl')],cwd=launcher,env=env,
                               stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
        for line in child.stdout:
            log.write(line);log.flush();print(line,end='',flush=True)
            seen=seen or marker in line
        code=child.wait()
    print('HLS_LAUNCH_LOG '+str(launcher/'console.log'),flush=True)
    assert code==0 and seen, 'HLS script did not complete: inspect retained log (batch launcher exit0 alone is insufficient)'
    assert (output/'project/solution1/syn/report/rns3_ntt_parallel_accel_n4096_csynth.xml').is_file()

if __name__=='__main__':
    main()
