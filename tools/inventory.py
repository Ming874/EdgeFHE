"""Which files form the repository inventory (release/SHA256SUMS): every file git would keep.

Mirrors .gitignore for what can exist in a working copy: build output, caches, Python virtual
environments, dashboard dependencies, the downloaded CDC data (tools/prepare_datasets.py rebuilds
it from data/*/manifest.json) and logs that are not evidence. Used by update_manifest.py and
verify_repository.py, so both agree on the same file set. Standard library only.
"""
import os
from pathlib import Path, PurePosixPath

SKIP_DIRS = {'.git', 'build', 'local', '__pycache__', 'node_modules', '.next', '.Xil', '.vscode'}
SKIP_SUFFIXES = {'.pyc', '.tsbuildinfo', '.jou', '.str'}
SKIP_FILES = {'BUNDLE_MANIFEST.json'}  # left at the root when a board bundle is extracted
EVIDENCE_LOGS = ('release/native_v4/', 'results/')  # receipts and board logs are kept


def skipped_dir(parts):
    name = parts[-1]
    if name in SKIP_DIRS or name.startswith('.venv'):
        return True
    return len(parts) == 3 and parts[0] == 'data' and name in ('raw', 'splits')


def tracked(relative):
    """True if a file at this repository-relative POSIX path belongs to the inventory."""
    path = PurePosixPath(relative)
    parts = path.parts
    if any(skipped_dir(parts[:i]) for i in range(1, len(parts))):
        return False
    if path.as_posix() in SKIP_FILES or path.suffix in SKIP_SUFFIXES:
        return False
    return path.suffix != '.log' or path.as_posix().startswith(EVIDENCE_LOGS)


def files(root, exclude=()):
    """Inventory files under root (sorted by POSIX path), skipping the paths in exclude."""
    root = Path(root)
    found = []
    for folder, dirs, names in os.walk(root):
        base = Path(folder).relative_to(root).parts
        dirs[:] = [d for d in dirs if not skipped_dir(base + (d,))]
        for name in names:
            path = Path(folder) / name
            relative = PurePosixPath(*base, name).as_posix()
            if path not in exclude and path.is_file() and tracked(relative):
                found.append((relative, path))
    return [path for _, path in sorted(found)]
