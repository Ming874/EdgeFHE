"""LOCAL ONLY: frontend model cards for the zoo (same schema as web/src/model-cards.json) + reachability check.
A field's legal values must lie inside the |z|<=8 bound enforced by normalize.cpp; a binary/integer value that is
unreachable (e.g. a rare category whose z exceeds 8) is reported, never silently clipped into a different meaning."""
import json, math
from pathlib import Path

HERE = Path(__file__).resolve().parent; REC = HERE.parent
FRONT = REC / 'frontend_showcase_20261024/web/src/model-cards.json'
META = {'finance-credit-default-logreg-v1': ('金融', '信用風險', '信用卡違約風險', 'credit-default'),
        'finance-banknote-auth-logreg-v2': ('金融', '鈔票鑑定', '鈔票真偽（影像小波特徵）', 'banknote'),
        'energy-heating-load-ridge-v1': ('能源', '建築節能', '建築暖氣負載', 'energy-efficiency'),
        'energy-power-plant-output-ridge-v1': ('能源', '發電預測', '複循環電廠滿載出力', 'power-plant'),
        'energy-grid-instability-logreg-v1': ('能源', '電網穩定', '分散式電網不穩定風險', 'grid-stability'),
        'medical-heart-failure-logreg-v1': ('醫療', '心臟衰竭', '心衰竭追蹤期死亡風險', 'heart-failure'),
        'medical-maternal-high-risk-logreg-v1': ('醫療', '孕產婦照護', '孕產婦高風險', 'maternal-health'),
        'medical-breast-cancer-malignant-logreg-v1': ('醫療', '乳癌篩檢', '乳房腫塊惡性（細胞核特徵）', None),
        'construction-concrete-strength-ridge-v1': ('營建', '材料強度', '混凝土抗壓強度', 'concrete'),
        'realestate-unit-price-ridge-v1': ('不動產', '房價估算', '新店區單位房價', 'real-estate'),
        'food-wine-good-quality-logreg-v1': ('食品', '品質分級', '葡萄酒高品質（≥7）', 'wine-quality'),
        'agriculture-rice-cammeo-logreg-v1': ('農業', '品種辨識', '稻米 Cammeo 品種', 'rice'),
        'fishery-abalone-rings-ridge-v1': ('漁業', '年齡估算', '鮑魚年輪數', 'abalone'),
        'transport-auto-mpg-ridge-v1': ('交通', '車輛油耗', '汽車油耗（mpg）', 'auto-mpg'),
        'security-phishing-site-logreg-v1': ('資訊安全', '釣魚網站', '釣魚網站偵測', 'phishing'),
        'building-room-occupancy-logreg-v1': ('智慧建築', '人員偵測', '室內有人偵測', 'occupancy')}


def main():
    training = {m['model_id']: m for m in json.loads((HERE / 'models_v1/ZOO_TRAINING.json').read_text(encoding='utf-8'))['models']}
    manifest = {r['slug']: r for r in json.loads((HERE / 'sources/SOURCE_MANIFEST.json').read_text(encoding='utf-8'))['sources']}
    cards = json.loads(FRONT.read_text(encoding='utf-8'))['models']; report = {}
    for mid, (domain, category, title, slug) in META.items():
        t = training[mid]; assert t['status'] == 'TRAINED_ADMISSIBLE', mid
        m = json.loads((HERE / 'models_v1' / (mid + '.json')).read_text(encoding='utf-8')); fields = []; unreachable = []
        for i, name in enumerate(m['features']):
            mu, sd = m['mean'][i], m['std'][i]; lo8, hi8 = mu - 8 * sd, mu + 8 * sd
            tmin, tmax = t['train_ranges']['min'][i], t['train_ranges']['max'][i]
            values_int = float(tmin).is_integer() and float(tmax).is_integer()
            kind = 'binary' if (tmin, tmax) == (0.0, 1.0) and values_int and name not in ('age',) else ('integer' if values_int and tmax - tmin <= 50 else 'number')
            if kind == 'binary':
                lo, hi = 0, 1
                if lo < lo8 or hi > hi8: unreachable.append(name)
            elif kind == 'integer':
                lo, hi = math.ceil(max(lo8, tmin if tmin >= 0 else lo8)), math.floor(hi8)
                if tmin < lo or tmax > hi: unreachable.append(name)
            else:
                lo, hi = (max(lo8, 0.0) if tmin >= 0 else lo8), hi8
            fields.append(dict(name=name, kind=kind, unit='', mean=mu, std=sd, minimum=lo, maximum=hi,
                               exampleRange=[t['train_ranges']['p25'][i], t['train_ranges']['p75'][i]], note='依資料集原始欄位定義輸入；研究用途。'))
        src = manifest.get(slug, {}) if slug else {}
        cards.append(dict(id=mid, sourceSha256=__import__('hashlib').sha256((HERE / 'models_v1' / (mid + '.json')).read_bytes()).hexdigest(),
                          domain=domain, category=category, title=title, operation='public-scalar-affine',
                          thresholdSpace='logit' if m['task'] == 'classification' else 'affine-output', decisionLogit=m['decision_logit'],
                          normalizedAbsMax=8, fields=fields, dataset=(src.get('name') or m['training_source']['name']),
                          sourceUrl=src.get('data_url', 'https://archive.ics.uci.edu/dataset/17'), status='LOCAL_CPP_CKKS_VERIFIED_NOT_ON_BOARD'))
        report[mid] = dict(features=len(fields), unreachable_values=unreachable)
    out = dict(schema='edgefhe.demo.model-cards.v1', zoo='2026-10-28', models=cards)
    (HERE / 'model-cards.zoo.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    bad = {k: v for k, v in report.items() if v['unreachable_values']}
    print('CARDS', len(cards), 'UNREACHABLE', json.dumps(bad, ensure_ascii=False))


if __name__ == '__main__':
    main()
