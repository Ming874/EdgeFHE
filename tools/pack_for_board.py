"""Pack what the KV260 needs into one archive (Windows or Linux, standard library only).

    python tools/pack_for_board.py               full bundle (~7 MB): copy with USB stick or scp
    python tools/pack_for_board.py --serial      stages 1-2 only (~2 MB): for the serial console
    python tools/pack_for_board.py --code-only   code update only (~200 KB): after editing code

Writes build/kv260_<mode>_<timestamp>.tar.gz with paths relative to the repository root (this
Grad folder), so it extracts into ~/Grad on the board, plus BUNDLE_MANIFEST.json (SHA-256 of
every file).
  full       the whole repository inventory (tools/inventory.py: the files listed in
             release/SHA256SUMS) plus the CDC test split, which git does not keep
  serial     what stages 1-2 use: software/, tools/, firmware/, the two board docs, the CDC
             test split and the model files (no evidence/, hardware/, validation/)
  code-only  software/ and tools/ only
On the board:
  mkdir -p ~/Grad && tar -xzf kv260_*.tar.gz -C ~/Grad && cd ~/Grad
  python3 -c "import json,hashlib,pathlib;m=json.load(open('BUNDLE_MANIFEST.json'))['files'];print('BAD',[n for n,h in m.items() if hashlib.sha256(pathlib.Path(n).read_bytes()).hexdigest()!=h])"
A full bundle also passes tools/verify_repository.py on the board when it is extracted into an
empty ~/Grad. Boards set up before 2026-10-14 have the old layout (code under
~/Grad/EdgeFHE-KV260-GitHub/): move that ~/Grad aside first (docs/BOARD_GUIDE.md).
TLS keys for the two-machine demo are never packed. Serial transfer: tools/kv260/README.md.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
sys.dont_write_bytecode = True  # before the local import, so the tree stays clean
from inventory import files as inventory_files  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ['data/cdc_diabetes/splits/test.csv', 'data/cdc_diabetes/manifest.json',
        'training/outputs/model.json', 'training/outputs/edge_preprocess.json']
MODES = {
    'full': {'parts': None, 'extra': DATA},  # None: the whole inventory
    'serial': {'parts': ['software', 'tools', 'firmware', 'docs/BOARD_GUIDE.md', 'docs/COMPARISON.md'], 'extra': DATA},
    'code-only': {'parts': ['software', 'tools'], 'extra': []},
}


def repo_files(parts):
    """Inventory files, all of them or only those under the given repository paths."""
    for path in inventory_files(ROOT):
        relative = path.relative_to(ROOT).as_posix()
        if parts is None or any(relative == part or relative.startswith(part + '/') for part in parts):
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--serial', action='store_true', help='stages 1-2 only, small enough for the serial console')
    mode.add_argument('--code-only', action='store_true', help='software/ and tools/ only')
    args = parser.parse_args()
    name = 'serial' if args.serial else 'code-only' if args.code_only else 'full'
    if name == 'full':
        # The board re-runs verify_repository.py on a full bundle, so refuse an inconsistent repository.
        check = subprocess.run([sys.executable, str(ROOT / 'tools/verify_repository.py')], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        if check.returncode != 0 or 'REPOSITORY_MANIFEST_AND_DOCUMENTS_PASS' not in check.stdout:
            raise SystemExit('verify_repository.py failed; run tools/update_manifest.py after edits:\n' + check.stdout[-2000:])
    files = list(repo_files(MODES[name]['parts']))
    for extra in MODES[name]['extra']:
        path = ROOT / extra
        if not path.is_file():
            raise SystemExit(f'Missing {extra}: run tools/prepare_datasets.py and training/train.py first')
        if path not in files:
            files.append(path)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    output = ROOT / 'build' / f'kv260_{name}_{stamp}.tar.gz'
    output.parent.mkdir(parents=True, exist_ok=True)
    # Older than any real date, so the board never warns about files from the future
    # even before its clock is set.
    mtime = 1_600_000_000
    manifest = {'mode': name, 'generated_local': stamp, 'files': {}}
    with tarfile.open(output, 'x:gz', compresslevel=9) as archive:
        for path in files:
            member = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            manifest['files'][member] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mtime = mtime
            info.mode = 0o755 if path.suffix in {'.sh', '.py'} else 0o644
            archive.addfile(info, io.BytesIO(data))
        body = (json.dumps(manifest, indent=1) + '\n').encode()
        info = tarfile.TarInfo('BUNDLE_MANIFEST.json')
        info.size = len(body)
        info.mtime = mtime
        archive.addfile(info, io.BytesIO(body))
    size = output.stat().st_size
    print(f'BUNDLE_WRITTEN {output} mode={name} files={len(files)} bytes={size}')
    if name == 'full':
        print('Copy it to the board (USB stick or scp), then: mkdir -p ~/Grad && tar -xzf <file> -C ~/Grad')
    else:
        print(f'Serial copy takes about {size * 4 / 3 / 11500:.0f} s: see tools/kv260/README.md')


if __name__ == '__main__':
    main()
