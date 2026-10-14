"""Small strict FDT reader for semantic overlay validation; no device access."""
import argparse, json, struct
from pathlib import Path

def parse_fdt(data):
    if len(data) < 40:
        raise ValueError('Truncated FDT')
    magic, total, off_struct, off_strings, off_rsv, version, last, cpu, size_strings, size_struct = struct.unpack_from('>10I', data)
    if magic != 0xd00dfeed or total != len(data) or version != 17 or last > 17:
        raise ValueError('Unsupported FDT header')
    if off_struct + size_struct > total or off_strings + size_strings > total:
        raise ValueError('FDT sections outside file')
    strings = data[off_strings:off_strings + size_strings]
    pos, end, stack, nodes = off_struct, off_struct + size_struct, [], {}
    def cstring(blob, start):
        stop = blob.find(b'\0', start)
        if stop < 0:
            raise ValueError('Unterminated FDT string')
        return blob[start:stop].decode('ascii'), stop + 1
    while pos < end:
        token = struct.unpack_from('>I', data, pos)[0]; pos += 4
        if token == 1:
            name, pos = cstring(data, pos); pos = (pos + 3) & ~3
            stack.append(name)
            path = '/' + '/'.join(stack[1:])
            if path in nodes:
                raise ValueError('Duplicate FDT node')
            nodes[path] = {}
        elif token == 2:
            if not stack: raise ValueError('Unbalanced FDT node')
            stack.pop()
        elif token == 3:
            length, nameoff = struct.unpack_from('>II', data, pos); pos += 8
            name, _ = cstring(strings, nameoff)
            path = '/' + '/'.join(stack[1:])
            if not stack or pos + length > end or name in nodes[path]:
                raise ValueError('Invalid/duplicate FDT property')
            nodes[path][name] = data[pos:pos+length]; pos = (pos + length + 3) & ~3
        elif token == 4:
            pass
        elif token == 9:
            if stack: raise ValueError('Unclosed FDT node')
            return nodes
        else:
            raise ValueError('Unknown FDT token')
    raise ValueError('Missing FDT end')

def cells(value):
    if len(value) % 4: raise ValueError('FDT cells alignment')
    return list(struct.unpack('>' + 'I' * (len(value)//4), value))

def string(value):
    if not value.endswith(b'\0'): raise ValueError('FDT string termination')
    return value[:-1].decode('ascii')

def require(ok, message):
    if not ok: raise ValueError(message)

def validate_overlay(data):
    nodes = parse_fdt(data)
    bus = '/fragment@1/__overlay__'
    dma = nodes[bus + '/dma@a0000000']
    proxy = nodes[bus + '/edgefhe-ii1-proxy@a0010000']
    region = nodes['/fragment@0/__overlay__']
    require(string(nodes['/fragment@0']['target-path']) == '/fpga-full', 'FPGA region mismatch')
    require(string(region['firmware-name']) == 'edgefhe-ii1-n4096-kv260.bit.bin', 'Firmware mismatch')
    require(cells(region['resets']) == [0xffffffff,116], 'Reset mismatch')
    require(cells(nodes[bus+'/edgefhe-ii1-afi']['config-afi']) == [4,0,5,0,14,512], 'AFI width mismatch')
    require(string(dma['compatible']) == 'xlnx,axi-dma-1.00.a', 'DMA binding mismatch')
    require(cells(dma['reg']) == [0,0xa0000000,0,0x10000], 'DMA address mismatch')
    require(cells(dma['interrupts']) == [0,89,4,0,104,4], 'DMA IRQ mismatch')
    require(cells(dma['xlnx,addrwidth']) == [32] and cells(dma['xlnx,sg-length-width']) == [26], 'DMA width/length mismatch')
    require(cells(dma['clocks']) == [0xffffffff,71]*3, 'DMA real clock provider mismatch')
    require(cells(dma['assigned-clocks']) == [0xffffffff,71] and cells(dma['assigned-clock-rates']) == [100000000], 'DMA assigned clock mismatch')
    require(dma['clock-names'] == b's_axi_lite_aclk\0m_axi_mm2s_aclk\0m_axi_s2mm_aclk\0', 'Clock names mismatch')
    for name, irq in [('mm2s',89),('s2mm',104)]:
        channel = nodes[bus+'/dma@a0000000/dma-channel-'+name]
        require(cells(channel['interrupts']) == [0,irq,4] and cells(channel['xlnx,datawidth']) == [128], 'Channel mismatch')
        require('xlnx,include-dre' not in channel, 'Unexpected DRE')
    require(cells(proxy['reg']) == cells(proxy['ntt-control']) == [0,0xa0010000,0,0x10000], 'NTT resource mismatch')
    require(string(proxy['compatible']) == 'ncue,edgefhe-rns3-n4096-ii1-dma-proxy', 'Proxy binding mismatch')
    require(cells(proxy['clocks']) == [0xffffffff,71], 'Proxy clock mismatch')
    require(cells(proxy['edgefhe,dma-aperture-end']) == [0,0x80000000], 'Aperture mismatch')
    handle = cells(dma['phandle'])[0]
    require(cells(proxy['dmas']) == [handle,0,handle,1] and proxy['dma-names'] == b'tx\0rx\0', 'DMA ownership mismatch')
    require(set(nodes['/__fixups__']) == {'amba','gic','zynqmp_clk','zynqmp_reset'}, 'External DT symbols mismatch')
    for properties in nodes.values():
        require('dma-coherent' not in properties and 'xlnx,include-sg' not in properties, 'Unsupported coherent/SG declaration')
        require(properties.get('compatible') not in (b'fixed-clock\0',b'generic-uio\0'), 'Unowned clock/control alias')
    return {'status':'PASS_OVERLAY_BINARY_SEMANTICS_ONLY', 'external_symbols':sorted(nodes['/__fixups__']),
            'afi_pairs':[[4,0],[5,0],[14,512]], 'clock_id':71, 'reset_id':116,
            'limits':['Not applied to live tree', 'No board load/DMA acceptance', 'Physical reset warning disposition pending']}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('overlay'); parser.add_argument('--output')
    args=parser.parse_args(); result=validate_overlay(Path(args.overlay).read_bytes())
    print(result['status'])
    if args.output:
        with Path(args.output).open('x',encoding='utf-8',newline='\n') as f:
            json.dump(result,f,indent=2); f.write('\n')
if __name__=='__main__':main()
