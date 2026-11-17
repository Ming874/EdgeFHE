"""EdgeFHE application model zoo (LOCAL ONLY, public data, numpy). Same method as the accepted train_models.py:
train-only fit, validation selects lambda (and the classification cut), held-out test used once. Added math gate:
a lambda is eligible only if the quantized model meets the CKKS admission budget of validate_model_cards.py
(|w|<=16, |b|<4, CRT headroom >= 10 bits at |z|<=8, precision gate >= 15 sigma). Leakage-prone columns are dropped
(row ids, timestamps, follow-up time, continuous twin targets); Parkinsons uses a subject-grouped split.
usage: python -X utf8 -B train_zoo.py"""
import csv, hashlib, json, math, platform
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent; SRC = HERE / 'sources'; OUT = HERE / 'models_v1'
SCALE = 1 << 30; N = 4096; Q = [998244353, 1004535809, 469762049]
FRESH_STD = math.sqrt(2 * N * (2 / 3) * 4.5 ** 2 + 4.5 ** 2); FRESH_WORST = 2 * N * 55 + 55
LAMBDAS = [.001, .01, .1, 1., 10., 30., 100., 300., 1000.]


def gate_for(model_id): return 5e-4 if 'ridge' in model_id else 2e-3          # frozen client.cpp:288 rule


def admissible(model_id, w, b):
    wi = [round(float(x) * SCALE) for x in w]; l1 = sum(abs(x) for x in wi); l2 = math.sqrt(sum(x * x for x in wi))
    if max(abs(x) for x in w) > 16 or abs(b) >= 4: return False, 'v1 bounded scalar profile'
    headroom = math.log2(math.prod(Q) / 2) - math.log2((sum(abs(x) for x in w) * 8 + abs(b)) * 2 ** 60 + l1 * FRESH_WORST)
    sigma = l2 * FRESH_STD * math.sqrt(N / 2) / 2 ** 60
    if headroom < 10: return False, 'CRT headroom %.1f' % headroom
    if gate_for(model_id) < 15 * sigma: return False, 'noise gate/sigma %.1f' % (gate_for(model_id) / sigma)
    if 6 * sigma > 5e-4: return False, 'native pipeline QA oracle 5e-4 < 6 sigma %.2e' % (6 * sigma)   # added after banknote-v1 failed native QA
    return True, 'ok gate/sigma %.1f headroom %.1f' % (gate_for(model_id) / sigma, headroom)


def split_ids(n, seed, groups=None):
    key = lambda i: hashlib.sha256(('%s/%s' % (seed, i)).encode()).digest()
    if groups is None:
        order = sorted(range(n), key=key); a, b = int(.65 * n), int(.8 * n)
        return dict(train=order[:a], validation=order[a:b], test=order[b:])
    g = sorted(set(groups), key=key); a, b = int(.65 * len(g)), int(.8 * len(g)); part = {}
    for k, gs in (('train', g[:a]), ('validation', g[a:b]), ('test', g[b:])):
        s = set(gs); part[k] = [i for i in range(n) if groups[i] in s]
    return part


def sigmoid(x): return np.exp(-np.logaddexp(0, -x))


def logistic(x, y, lam):
    a = np.c_[np.ones(len(x)), x]; w = np.zeros(a.shape[1]); n = len(y)
    bal = np.where(y == 1, n / (2 * np.sum(y)), n / (2 * np.sum(1 - y))); pen = np.r_[0., np.full(x.shape[1], lam)]
    loss = lambda v: np.mean(bal * (np.logaddexp(0, a @ v) - y * (a @ v))) + .5 * np.sum(pen * v * v)
    for _ in range(200):
        p = sigmoid(a @ w); g = a.T @ (bal * (p - y)) / n + pen * w
        if np.max(np.abs(g)) < 1e-9: break
        h = a.T @ ((bal * p * (1 - p))[:, None] * a) / n + np.diag(pen) + np.eye(len(w)) * 1e-10
        step = np.linalg.solve(h, g); t = 1.; old = loss(w)
        while t > 1e-9 and loss(w - t * step) > old: t *= .5
        w -= t * step
    if np.max(np.abs(g)) > 1e-5: raise ValueError('logistic optimizer did not converge')
    return w[1:], float(w[0])


def auc(y, s):
    order = np.argsort(s, kind='stable'); rank = np.empty(len(s)); i = 0
    while i < len(s):
        j = i + 1
        while j < len(s) and s[order[j]] == s[order[i]]: j += 1
        rank[order[i:j]] = (i + 1 + j) / 2; i = j
    pos = float(np.sum(y)); neg = len(y) - pos
    return float((np.sum(rank[y == 1]) - pos * (pos + 1) / 2) / (pos * neg))


def clf_metrics(y, s, cut):
    pred = s >= cut; tp = int(np.sum(pred & (y == 1))); fp = int(np.sum(pred & (y == 0))); tn = int(np.sum(~pred & (y == 0))); fn = int(np.sum(~pred & (y == 1)))
    rec = tp / max(1, tp + fn); prec = tp / max(1, tp + fp)
    return dict(AUROC=auc(y, s), accuracy=(tp + tn) / len(y), precision=prec, recall=rec, F1=2 * tp / max(1, 2 * tp + fp + fn),
                balanced_accuracy=(rec + tn / max(1, tn + fp)) / 2, confusion=dict(TP=tp, FP=fp, TN=tn, FN=fn))


def train(spec, x, y, groups=None):
    mid, task = spec['id'], spec['task']; ids = split_ids(len(x), spec['seed'], groups); ix = {k: np.array(v) for k, v in ids.items()}
    mean = x[ix['train']].mean(0); std = x[ix['train']].std(0)
    if not np.all(std > 0): raise ValueError('zero variance ' + mid)
    z = (x - mean) / std
    tm = float(y[ix['train']].mean()) if task == 'regression' else 0.; ts = float(y[ix['train']].std()) if task == 'regression' else 1.
    yn = (y - tm) / ts; cands = []
    for lam in LAMBDAS:
        if task == 'regression':
            a = z[ix['train']]; v = yn[ix['train']]; w = np.linalg.solve(a.T @ a + lam * len(a) * np.eye(x.shape[1]) / len(a), a.T @ v); b = float(np.mean(v - a @ w))
            score = -float(np.mean((z[ix['validation']] @ w + b - yn[ix['validation']]) ** 2))
        else:
            w, b = logistic(z[ix['train']], y[ix['train']], lam); score = auc(y[ix['validation']], z[ix['validation']] @ w + b)
        ok, why = admissible(mid, w, b); cands.append(dict(lam=lam, score=score, admissible=ok, reason=why, w=w, b=b))
    eligible = [c for c in cands if c['admissible']]
    if not eligible: return dict(model_id=mid, status='REJECTED_NO_ADMISSIBLE_LAMBDA', search=[{k: c[k] for k in ('lam', 'score', 'reason')} for c in cands])
    best = max(eligible, key=lambda c: (c['score'], c['lam'])); w, b, lam = best['w'], best['b'], best['lam']
    wi = [int(round(float(v) * SCALE)) for v in w]; bi = int(round(b * SCALE * SCALE))
    pred = z @ (np.array(wi, dtype=np.float64) / SCALE) + bi / (SCALE * SCALE); cut = 0.
    if task == 'classification':
        cut = float(max(np.linspace(-4, 4, 161), key=lambda c: (clf_metrics(y[ix['validation']], pred[ix['validation']], c)['F1'], -abs(c))))
    model = dict(schema='edgefhe.affine-research.v1', model_id=mid, operation='public-scalar-affine', max_depth=1, features=spec['features'],
                 mean=mean.tolist(), std=std.tolist(), weights=w.tolist(), bias=b, weights_int=wi, bias_int=bi, weight_scale=SCALE,
                 output_scale=SCALE * SCALE, input_abs_max=8, task=task, decision_logit=cut, target_mean=tm, target_std=ts,
                 training_source=spec['source'], split_seed=spec['seed'], split='subject-grouped' if groups is not None else 'row',
                 selected_lambda=lam, deployment_approved=False, output_policy='owner_only_no_cloud_feedback')
    raw = (json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    path = OUT / (mid + '.json')
    if path.exists() and path.read_bytes() != raw: raise ValueError('preserve existing artifact ' + mid)
    path.write_bytes(raw); t = ix['test']
    if task == 'regression':
        p = pred[t] * ts + tm; tr = y[t]
        metrics = dict(RMSE=float(np.sqrt(np.mean((p - tr) ** 2))), MAE=float(np.mean(np.abs(p - tr))), R2=float(1 - np.sum((p - tr) ** 2) / np.sum((tr - tr.mean()) ** 2)),
                       constant_train_mean_RMSE=float(np.sqrt(np.mean((tr - tm) ** 2))))
    else:
        metrics = clf_metrics(y[t], pred[t], cut); metrics['majority_class_accuracy'] = float(max(np.mean(y[t]), 1 - np.mean(y[t])))
    with (OUT / (mid + '.public_test.csv')).open('w', newline='', encoding='utf-8') as f:
        wr = csv.writer(f); wr.writerow(spec['features']); wr.writerows(x[t].tolist())
    useful = (metrics['R2'] >= 0.3) if task == 'regression' else (metrics['AUROC'] >= 0.7 and metrics['balanced_accuracy'] > 0.6)
    return dict(model_id=mid, status='TRAINED_ADMISSIBLE' if useful else 'ADMISSIBLE_BUT_NOT_USEFUL_EXCLUDED', useful=useful,
                usefulness_rule='regression test R2>=0.3; classification test AUROC>=0.7 and balanced accuracy>0.6', model_sha256=hashlib.sha256(raw).hexdigest(), rows=len(x), features=len(spec['features']),
                split_counts={k: len(v) for k, v in ids.items()}, selected_lambda=lam, admission=best['reason'], test_metrics=metrics,
                test_rows_outside_z8=int(np.sum(np.max(np.abs(z[t]), axis=1) > 8)), search=[{k: c[k] for k in ('lam', 'score', 'reason')} for c in cands],
                train_ranges=dict(min=x[ix['train']].min(0).tolist(), max=x[ix['train']].max(0).tolist(),
                                  p25=np.percentile(x[ix['train']], 25, axis=0).tolist(), p75=np.percentile(x[ix['train']], 75, axis=0).tolist()))


def rows(slug):
    with (SRC / (slug + '.csv')).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def num(rs, cols): return np.array([[float(r[c]) for c in cols] for r in rs])


def datasets(manifest):
    src = lambda slug, **k: dict(name=manifest[slug]['name'], uci_id=manifest[slug]['id'], data_url=manifest[slug]['data_url'],
                                 sha256=manifest[slug]['csv_sha256'], license='CC BY 4.0 (UCI repository policy; per-dataset page check recorded separately)', **k)
    out = []
    r = rows('credit-default'); cols = ['X%d' % i for i in range(1, 24)]
    out.append((dict(id='finance-credit-default-logreg-v1', task='classification', seed=20261101, features=['limit_bal', 'sex', 'education', 'marriage', 'age', 'pay_0', 'pay_2', 'pay_3', 'pay_4', 'pay_5', 'pay_6', 'bill_amt1', 'bill_amt2', 'bill_amt3', 'bill_amt4', 'bill_amt5', 'bill_amt6', 'pay_amt1', 'pay_amt2', 'pay_amt3', 'pay_amt4', 'pay_amt5', 'pay_amt6'],
                     source=src('credit-default', note='coded categorical columns used as published integers; research only')), num(r, cols), np.array([float(x['Y']) for x in r]), None))
    r = rows('banknote'); out.append((dict(id='finance-banknote-auth-logreg-v2', task='classification', seed=20261102, features=['variance', 'skewness', 'curtosis', 'entropy'], source=src('banknote')),
                                      num(r, ['variance', 'skewness', 'curtosis', 'entropy']), np.array([float(x['class']) for x in r]), None))
    r = rows('energy-efficiency'); out.append((dict(id='energy-heating-load-ridge-v1', task='regression', seed=20261103, features=['relative_compactness', 'surface_area', 'wall_area', 'roof_area', 'overall_height', 'orientation', 'glazing_area', 'glazing_area_distribution'], source=src('energy-efficiency', target='Y1 heating load')),
                                               num(r, ['X%d' % i for i in range(1, 9)]), np.array([float(x['Y1']) for x in r]), None))
    r = rows('power-plant'); out.append((dict(id='energy-power-plant-output-ridge-v1', task='regression', seed=20261104, features=['ambient_temperature_C', 'exhaust_vacuum_cmHg', 'ambient_pressure_mbar', 'relative_humidity_pct'], source=src('power-plant', target='PE net electrical output MW')),
                                         num(r, ['AT', 'V', 'AP', 'RH']), np.array([float(x['PE']) for x in r]), None))
    r = rows('grid-stability'); cols = ['tau1', 'tau2', 'tau3', 'tau4', 'p1', 'p2', 'p3', 'p4', 'g1', 'g2', 'g3', 'g4']
    out.append((dict(id='energy-grid-instability-logreg-v1', task='classification', seed=20261105, features=cols, source=src('grid-stability', target='stabf==unstable; continuous stab excluded (twin target)')),
                num(r, cols), np.array([1. if x['stabf'] == 'unstable' else 0. for x in r]), None))
    r = rows('heart-failure'); cols = ['age', 'anaemia', 'creatinine_phosphokinase', 'diabetes', 'ejection_fraction', 'high_blood_pressure', 'platelets', 'serum_creatinine', 'serum_sodium', 'sex', 'smoking']
    out.append((dict(id='medical-heart-failure-logreg-v1', task='classification', seed=20261106, features=cols, source=src('heart-failure', excluded='time (follow-up period, outcome leakage)')),
                num(r, cols), np.array([float(x['death_event']) for x in r]), None))
    r = rows('parkinsons-telemonitoring'); cols = ['age', 'sex', 'Jitter(%)', 'Jitter(Abs)', 'Jitter:RAP', 'Jitter:PPQ5', 'Jitter:DDP', 'Shimmer', 'Shimmer(dB)', 'Shimmer:APQ3', 'Shimmer:APQ5', 'Shimmer:APQ11', 'Shimmer:DDA', 'NHR', 'HNR', 'RPDE', 'DFA', 'PPE']
    out.append((dict(id='medical-parkinsons-updrs-ridge-v1', task='regression', seed=20261107, features=[c.replace('(%)', '_pct').replace('(Abs)', '_abs').replace('(dB)', '_dB').replace(':', '_') for c in cols],
                     source=src('parkinsons-telemonitoring', target='total_UPDRS', excluded='subject#, test_time, motor_UPDRS', split='subject-grouped')),
                num(r, cols), np.array([float(x['total_UPDRS']) for x in r]), [x['subject#'] for x in r]))
    r = rows('maternal-health'); cols = ['Age', 'SystolicBP', 'DiastolicBP', 'BS', 'BodyTemp', 'HeartRate']
    out.append((dict(id='medical-maternal-high-risk-logreg-v1', task='classification', seed=20261108, features=['age', 'systolic_bp', 'diastolic_bp', 'blood_sugar_mmolL', 'body_temp_F', 'heart_rate_bpm'], source=src('maternal-health', target='RiskLevel==high risk')),
                num(r, cols), np.array([1. if x['RiskLevel'] == 'high risk' else 0. for x in r]), None))
    from sklearn.datasets import load_breast_cancer
    bc = load_breast_cancer(); bpath = Path(__import__('sklearn').__file__).parent / 'datasets/data/breast_cancer.csv'
    out.append((dict(id='medical-breast-cancer-malignant-logreg-v1', task='classification', seed=20261109, features=[f.replace(' ', '_') for f in bc.feature_names],
                     source=dict(name='Breast Cancer Wisconsin (Diagnostic), scikit-learn %s bundled copy' % __import__('sklearn').__version__, uci_id=17,
                                 sha256=hashlib.sha256(bpath.read_bytes()).hexdigest(), license='CC BY 4.0 (UCI 17)', target='malignant')),
                bc.data, (bc.target == 0).astype(float), None))
    r = rows('concrete'); cols = ['Cement', 'Blast Furnace Slag', 'Fly Ash', 'Water', 'Superplasticizer', 'Coarse Aggregate', 'Fine Aggregate', 'Age']
    out.append((dict(id='construction-concrete-strength-ridge-v1', task='regression', seed=20261110, features=['cement_kgm3', 'slag_kgm3', 'fly_ash_kgm3', 'water_kgm3', 'superplasticizer_kgm3', 'coarse_aggregate_kgm3', 'fine_aggregate_kgm3', 'age_day'], source=src('concrete', target='compressive strength MPa')),
                num(r, cols), np.array([float(x['Concrete compressive strength']) for x in r]), None))
    r = rows('real-estate'); cols = ['X1 transaction date', 'X2 house age', 'X3 distance to the nearest MRT station', 'X4 number of convenience stores', 'X5 latitude', 'X6 longitude']
    out.append((dict(id='realestate-unit-price-ridge-v1', task='regression', seed=20261111, features=['transaction_date', 'house_age_year', 'mrt_distance_m', 'convenience_stores', 'latitude', 'longitude'], source=src('real-estate', target='price per ping (10k TWD)')),
                num(r, cols), np.array([float(x['Y house price of unit area']) for x in r]), None))
    r = rows('wine-quality'); cols = ['fixed_acidity', 'volatile_acidity', 'citric_acid', 'residual_sugar', 'chlorides', 'free_sulfur_dioxide', 'total_sulfur_dioxide', 'density', 'pH', 'sulphates', 'alcohol']
    x = np.c_[num(r, cols), np.array([1. if q['color'] == 'red' else 0. for q in r])]
    out.append((dict(id='food-wine-good-quality-logreg-v1', task='classification', seed=20261112, features=cols + ['is_red'], source=src('wine-quality', target='quality>=7')),
                x, np.array([1. if float(q['quality']) >= 7 else 0. for q in r]), None))
    r = rows('rice'); cols = ['Area', 'Perimeter', 'Major_Axis_Length', 'Minor_Axis_Length', 'Eccentricity', 'Convex_Area', 'Extent']
    out.append((dict(id='agriculture-rice-cammeo-logreg-v1', task='classification', seed=20261113, features=[c.lower() for c in cols], source=src('rice', target='Class==Cammeo')),
                num(r, cols), np.array([1. if x['Class'] == 'Cammeo' else 0. for x in r]), None))
    r = rows('abalone'); cols = ['Length', 'Diameter', 'Height', 'Whole_weight', 'Shucked_weight', 'Viscera_weight', 'Shell_weight']
    x = np.c_[np.array([[1. if q['Sex'] == 'M' else 0., 1. if q['Sex'] == 'F' else 0.] for q in r]), num(r, cols)]
    out.append((dict(id='fishery-abalone-rings-ridge-v1', task='regression', seed=20261114, features=['sex_male', 'sex_female'] + [c.lower() for c in cols], source=src('abalone', target='rings (age-1.5)')),
                x, np.array([float(q['Rings']) for q in r]), None))
    r = [q for q in rows('auto-mpg') if q['horsepower'] not in ('', '?', 'NaN')]; cols = ['cylinders', 'displacement', 'horsepower', 'weight', 'acceleration', 'model_year']
    x = np.c_[num(r, cols), np.array([[1. if q['origin'] == '2' else 0., 1. if q['origin'] == '3' else 0.] for q in r])]
    out.append((dict(id='transport-auto-mpg-ridge-v1', task='regression', seed=20261115, features=cols + ['origin_europe', 'origin_japan'], source=src('auto-mpg', target='mpg', excluded='car_name; rows with missing horsepower')),
                x, np.array([float(q['mpg']) for q in r]), None))
    r = rows('phishing'); cols = [c for c in r[0] if c != 'result']
    out.append((dict(id='security-phishing-site-logreg-v1', task='classification', seed=20261116, features=cols, source=src('phishing', target='result==-1 (phishing)')),
                num(r, cols), np.array([1. if q['result'] == '-1' else 0. for q in r]), None))
    # UCI occupancy CSV concatenates 3 files; the repeated header rows lack the id column
    r = [q for q in rows('occupancy') if q['id'] != 'date']; cols = ['Temperature', 'Humidity', 'Light', 'CO2', 'HumidityRatio']
    out.append((dict(id='building-room-occupancy-logreg-v1', task='classification', seed=20261117, features=['temperature_C', 'humidity_pct', 'light_lux', 'co2_ppm', 'humidity_ratio'], source=src('occupancy', excluded='id, date')),
                num(r, cols), np.array([float(q['Occupancy']) for q in r]), None))
    return out


def main():
    OUT.mkdir(exist_ok=True)
    man = {r['slug']: r for r in json.loads((SRC / 'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))['sources']}
    for slug, r in man.items():
        if hashlib.sha256((SRC / (slug + '.csv')).read_bytes()).hexdigest() != r['csv_sha256']: raise ValueError('source changed ' + slug)
    results = []
    for spec, x, y, groups in datasets(man):
        assert x.shape[1] == len(spec['features']) <= 32 and np.all(np.isfinite(x)) and len(x) == len(y)
        res = train(spec, x, y, groups); res['spec'] = {k: v for k, v in spec.items() if k != 'features'}; results.append(res)
        m = res.get('test_metrics', {}); print(res['status'], spec['id'], 'F=%d' % len(spec['features']), res.get('admission', ''),
                                                  {k: round(v, 3) for k, v in m.items() if isinstance(v, float)})
    (OUT / 'ZOO_TRAINING.json').write_text(json.dumps(dict(status='ZOO_TRAINING_DONE', numpy=np.__version__, python=platform.python_version(), models=results,
                                                           field_validation=False, deployment_approved=False), ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
