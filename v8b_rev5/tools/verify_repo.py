"""Check every file of this repository against SHA256SUMS (no missing, extra or changed file).
usage: python tools/verify_repo.py"""
import hashlib, sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
want = dict(l.split('  ', 1)[::-1] for l in (root / 'SHA256SUMS').read_text(encoding='utf-8').splitlines() if l)
have = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.relative_to(root).parts[0] != '.git'} - {'SHA256SUMS'}
bad = sorted(set(want) ^ have) + [f for f in sorted(set(want) & have) if hashlib.sha256((root / f).read_bytes()).hexdigest() != want[f]]
print('REPO_VERIFY_FAIL' if bad else 'REPO_VERIFY_PASS', len(have), bad[:20])
sys.exit(1 if bad else 0)
