"""Train the 4-feature logistic regression used by the EdgeFHE CKKS demo.

Usage (from C:/GitHub/Grad):
    training/.venv/Scripts/python training/train.py          # Windows
    training/.venv/bin/python training/train.py              # Linux / WSL

Inputs : data/cdc_diabetes/splits/{train,validation,test}.csv  (tools/prepare_datasets.py)
Outputs: training/outputs/model.json            public model for the cloud, repo model format
         training/outputs/edge_preprocess.json  feature order, mean, std and clip for the KV260
         training/outputs/training_report.json  selection, metrics and checks for the dashboard

The 4 features are chosen greedily on the validation set; the test set is scored once, at
the end. Logits are computed exactly as the deployed system does: standardize with the
train-set mean/std, clip to +-input_abs_max, then w.z + b; decision = logit > 0.
"""
import csv
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np
import sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, roc_auc_score, roc_curve

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / 'data' / 'cdc_diabetes'
OUT = HERE / 'outputs'
LABEL = 'Diabetes_binary'
SELECTED = 4
MODEL_ID = 'cdc-diabetes-lr4-v1'
# Fixed profile limits (software/native_ckks/core.hpp and model.py).
DEGREE, SCALE = 4096, 2 ** 29
INPUT_ABS_MAX = 16.0
MAX_ABS_PARAMETER = 16.0
MAX_LOGIT_ERROR = 5e-4


def load(split):
    path = DATA / 'splits' / f'{split}.csv'
    with open(path, newline='', encoding='utf-8') as f:
        header = next(csv.reader(f))
    values = np.loadtxt(path, delimiter=',', skiprows=1)
    columns = [h for h in header if h not in (LABEL, 'ID')]
    return columns, values[:, [header.index(c) for c in columns]], values[:, header.index(LABEL)].astype(int)


def analytic_bound(weights, input_abs_max):
    """Same conservative CKKS logit error bound as native_ckks/model.py analytic_bound()."""
    ex = DEGREE / (2 * SCALE) + 2064 / SCALE + 1e-8
    ew = DEGREE / (2 * SCALE) + 1e-8
    eb = DEGREE / (2 * SCALE * SCALE) + 1e-8
    return sum(abs(w) * ex + input_abs_max * ew + ex * ew for w in weights) + eb + 1e-8


def metrics(y, logit):
    predicted = logit > 0
    tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel()
    fpr, tpr, _ = roc_curve(y, logit)
    keep = np.unique(np.linspace(0, len(fpr) - 1, 101).round().astype(int))  # light curve for the dashboard
    return {
        'rows': int(len(y)), 'auc': float(roc_auc_score(y, logit)),
        'accuracy': float((tp + tn) / len(y)), 'sensitivity': float(tp / (tp + fn)),
        'specificity': float(tn / (tn + fp)), 'precision': float(tp / (tp + fp)) if tp + fp else 0.0,
        'f1': float(2 * tp / (2 * tp + fp + fn)),
        'confusion': {'tn': int(tn), 'fp': int(fp), 'fn': int(fn), 'tp': int(tp)},
        'roc': [[round(float(fpr[i]), 5), round(float(tpr[i]), 5)] for i in keep],
    }


def main():
    started = time.time()
    columns, x_train, y_train = load('train')
    _, x_val, y_val = load('validation')
    mean = x_train.mean(axis=0)
    std = x_train.std(axis=0)
    if np.any(std == 0):
        raise SystemExit('A feature is constant on the training set')
    standardize = lambda x: np.clip((x - mean) / std, -INPUT_ABS_MAX, INPUT_ABS_MAX)  # noqa: E731
    z_train, z_val = standardize(x_train), standardize(x_val)

    def fit(indices):
        return LogisticRegression(class_weight='balanced', max_iter=2000).fit(z_train[:, indices], y_train)

    def val_auc(indices):
        return float(roc_auc_score(y_val, fit(indices).decision_function(z_val[:, indices])))

    single = sorted(({'feature': c, 'validation_auc': val_auc([i])} for i, c in enumerate(columns)),
                    key=lambda item: -item['validation_auc'])
    print('single-feature validation AUC:', ', '.join(f"{s['feature']}={s['validation_auc']:.4f}" for s in single[:6]))

    selected, path = [], []
    for step in range(1, SELECTED + 1):
        trials = [(val_auc(selected + [i]), i) for i in range(len(columns)) if i not in selected]
        best_auc, best = max(trials)
        selected.append(best)
        path.append({'step': step, 'added': columns[best], 'validation_auc': best_auc,
                     'runner_up': columns[sorted(trials)[-2][1]], 'runner_up_auc': sorted(trials)[-2][0]})
        print(f'step {step}: + {columns[best]} -> validation AUC {best_auc:.4f}')

    model = fit(selected)
    features = [columns[i] for i in selected]
    weights = [float(w) for w in model.coef_[0]]
    bias = float(model.intercept_[0])
    bound = analytic_bound(weights, INPUT_ABS_MAX)
    logit = lambda z: z[:, selected] @ np.array(weights) + bias  # noqa: E731
    if not np.allclose(logit(z_val), model.decision_function(z_val[:, selected]), atol=1e-9):
        raise SystemExit('Manual logits disagree with scikit-learn')
    checks = {
        'max_abs_weight': max(abs(w) for w in weights), 'abs_bias': abs(bias),
        'input_abs_max': INPUT_ABS_MAX, 'error_bound': bound, 'max_logit_error': MAX_LOGIT_ERROR,
        'clipped_rows': {}, 'passes': bool(max(abs(w) for w in weights) <= MAX_ABS_PARAMETER
                                           and abs(bias) <= MAX_ABS_PARAMETER and bound <= MAX_LOGIT_ERROR),
    }
    if not checks['passes']:
        raise SystemExit(f'Model violates the CKKS profile limits: {checks}')

    # The test set is scored once, after every choice above was made on validation.
    _, x_test, y_test = load('test')
    results = {}
    for name, x, y in (('validation', x_val, y_val), ('test', x_test, y_test)):
        raw = (x[:, selected] - mean[selected]) / std[selected]
        checks['clipped_rows'][name] = int(np.any(np.abs(raw) > INPUT_ABS_MAX, axis=1).sum())
        results[name] = metrics(y, logit(standardize(x)))
    checks['clipped_rows']['train'] = int(np.any(np.abs((x_train[:, selected] - mean[selected]) / std[selected])
                                                 > INPUT_ABS_MAX, axis=1).sum())

    OUT.mkdir(exist_ok=True)
    cloud_model = {'model_id': MODEL_ID, 'weights': weights, 'bias': bias,
                   'input_abs_max': INPUT_ABS_MAX, 'max_logit_error': MAX_LOGIT_ERROR}
    (OUT / 'model.json').write_text(json.dumps(cloud_model, indent=2) + '\n', encoding='utf-8')
    manifest = DATA / 'manifest.json'
    edge = {'model_id': MODEL_ID, 'features': features, 'mean': [float(mean[i]) for i in selected],
            'std': [float(std[i]) for i in selected], 'clip': INPUT_ABS_MAX, 'label': LABEL,
            'decision': 'class 1 when logit > 0 (probability > 0.5)',
            'dataset_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}
    (OUT / 'edge_preprocess.json').write_text(json.dumps(edge, indent=2) + '\n', encoding='utf-8')
    dataset = json.loads(manifest.read_text(encoding='utf-8'))
    report = {
        'generated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'model_id': MODEL_ID,
        'dataset': {'name': dataset['dataset'], 'raw_sha256': dataset['raw_sha256'], 'seed': dataset['seed'],
                    'splits': {k: {'rows': v['rows'], 'positive_rate': v['positive_rate']}
                               for k, v in dataset['splits'].items()}},
        'environment': {'python': platform.python_version(), 'numpy': np.__version__,
                        'scikit_learn': sklearn.__version__, 'platform': platform.platform()},
        'method': {'model': 'logistic regression', 'class_weight': 'balanced',
                   'selection': f'greedy forward selection of {SELECTED} features by validation ROC-AUC',
                   'standardization': 'train-set mean and std, clipped to +-input_abs_max',
                   'decision': 'logit > 0'},
        'single_feature': single, 'selection_path': path,
        'features': features, 'weights': weights, 'bias': bias,
        'mean': edge['mean'], 'std': edge['std'], 'system_checks': checks, 'metrics': results,
        'baseline_test_accuracy_all_negative': float(1 - y_test.mean()),
        'seconds': round(time.time() - started, 2),
    }
    (OUT / 'training_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    for name, m in results.items():
        print(f"{name:10s} AUC={m['auc']:.4f} accuracy={m['accuracy']:.4f} "
              f"sensitivity={m['sensitivity']:.4f} specificity={m['specificity']:.4f}")
    print(f'features={features} error_bound={bound:.3e} (limit {MAX_LOGIT_ERROR})')
    print(f'TRAINING_DONE -> {OUT}')


if __name__ == '__main__':
    main()
