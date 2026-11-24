"""Convert the CDC 4-feature logistic regression into the V8b rev5 model artifacts of the demo.

The CDC model of training/outputs (cdc-diabetes-lr4-v1) is not part of the frozen V8b catalogue. The demo overlay
(demo/overlay/make_overlay.py) adds it to a staged copy of v8b_rev5 outside the repository; no byte under v8b_rev5/
changes. This converter derives, byte for byte, what that overlay needs, and it maintains the derived fields of the
form schema that the gateway, the owner screen and the rehearsal driver read:

  cdc-diabetes-lr4-v1.json  Model file, schema edgefhe.affine-research.v1, serialized like
                            v8b_rev5/models/zoo19/train_zoo.py: json.dumps(indent=2, ensure_ascii=False) + LF.
                            Its SHA-256 is the catalogue source_sha256 and the wire model_id.
  catalog_line.hpp          The single Model{...} line plus LF (format of v8b_rev5/models/zoo19/qa_native_zoo.py)
                            that the overlay inserts before the final '};}' line of
                            software/client/models/model_catalog.hpp, so the CDC entry becomes catalogue()[3].
  EXPECTED.json             What the overlay must reproduce: model JSON, catalogue before and after, the kept
                            profile id (profile option P1) and staged_tree_id = SHA-256 of the staged SHA256SUMS.
  form_cdc.json             Form schema. Its text is maintained by hand. This converter owns the derived fields
                            (model block, BMI normalization bound, logit edges, preset summaries and expected
                            results, level shares and rates) and, with --levels, the level counts of the local
                            test split. Every JSON file here uses the serialization above.

usage (from the repository root; Python 3.10+ standard library only):
  python -I -B demo/model/convert_cdc_to_v8b.py --repo . --check             compare every committed byte (CI-safe)
  python -I -B demo/model/convert_cdc_to_v8b.py --repo . --levels            recompute the level table (local splits)
  python -I -B demo/model/convert_cdc_to_v8b.py --repo . --write [--levels]  rewrite the files of demo/model
  python -I -B demo/model/convert_cdc_to_v8b.py --repo . --out DIR [--levels]  write them to DIR (outside the repo)
"""
import argparse
import csv
import decimal
import hashlib
import json
import math
import re
import sys
from pathlib import Path

MODEL_ID = 'cdc-diabetes-lr4-v1'
MODEL_FILE = MODEL_ID + '.json'
LINE_FILE = 'catalog_line.hpp'
EXPECTED_FILE = 'EXPECTED.json'
FORM_FILE = 'form_cdc.json'
FORM_SCHEMA = 'edgefhe.demo.form.v1'
EXPECTED_SCHEMA = 'edgefhe.demo.overlay-expected.v1'

# Frozen identities. Another value means a new model or another V8b base; the dissector, the gateway configuration,
# the permits and the overlay change with it, so the converter stops instead of producing it.
PIN = {
    'model_json': '63cee36f9ac68b8e274dc45b4fa2fee0aeae5c4089b3d18fe51d7cf0c588c106',
    'catalogue_before': 'f66e4d760b659f2e44bb7fa96b144ab9febac65838a5f3e71113814cdd04850d',
    'catalogue_after': '840bab207b516e3a36a3bd4407679684b73f2b040c960b4bb4791c5a334d3425',
    'profile': 'c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c',
}

# Paths inside v8b_rev5 (which is also the root of the staged tree).
CATALOGUE = 'software/client/models/model_catalog.hpp'
PROFILE_ID = 'software/client/profile_id.hpp'
DESCRIPTOR = 'software/client/PROFILE_DESCRIPTOR.json'
DRIVER_PROFILE = 'software/driver/profile_identity_v8.h'
STAGED_MODEL = 'models/zoo19/models_release_v1/' + MODEL_FILE
SUMS = 'SHA256SUMS'
TAIL = b'};}\n'
CATALOGUE_ENTRIES = 3

SCALE = 1 << 30                   # weight scale 2^30; output scale 2^60
INPUT_ABS_MAX = 8                 # normalize.cpp:29 refuses |z| > 8; there is no clipping
NO_WRAP_FACTOR = 35184372486187   # factor of no_wrap_coefficient_bound in every frozen zoo model (models_release_v1)
BOUND_PLACES = 4                  # client_normalization_max: largest value with this many decimals that passes |z| <= 8
SHARE_PLACES = 4                  # rounding of observed_share and observed_rate

REFERENCE_LOGIT = ('bias + sum over the features in model order of weights[f] * ((x[f] - mean[f]) / std[f]), with '
                   'the model JSON doubles (client.cpp:276); a row with any |z| > 8 is refused (normalize.cpp:29)')
LEVELS_COMMAND = 'python -I -B demo/model/convert_cdc_to_v8b.py --repo . --levels --write'


class ConvertError(Exception):
    pass


def need(condition, message):
    if not condition:
        raise ConvertError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def dump_json(obj):
    """Serialization of the zoo model files (train_zoo.py) and of every JSON file of demo/model."""
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def read_json(path):
    return json.loads(Path(path).read_bytes().decode('utf-8'))


# ----------------------------------------------------------------------------------------------------- model file
def build_model(repo):
    """The model dict and its exact bytes. Key order and values are frozen: the SHA-256 of these bytes is the
    catalogue source_sha256 and the wire model_id (PIN['model_json'])."""
    out = repo / 'training' / 'outputs'
    v4 = read_json(out / 'model.json')
    edge = read_json(out / 'edge_preprocess.json')
    report = read_json(out / 'training_report.json')
    manifest_bytes = (repo / 'data' / 'cdc_diabetes' / 'manifest.json').read_bytes()
    man = json.loads(manifest_bytes.decode('utf-8'))
    need(v4['model_id'] == edge['model_id'] == report['model_id'] == MODEL_ID, 'training/outputs name another model')
    need(report['weights'] == v4['weights'] and report['bias'] == v4['bias'], 'model.json and training_report.json disagree')
    need(report['mean'] == edge['mean'] and report['std'] == edge['std'], 'edge_preprocess.json and training_report.json disagree')
    need(report['features'] == edge['features'], 'feature order differs between training outputs')
    need(sha256(manifest_bytes) == edge['dataset_manifest_sha256'], 'data/cdc_diabetes/manifest.json is not the training manifest')
    v4_canonical = sha256(json.dumps(v4, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8'))
    w = [float(x) for x in v4['weights']]
    b = float(v4['bias'])
    wi = [int(round(x * SCALE)) for x in w]   # x * 2^30 is exact; Python round() is round-half-even
    bi = int(round(b * SCALE * SCALE))        # b * 2^60 is exact
    l1 = sum(abs(x) for x in wi)
    model = {
        'schema': 'edgefhe.affine-research.v1', 'model_id': v4['model_id'], 'profile_sha256': driver_profile(repo),
        'operation': 'public-scalar-affine', 'max_depth': 1, 'features': edge['features'], 'mean': edge['mean'],
        'std': edge['std'], 'weights': w, 'bias': b, 'weights_int': wi, 'bias_int': bi, 'weight_scale': SCALE,
        'output_scale': SCALE * SCALE, 'input_abs_max': INPUT_ABS_MAX,
        'no_wrap_coefficient_bound': str(NO_WRAP_FACTOR * l1 + abs(bi)),
        'task': 'classification', 'decision_logit': 0.0, 'target_mean': 0.0, 'target_std': 1.0,
        'training_source': {
            'name': man['dataset'], 'uci_id': 891, 'data_url': man['source_url'], 'doi': '10.24432/C53919',
            'sha256': man['raw_sha256'], 'dataset_manifest_sha256': edge['dataset_manifest_sha256'],
            'license': man['license'], 'label': edge['label'],
            'method': 'scikit-learn LogisticRegression(class_weight=balanced, max_iter=2000); '
                      'greedy forward selection of 4 features by validation ROC-AUC (training/train.py)',
            'v4_model_canonical_sha256': v4_canonical, 'v4_input_clip': v4['input_abs_max'],
            'probability_note': 'sigmoid(logit) of a class-balanced fit; not a calibrated disease probability'},
        'split_seed': man['seed'], 'split': man['split_rule'], 'deployment_approved': False,
        'output_policy': 'owner_only_no_cloud_feedback', 'feature_padding': 'groups_of_four_zero_pad_public_extra_features',
    }
    need(all(abs(x) <= 16 * SCALE for x in wi) and abs(bi) < (1 << 62), 'integer weights exceed the cloud bounds')
    return model, dump_json(model)


def driver_profile(repo):
    """The V8b hardware profile id (32 bytes of ef_profile in the driver header) as lowercase hex."""
    text = (repo / 'v8b_rev5' / DRIVER_PROFILE).read_bytes().decode('ascii')
    start = text.index('ef_profile[32]')
    body = text[text.index('{', start) + 1:text.index('}', start)]
    values = [int(v, 16) for v in re.findall(r'0x([0-9a-fA-F]{2})', body)]
    need(len(values) == 32, 'cannot read ef_profile from ' + DRIVER_PROFILE)
    return bytes(values).hex()


def catalogue_line(model, model_sha):
    """Model{id,source_sha256,features,mean,sd,weights,weights_int,bias,bias_int,uci,dictionary}, + LF (models.hpp)."""
    def doubles(values):
        return '{' + ','.join(float(v).hex() for v in values) + '}'
    parts = [json.dumps(model['model_id']), json.dumps(model_sha),
             '{' + ','.join(json.dumps(f) for f in model['features']) + '}',
             doubles(model['mean']), doubles(model['std']), doubles(model['weights']),
             '{' + ','.join(str(v) + 'LL' for v in model['weights_int']) + '}',
             float(model['bias']).hex(), str(model['bias_int']) + 'LL', '0', '0']
    return ('Model{' + ','.join(parts) + '},\n').encode('ascii')


# ------------------------------------------------------------------------------------------- overlay expectations
def parse_sums(data):
    need(data.endswith(b'\n') and b'\r' not in data, 'SHA256SUMS must be LF-terminated')
    entries = {}
    for line in data.decode('utf-8').split('\n')[:-1]:
        digest, sep, name = line.partition('  ')
        need(sep and re.fullmatch('[0-9a-f]{64}', digest) and name and name not in entries, 'malformed SHA256SUMS line')
        entries[name] = digest
    return entries


def sums_text(entries):
    """The bytes v8b_rev5/tools/update_sums.py writes for this file set (sorted POSIX paths, two spaces, LF)."""
    return ''.join('%s  %s\n' % (entries[name], name) for name in sorted(entries)).encode('utf-8')


def canonical_descriptor_sha(data):
    """profile_hex rule: SHA-256 of the descriptor as canonical sorted compact JSON."""
    return sha256(json.dumps(json.loads(data.decode('utf-8')), sort_keys=True, separators=(',', ':')).encode('utf-8'))


def build_expected(repo, model_bytes, line_bytes):
    v8 = repo / 'v8b_rev5'
    before = (v8 / CATALOGUE).read_bytes()
    need(before.endswith(TAIL) and b'\r' not in before and before.count(b'Model{') == CATALOGUE_ENTRIES,
         'unexpected base catalogue layout')
    need(line_bytes.count(b'\n') == 1 and line_bytes.endswith(b'},\n'), 'catalogue line must be one line')
    after = before[:-len(TAIL)] + line_bytes + TAIL
    pid = (v8 / PROFILE_ID).read_bytes()
    found = re.findall(rb'profile_hex="([0-9a-f]{64})"', pid)
    need(len(found) == 1, 'cannot read profile_hex from ' + PROFILE_ID)
    profile = found[0].decode('ascii')
    descriptor = (v8 / DESCRIPTOR).read_bytes()
    need(canonical_descriptor_sha(descriptor) == profile, 'profile_hex is not the canonical hash of ' + DESCRIPTOR)
    sums = (v8 / SUMS).read_bytes()
    base = parse_sums(sums)
    need(sums_text(base) == sums, 'v8b_rev5/SHA256SUMS is not in the form tools/update_sums.py writes')
    for name, data in ((CATALOGUE, before), (PROFILE_ID, pid), (DESCRIPTOR, descriptor)):
        need(base.get(name) == sha256(data), 'v8b_rev5 working tree differs from SHA256SUMS at %s '
             '(run python -B v8b_rev5/tools/verify_repo.py)' % name)
    need(STAGED_MODEL not in base, STAGED_MODEL + ' already exists in the base tree')
    staged = dict(base)
    staged[CATALOGUE] = sha256(after)
    staged[STAGED_MODEL] = sha256(model_bytes)
    return {
        'schema': EXPECTED_SCHEMA,
        'generator': 'demo/model/convert_cdc_to_v8b.py',
        'model_id': MODEL_ID,
        'model_json': sha256(model_bytes),
        'model_json_bytes': len(model_bytes),
        'model_json_staged_path': STAGED_MODEL,
        'catalog_line': sha256(line_bytes),
        'catalog_line_bytes': len(line_bytes),
        'catalogue_path': CATALOGUE,
        'catalogue_before': sha256(before),
        'catalogue_after': sha256(after),
        'catalogue_entries_before': CATALOGUE_ENTRIES,
        'catalogue_index': CATALOGUE_ENTRIES,
        'profile_option': 'P1',
        'profile': profile,
        'profile_id_hpp': sha256(pid),
        'profile_descriptor': sha256(descriptor),
        'profile_descriptor_note': 'unchanged by the overlay and read by no build or runtime step: in the staged tree it '
                                   'lists the catalogue hash before the overlay, and the base tree already lists '
                                   'owner.cpp with an older hash (known deviation of profile option P1, which keeps '
                                   'the profile id)',
        'base_tree_files': len(base),
        'base_sums': sha256(sums),
        'staged_tree_files': len(staged),
        'staged_tree_id': sha256(sums_text(staged)),
        'staged_tree_id_rule': 'SHA-256 of the SHA256SUMS that the staged tools/update_sums.py writes '
                               '(paths relative to the staged v8b_rev5 root)',
        'changed': [CATALOGUE],
        'added': [STAGED_MODEL],
    }


# ---------------------------------------------------------------------------------------------- form derivation
def reference_logit(model, x):
    """Plaintext reference in the client's order (client.cpp:276)."""
    acc = model['bias']
    for f in range(len(model['features'])):
        acc += model['weights'][f] * ((x[f] - model['mean'][f]) / model['std'][f])
    return acc


def z_values(model, x):
    return [(x[f] - model['mean'][f]) / model['std'][f] for f in range(len(model['features']))]


def logit_of_p(p):
    """ln(p / (1 - p)) for the decimal value of p, correctly rounded to a double (no libm dependence)."""
    with decimal.localcontext() as ctx:
        ctx.prec = 60
        d = decimal.Decimal(repr(p))
        need(0 < d < 1, 'edge outside (0, 1)')
        return float((d / (1 - d)).ln())


def classify(logit, edges, band):
    """(level, boundary): level = 1 + number of edges below the logit; boundary = adjacent levels within the band."""
    for i, edge in enumerate(edges):
        if abs(logit - edge) <= band:
            return None, [i + 1, i + 2]
    return 1 + sum(1 for edge in edges if logit > edge), None


def normalization_max(mean, sd, abs_max, places):
    """Largest value with `places` decimals whose normalized value passes the client check |z| <= abs_max."""
    scale = 10 ** places

    def value(k):
        return float('%d.%0*d' % (k // scale, places, k % scale))

    def accepted(k):
        return abs((value(k) - mean) / sd) <= abs_max
    k = int(math.floor((mean + abs_max * sd) * scale))
    while not accepted(k):
        k -= 1
    while accepted(k + 1):
        k += 1
    return value(k)


def refusal(form, values):
    """Reference form validation: the refusal code, or None when the form is accepted."""
    fields = form['fields']
    if not isinstance(values, dict) or sorted(values) != sorted(f['name'] for f in fields):
        return form['refuse_fields']
    for f in fields:
        v = values[f['name']]
        if f['kind'] == 'choice':
            if type(v) is not int:
                return f['refuse_invalid']
            for option in f.get('refused_options', []):
                if v == option['value']:
                    return option['code']
            if v not in [option['value'] for option in f['options']]:
                return f['refuse_invalid']
        else:
            if type(v) not in (int, float) or not math.isfinite(v) or round(v, f['decimals']) != v:
                return f['refuse_invalid']
            if v < f['minimum']:
                return f['refuse_below']
            if v > f['maximum']:
                return f['refuse_above']
    return None


def summary(form, values):
    parts = []
    for f in form['fields']:
        v = values[f['name']]
        if f['kind'] == 'choice':
            options = f['options'] + f.get('refused_options', [])
            labels = [option['label'] for option in options if option['value'] == v]
            text = labels[0] if labels else str(v)
        else:
            text = '%.*f' % (f['decimals'], v)
        parts.append(f['short'] + '：' + text)
    return '；'.join(parts)


def derive_form(form, model, model_sha, counts=None):
    """A copy of the form schema with every derived field recomputed (counts: a fresh level table, else kept)."""
    form = json.loads(json.dumps(form))
    need(form.get('schema') == FORM_SCHEMA, FORM_FILE + ': schema must be ' + FORM_SCHEMA)
    features = model['features']
    form['model'] = {
        'model_id': model['model_id'], 'file': MODEL_FILE, 'sha256': model_sha, 'features': list(features),
        'csv_header': ','.join(features), 'normalization_abs_max': model['input_abs_max'],
        'reference_logit': REFERENCE_LOGIT}
    fields = form['fields']
    need([f['name'] for f in fields] == features, FORM_FILE + ': fields must follow the model order ' + ','.join(features))
    codes = [form['refuse_fields']]
    for i, f in enumerate(fields):
        codes.append(f['refuse_invalid'])
        if f['kind'] == 'number':
            bound = normalization_max(model['mean'][i], model['std'][i], model['input_abs_max'], BOUND_PLACES)
            f['client_normalization_max'] = bound
            need(f['minimum'] <= f['maximum'] <= bound, FORM_FILE + ': %s maximum above the normalization bound' % f['name'])
            need(abs((f['minimum'] - model['mean'][i]) / model['std'][i]) <= model['input_abs_max'],
                 FORM_FILE + ': %s minimum below the normalization bound' % f['name'])
            codes += [f['refuse_below'], f['refuse_above']]
        else:
            need(f['kind'] == 'choice', FORM_FILE + ': kind must be choice or number')
            for option in f['options']:
                need(type(option['value']) is int and abs((option['value'] - model['mean'][i]) / model['std'][i])
                     <= model['input_abs_max'], FORM_FILE + ': %s option outside the normalization bound' % f['name'])
            for option in f.get('refused_options', []):
                need(option['value'] not in [o['value'] for o in f['options']], FORM_FILE + ': refused option is offered')
                codes.append(option['code'])
    need(set(codes) == set(form['refusals']), FORM_FILE + ': every refusal code needs exactly one message')

    levels = form['levels']
    edges = [logit_of_p(p) for p in levels['edges_p_balanced']]
    need(all(a < b for a, b in zip(edges, edges[1:])), FORM_FILE + ': edges must increase')
    levels['edges_logit'] = edges
    band = levels['band_abs_logit']
    need(0 < band and all(b - a > 2 * band for a, b in zip(edges, edges[1:])), FORM_FILE + ': band too wide')
    items = levels['items']
    need(len(items) == len(edges) + 1, FORM_FILE + ': one level more than edges')
    for i, item in enumerate(items):
        need(item['level'] == i + 1 and item['code'] == 'L%d' % (i + 1), FORM_FILE + ': levels must be L1..Ln')
        item['logit_lower'] = edges[i - 1] if i else None
        item['logit_upper'] = edges[i] if i < len(edges) else None
    if counts is not None:
        levels['observed_source'] = counts['source']
        for i, item in enumerate(items):
            item['observed_rows'] = counts['rows'][i]
            item['observed_positives'] = counts['positives'][i]
    source = levels['observed_source']
    need(isinstance(source, dict), FORM_FILE + ': no level table yet; run ' + LEVELS_COMMAND + ' with the local splits')
    need(source['edges_p_balanced'] == levels['edges_p_balanced'] and source['band_abs_logit'] == band,
         FORM_FILE + ': the level table was computed for other edges; run ' + LEVELS_COMMAND)
    evaluated = source['rows_evaluated']
    need(sum(item['observed_rows'] for item in items) == evaluated
         and sum(item['observed_positives'] for item in items) == source['positives_evaluated']
         and source['rows_total'] == evaluated + source['rows_refused_abs_z_above_8'],
         FORM_FILE + ': level counts do not add up')
    for item in items:
        item['observed_share'] = round(item['observed_rows'] / evaluated, SHARE_PLACES)
        item['observed_rate'] = round(item['observed_positives'] / item['observed_rows'], SHARE_PLACES) if item['observed_rows'] else None

    ids = [p['id'] for p in form['presets']]
    need(len(set(ids)) == len(ids), FORM_FILE + ': preset ids must be unique')
    for preset in form['presets']:
        values = preset['form']
        code = refusal(form, values)
        need(code != form['refuse_fields'] and all(type(values[n]) in (int, float) for n in features),
             FORM_FILE + ': preset %s must give every field a number' % preset['id'])
        x = [float(values[n]) for n in features]
        max_abs_z = max(abs(z) for z in z_values(model, x))
        preset['summary'] = summary(form, values)
        if code is None:
            need(max_abs_z <= model['input_abs_max'], FORM_FILE + ': preset %s passes the form but not the client' % preset['id'])
            logit = reference_logit(model, x)
            level, boundary = classify(logit, edges, band)
            preset['expected'] = {'refused': None, 'logit_plain': logit, 'level': level, 'boundary': boundary,
                                  'max_abs_z': max_abs_z}
        else:
            preset['expected'] = {'refused': code, 'logit_plain': None, 'level': None, 'boundary': None,
                                  'max_abs_z': max_abs_z}
    return form


# ------------------------------------------------------------------------------------------------ level table
def level_counts(repo, model, form):
    """Rows and positives per level on the local test split (untracked; checked against the dataset manifest)."""
    manifest_path = repo / 'data' / 'cdc_diabetes' / 'manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    man = json.loads(manifest_bytes.decode('utf-8'))
    split = man['splits']['test']
    relative = 'data/cdc_diabetes/splits/' + split['file']
    path = repo / relative
    need(path.is_file(), 'missing local test split %s (untracked; regenerate it as described in data/README.md)' % relative)
    raw = path.read_bytes()
    need(sha256(raw) == split['sha256'], relative + ' does not match data/cdc_diabetes/manifest.json')
    levels = form['levels']
    edges = [logit_of_p(p) for p in levels['edges_p_balanced']]
    band = levels['band_abs_logit']
    reader = csv.reader(raw.decode('utf-8').splitlines())
    header = next(reader)
    columns = [header.index(name) for name in model['features']]
    label = header.index(man['label'])
    total = refused = in_band = 0
    rows = [0] * (len(edges) + 1)
    positives = [0] * (len(edges) + 1)
    for record in reader:
        total += 1
        y = int(record[label])
        need(y in (0, 1), 'label must be 0 or 1')
        x = [float(record[c]) for c in columns]
        if any(not math.isfinite(z) or abs(z) > model['input_abs_max'] for z in z_values(model, x)):
            refused += 1
            continue
        logit = reference_logit(model, x)
        k = sum(1 for edge in edges if logit > edge)
        rows[k] += 1
        positives[k] += y
        if any(abs(logit - edge) <= band for edge in edges):
            in_band += 1
    need(total == split['rows'], relative + ': row count differs from the manifest')
    source = {
        'split': 'test',
        'file': relative,
        'file_sha256': split['sha256'],
        'manifest': 'data/cdc_diabetes/manifest.json',
        'manifest_sha256': sha256(manifest_bytes),
        'label': man['label'] + ' (0 = no diabetes, 1 = prediabetes or diabetes)',
        'logit': 'plaintext reference logit (model.reference_logit) on the raw test values',
        'edges_p_balanced': levels['edges_p_balanced'],
        'band_abs_logit': band,
        'edges_note': 'edges fixed before this computation; not tuned on the test split',
        'command': LEVELS_COMMAND,
        'rows_total': total,
        'rows_refused_abs_z_above_8': refused,
        'rows_evaluated': total - refused,
        'positives_evaluated': sum(positives),
        'rows_in_band': in_band,
    }
    return {'source': source, 'rows': rows, 'positives': positives}


# ------------------------------------------------------------------------------------------------------ driver
def build_all(repo, levels=False):
    model, model_bytes = build_model(repo)
    model_sha = sha256(model_bytes)
    line_bytes = catalogue_line(model, model_sha)
    expected = build_expected(repo, model_bytes, line_bytes)
    for name, value in PIN.items():
        need(expected[name] == value, 'frozen identity changed: %s=%s (pinned %s)' % (name, expected[name], value))
    form_path = repo / 'demo' / 'model' / FORM_FILE
    need(form_path.is_file(), 'missing ' + FORM_FILE + ' (the hand-maintained form schema)')
    form_in = read_json(form_path)
    counts = level_counts(repo, model, form_in) if levels else None
    form = derive_form(form_in, model, model_sha, counts)
    files = {MODEL_FILE: model_bytes, LINE_FILE: line_bytes, EXPECTED_FILE: dump_json(expected), FORM_FILE: dump_json(form)}
    return {'model': model, 'expected': expected, 'form': form, 'files': files}


def print_levels(form):
    levels = form['levels']
    s = levels['observed_source']
    print('CDC_LEVELS split=%s rows=%d refused=%d evaluated=%d positives=%d in_band=%d' % (
        s['split'], s['rows_total'], s['rows_refused_abs_z_above_8'], s['rows_evaluated'], s['positives_evaluated'],
        s['rows_in_band']))
    for item in levels['items']:
        lower = '-inf' if item['logit_lower'] is None else '%.6f' % item['logit_lower']
        upper = '+inf' if item['logit_upper'] is None else '%.6f' % item['logit_upper']
        rate = 'n/a' if item['observed_rate'] is None else '%.4f' % item['observed_rate']
        print('CDC_LEVEL %s logit=(%s,%s] rows=%d share=%.4f positives=%d observed_rate=%s' % (
            item['code'], lower, upper, item['observed_rows'], item['observed_share'], item['observed_positives'], rate))


def write_files(files, target):
    target.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = target / name
        if path.is_file() and path.read_bytes() == data:
            print('CDC_MODEL_UNCHANGED %s' % name)
        else:
            path.write_bytes(data)
            print('CDC_MODEL_WRITTEN %s sha256=%s bytes=%d' % (name, sha256(data), len(data)))


def main(argv=None):
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--repo', default=str(here.parent.parent), help='repository root (default: two levels up)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='regenerate in memory and compare with demo/model')
    mode.add_argument('--write', action='store_true', help='rewrite the files of demo/model')
    mode.add_argument('--out', help='write the files to this directory (outside the repository)')
    ap.add_argument('--levels', action='store_true', help='recompute the level table from the local test split')
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    model_dir = repo / 'demo' / 'model'
    try:
        need((repo / 'v8b_rev5' / SUMS).is_file() and (repo / 'training' / 'outputs').is_dir(), 'not the repository root: %s' % repo)
        if not (a.check or a.write or a.out or a.levels):
            ap.error('choose --check, --levels, --write or --out')
        built = build_all(repo, levels=a.levels)
        files, expected = built['files'], built['expected']
        if a.levels:
            print_levels(built['form'])
        if a.check or (a.levels and not (a.write or a.out)):
            names = list(files) if a.check else [FORM_FILE]
            bad = []
            for name in names:
                path = model_dir / name
                have = path.read_bytes() if path.is_file() else None
                if have != files[name]:
                    bad.append('%s(have=%s,want=%s)' % (name, sha256(have) if have is not None else 'missing', sha256(files[name])))
            if bad:
                print(('CDC_MODEL_CHECK_FAIL ' if a.check else 'CDC_LEVELS_DIFFER ') + ' '.join(bad))
                print('regenerate with: python -I -B demo/model/convert_cdc_to_v8b.py --repo . --write' + (' --levels' if a.levels else ''))
                return 1
            if a.check:
                print('CDC_MODEL_CHECK_PASS model=%s catalogue_after=%s profile=%s staged_tree_id=%s' % (
                    expected['model_json'], expected['catalogue_after'], expected['profile'], expected['staged_tree_id']))
            else:
                print('CDC_LEVELS_MATCH %s' % FORM_FILE)
            return 0
        if a.out:
            target = Path(a.out).resolve()
            need(target != repo and repo not in target.parents, '--out must lie outside the repository')
        else:
            target = model_dir
        write_files(files, target)
        print('CDC_MODEL_READY model=%s catalogue_after=%s profile=%s staged_tree_id=%s' % (
            expected['model_json'], expected['catalogue_after'], expected['profile'], expected['staged_tree_id']))
        return 0
    except (ConvertError, OSError, KeyError, IndexError, ValueError, TypeError) as e:
        print('%s %s: %s' % ('CDC_MODEL_CHECK_FAIL' if a.check else 'CDC_MODEL_FAIL', type(e).__name__, e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
