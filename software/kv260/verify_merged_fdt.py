"""Verify pure-file overlay merge against original boot FDT symbols."""
import argparse,json
from pathlib import Path
from fdt_contract import parse_fdt,cells,string,require
def verify(base_data,merged_data):
    base=parse_fdt(base_data);merged=parse_fdt(merged_data)
    symbols=base['/__symbols__']
    locations={name:string(symbols[name]) for name in ('amba','gic','zynqmp_clk','zynqmp_reset')}
    handles={name:cells(base[path]['phandle'])[0] for name,path in locations.items()}
    bus=locations['amba'];dma=merged[bus+'/dma@a0000000'];proxy=merged[bus+'/edgefhe-ii1-proxy@a0010000']
    require(cells(dma['clocks'])==[handles['zynqmp_clk'],71]*3,'Boot clock phandle mismatch')
    require(cells(dma['assigned-clocks'])==[handles['zynqmp_clk'],71],'Boot assigned clock mismatch')
    require(cells(dma['interrupt-parent'])==[handles['gic']],'Boot GIC mismatch')
    require(cells(merged['/fpga-full']['resets'])==[handles['zynqmp_reset'],116],'Boot reset mismatch')
    require(cells(proxy['dmas'])==[cells(dma['phandle'])[0],0,cells(dma['phandle'])[0],1],'Boot local DMA mismatch')
    require(cells(proxy['clocks'])==[handles['zynqmp_clk'],71],'Boot proxy clock mismatch')
    return {'status':'PASS_BOOT_FDT_FILE_MERGE_ONLY','base_symbols':locations,
            'limits':['fdtoverlay operates on saved files; no kernel overlay or FPGA programming',
                      'Not AFI/clock provider probe, reset or DMA acceptance']}
def main():
    p=argparse.ArgumentParser();p.add_argument('base');p.add_argument('merged');p.add_argument('output');a=p.parse_args()
    result=verify(Path(a.base).read_bytes(),Path(a.merged).read_bytes())
    with Path(a.output).open('x',encoding='utf-8',newline='\n') as f:json.dump(result,f,indent=2);f.write('\n')
    print(result['status'])
if __name__=='__main__':main()
