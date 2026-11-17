"""Rewrite SHA256SUMS after editing files of this subproject: the same file set verify_repo.py checks
(every file except SHA256SUMS itself; remove build output such as __pycache__/ first).
Run tools/verify_repo.py afterwards. usage: python -B tools/update_sums.py"""
import hashlib
from pathlib import Path

root = Path(__file__).resolve().parent.parent
files = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.relative_to(root).parts[0] != '.git')
files = [f for f in files if f != 'SHA256SUMS']
(root / 'SHA256SUMS').write_text(''.join('%s  %s\n' % (hashlib.sha256((root / f).read_bytes()).hexdigest(), f) for f in files),
                                 encoding='utf-8', newline='\n')
print('SHA256SUMS_UPDATED files=%d' % len(files))
