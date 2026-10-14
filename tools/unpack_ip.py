"""Unpack the frozen IP into a NEW local directory with path/CRC checks."""
import argparse
from pathlib import Path
import zipfile
from verify_repository import main as verify
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args()
    verify()
    out=args.output.resolve()
    assert not out.exists(), 'Output exists; choose a new directory'
    archive=ROOT/'hardware/ip/edgefhe_local_hls_rns3_ntt_parallel_accel_n4096_1_1.zip'
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None, 'IP ZIP CRC error'
        for item in z.infolist():
            assert (out/item.filename).resolve().is_relative_to(out), 'Unsafe ZIP entry'
            assert '\\' not in item.filename and not item.filename.startswith('/'), 'Unsafe ZIP path'
        out.mkdir(parents=True)
        z.extractall(out)
    assert (out/'component.xml').is_file(), 'Missing IP component.xml'
    print('FROZEN_IP_EXTRACT_PASS '+str(out))

if __name__=='__main__':
    main()
