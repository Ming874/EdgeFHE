"""Refresh inventory after documentation/tool edits; refuse changed frozen sources."""
import hashlib
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True  # before the local import, so the tree stays clean
from inventory import files as inventory_files  # noqa: E402
ROOT=Path(__file__).resolve().parents[1]

def main():
    origins=json.loads((ROOT/'release/PROVENANCE.json').read_text(encoding='utf-8'))
    for item in origins['copies']:
        p=ROOT/item['package_path']
        assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==item['sha256'], 'Frozen source changed: '+item['package_path']
    manifest=ROOT/'release/SHA256SUMS'
    files=inventory_files(ROOT, exclude={manifest})
    manifest.write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(ROOT).as_posix()+'\n' for p in files),encoding='utf-8',newline='\n')
    print(f'INVENTORY_REFRESHED_SOURCE_IDENTITIES_UNCHANGED files={len(files)}; run verify_repository.py next')

if __name__=='__main__':
    main()
