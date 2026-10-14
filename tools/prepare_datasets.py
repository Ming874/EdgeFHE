"""Download the CDC Diabetes Health Indicators data and write a reproducible split.

Usage (run from C:/GitHub/Grad, Python 3.10+, standard library only):
    python -I tools/prepare_datasets.py --out data/cdc_diabetes

Writes <out>/raw/data.csv (the untouched download), <out>/splits/{train,validation,test}.csv
and <out>/manifest.json with the source URL, seed, row counts, class balance and SHA-256 of
every file, so anyone can regenerate the same split and check it byte for byte.

Source: UCI dataset 891 (CDC BRFSS), 253,680 rows. Split stratified by the label:
70% train, 15% validation, 15% test.
"""
import argparse
import csv
import hashlib
import http.client
import json
from pathlib import Path
import random
import sys
import time
import urllib.request

SEED = 20261013
CHUNK = 1 << 20
SPEC = {
    'name': 'CDC Diabetes Health Indicators',
    'url': 'https://archive.ics.uci.edu/static/public/891/data.csv',
    'page': 'https://archive.ics.uci.edu/dataset/891/cdc+diabetes+health+indicators',
    'raw': 'data.csv',
    'rows': 253680,
    'label': 'Diabetes_binary',
    'license': 'UCI: "See linked dataset for licensing information" (Kaggle alexteboul/'
               'diabetes-health-indicators-dataset); underlying survey is CDC BRFSS. Verify before publishing.',
    'citation': 'CDC Diabetes Health Indicators [Dataset]. UCI Machine Learning Repository. '
                'https://doi.org/10.24432/C53919',
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def download(url, target, attempts=5):
    """Stream to disk, retrying dropped connections; reuse an existing complete download."""
    if target.exists():
        print(f'reusing {target} ({target.stat().st_size / 1e6:.1f} MB)', flush=True)
        return
    partial = target.with_suffix(target.suffix + '.part')
    for attempt in range(1, attempts + 1):
        done = 0
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'EdgeFHE-dataset-prep/1.0'})
            with urllib.request.urlopen(request, timeout=60) as response, open(partial, 'wb') as out:
                for block in iter(lambda: response.read(CHUNK), b''):
                    out.write(block)
                    done += len(block)
            partial.rename(target)
            print(f'downloaded {target} ({done / 1e6:.1f} MB)', flush=True)
            return
        except (OSError, http.client.HTTPException) as error:
            print(f'  attempt {attempt}/{attempts} failed at {done / 1e6:.1f} MB: {error!r}', flush=True)
            if attempt == attempts:
                raise
            time.sleep(min(60, 15 * attempt))


def split(raw, splits):
    with open(raw, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    if SPEC['label'] not in header:
        raise SystemExit(f'Label column {SPEC["label"]} missing; header is {header}')
    if len(rows) != SPEC['rows']:
        raise SystemExit(f'Expected {SPEC["rows"]} rows, found {len(rows)}')
    label = header.index(SPEC['label'])
    by_class = {}
    for row in rows:
        if len(row) != len(header) or row[label] not in ('0', '1'):
            raise SystemExit(f'Malformed row: {row[:4]}')
        by_class.setdefault(row[label], []).append(row)
    rng = random.Random(SEED)
    parts = {'train': [], 'validation': [], 'test': []}
    for value in sorted(by_class):  # stratified: each class split 70/15/15 on its own
        group = by_class[value]
        rng.shuffle(group)
        n_val = round(len(group) * 0.15)
        parts['validation'] += group[:n_val]
        parts['test'] += group[n_val:2 * n_val]
        parts['train'] += group[2 * n_val:]
    report = {}
    for name, part in parts.items():
        rng.shuffle(part)  # mix the classes back together
        path = splits / f'{name}.csv'
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, lineterminator='\n')
            writer.writerow(header)
            writer.writerows(part)
        positives = sum(row[label] == '1' for row in part)
        report[name] = {'file': path.name, 'rows': len(part), 'positives': positives,
                        'positive_rate': round(positives / len(part), 6), 'full_2048_batches': len(part) // 2048,
                        'sha256': sha256(path)}
    return header, report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--force', action='store_true', help='replace existing splits')
    args = parser.parse_args()
    raw_dir, splits = args.out / 'raw', args.out / 'splits'
    if splits.exists() and any(splits.iterdir()) and not args.force:
        raise SystemExit(f'{splits} already has files; use --force to regenerate')
    raw_dir.mkdir(parents=True, exist_ok=True)
    splits.mkdir(parents=True, exist_ok=True)
    raw = raw_dir / SPEC['raw']
    download(SPEC['url'], raw)
    raw_hash = sha256(raw)
    print(f'raw sha256 {raw_hash}', flush=True)
    columns, report = split(raw, splits)
    manifest = {
        'dataset': SPEC['name'], 'source_url': SPEC['url'], 'page': SPEC['page'], 'license': SPEC['license'],
        'citation': SPEC['citation'], 'raw_file': f'raw/{raw.name}', 'raw_sha256': raw_hash,
        'raw_bytes': raw.stat().st_size, 'seed': SEED, 'label': SPEC['label'], 'columns': columns,
        'split_rule': 'stratified 70/15/15 by label', 'splits': report,
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'python': sys.version.split()[0],
    }
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    for name, item in report.items():
        print(f'{name:10s} rows={item["rows"]:>8,} positive_rate={item["positive_rate"]:.4f} '
              f'batches_of_2048={item["full_2048_batches"]}', flush=True)
    print(f'DATASET_READY -> {args.out}', flush=True)


if __name__ == '__main__':
    main()
