"""USER-approved (2026-10-28) download of public UCI datasets for the EdgeFHE model zoo. LOCAL ONLY.
For each id: the official UCI API metadata (license, variables, data_url) and the data CSV are saved unchanged;
URL, bytes and SHA-256 go to SOURCE_MANIFEST.json. Existing files are never overwritten."""
import hashlib, json, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent; SRC = HERE / 'sources'
IDS = {350: 'credit-default', 267: 'banknote', 242: 'energy-efficiency', 294: 'power-plant', 471: 'grid-stability',
       519: 'heart-failure', 189: 'parkinsons-telemonitoring', 863: 'maternal-health', 165: 'concrete', 477: 'real-estate',
       186: 'wine-quality', 545: 'rice', 1: 'abalone', 9: 'auto-mpg', 327: 'phishing', 357: 'occupancy'}


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'edgefhe-research-fetch/1'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    SRC.mkdir(parents=True, exist_ok=True); rows = []
    for uid, slug in IDS.items():
        meta_url = 'https://archive.ics.uci.edu/api/dataset?id=%d' % uid
        try:
            meta_raw = get(meta_url); meta = json.loads(meta_raw)['data']
            data_url = meta['data_url']; data = get(data_url)
            assert data_url.startswith('https://archive.ics.uci.edu/'), 'UNEXPECTED_HOST'
            for name, raw in ((slug + '.meta.json', meta_raw), (slug + '.csv', data)):
                p = SRC / name
                with p.open('xb') as f: f.write(raw)
            rows.append(dict(id=uid, slug=slug, name=meta['name'], license=meta.get('license'), doi=meta.get('doi'),
                             metadata_url=meta_url, data_url=data_url, csv_bytes=len(data), csv_sha256=hashlib.sha256(data).hexdigest(),
                             meta_sha256=hashlib.sha256(meta_raw).hexdigest(), fetched_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
            print('OK', uid, slug, len(data), meta.get('license'))
        except Exception as e:
            rows.append(dict(id=uid, slug=slug, error=repr(e))); print('FAIL', uid, slug, repr(e))
    (SRC / 'SOURCE_MANIFEST.json').write_text(json.dumps(dict(approved_by_user='2026-10-28 chat', sources=rows), ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print('TOTAL_BYTES', sum(r.get('csv_bytes', 0) for r in rows), 'FAILED', [r['slug'] for r in rows if 'error' in r])


if __name__ == '__main__':
    main()
