"""Unit tests of the CDC model artifacts in demo/model (standard library only; no network, no device).

The checks restate the client arithmetic independently of convert_cdc_to_v8b.py: the reference logit of
client.cpp:276, the |z| <= 8 refusal of normalize.cpp:29 and the catalogue entry layout of models.hpp. The level
table is recomputed only when the untracked local test split exists (see data/README.md); otherwise that test is
skipped and the recorded counts are checked for consistency.

usage: python -B demo/model/test_model.py          (ends with OK)
"""
import csv
import hashlib
import importlib.util
import io
import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
V8 = ROOT / 'v8b_rev5'
CONVERTER = HERE / 'convert_cdc_to_v8b.py'
SPLIT = ROOT / 'data' / 'cdc_diabetes' / 'splits' / 'test.csv'

MODEL_SHA = '63cee36f9ac68b8e274dc45b4fa2fee0aeae5c4089b3d18fe51d7cf0c588c106'
CATALOGUE_BEFORE = 'f66e4d760b659f2e44bb7fa96b144ab9febac65838a5f3e71113814cdd04850d'
CATALOGUE_AFTER = '840bab207b516e3a36a3bd4407679684b73f2b040c960b4bb4791c5a334d3425'
PROFILE = 'c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c'
PROFILE_ID_HPP = '01938b185c68248dc3fd8bb98a6a250dcd2fda0efacdc48bddce490aa24be482'
W_INT = [749408897, 478576138, 543091430, 592544664]
B_INT = -666458326697820288
CATALOGUE = 'software/client/models/model_catalog.hpp'
STAGED_MODEL = 'models/zoo19/models_release_v1/cdc-diabetes-lr4-v1.json'

# Presets as fixed by the demo plan: (id, GenHlth, HighBP, BMI, Age), plaintext logit to 6 decimals, outcome.
PLAN = [
    ('A', (1, 0, 22.0, 1), -3.713408, 1),
    ('C', (3, 0, 27.0, 7), -0.935837, 1),
    ('D', (3, 1, 25.0, 7), -0.188351, 2),
    ('E', (2, 1, 30.0, 9), -0.097814, 2),
    ('M', (3, 1, 31.2, 9), 0.648683, 3),
    ('F', (4, 1, 35.0, 10), 1.775369, 4),
    ('G', (5, 1, 45.0, 13), 3.739088, 4),
    ('H', (5, 1, 81.0, 13), 6.498770, 4),
    ('K', (4, 1, 26.0, 4), 0.000182, 'boundary'),
    ('J', (3, 0, 82.0, 7), None, 'refused'),
]

HEX_DOUBLE = re.compile(r'-?0x[01]\.[0-9a-f]+p[+-][0-9]+')
LINE = re.compile(r'Model\{"(?P<id>[^"]+)","(?P<sha>[0-9a-f]{64})",\{(?P<features>[^}]*)\},'
                  r'\{(?P<mean>[^}]*)\},\{(?P<sd>[^}]*)\},\{(?P<weights>[^}]*)\},\{(?P<wint>[^}]*)\},'
                  r'(?P<bias>[^,]+),(?P<bint>-?[0-9]+)LL,(?P<uci>[01]),(?P<dictionary>[01])\},')


def raw(name):
    return (HERE / name).read_bytes()


def load(name):
    return json.loads(raw(name).decode('utf-8'))


def dump(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def client_logit(model, x):
    """client.cpp:276: expected = bias; expected += weights[f] * ((raw - mean[f]) / sd[f]) in feature order."""
    total = model['bias']
    for f, value in enumerate(x):
        total = total + model['weights'][f] * ((value - model['mean'][f]) / model['std'][f])
    return total


def client_accepts(model, x):
    """normalize.cpp:27-29: refuse a non-finite value or |z| > 8 (no clipping)."""
    for f, value in enumerate(x):
        z = (value - model['mean'][f]) / model['std'][f]
        if not math.isfinite(z) or abs(z) > 8.0:
            return False
    return True


def mid_values(model):
    return [float(m) for m in model['mean']]


class ModelFile(unittest.TestCase):
    def setUp(self):
        self.bytes = raw('cdc-diabetes-lr4-v1.json')
        self.model = json.loads(self.bytes.decode('utf-8'))

    def test_frozen_bytes(self):
        self.assertEqual(sha(self.bytes), MODEL_SHA)
        self.assertEqual(len(self.bytes), 2247)
        self.assertNotIn(b'\r', self.bytes)
        self.assertEqual(dump(self.model), self.bytes, 'json.dumps(indent=2, ensure_ascii=False) + LF')

    def test_schema_fields(self):
        m = self.model
        self.assertEqual(m['schema'], 'edgefhe.affine-research.v1')
        self.assertEqual(m['model_id'], 'cdc-diabetes-lr4-v1')
        self.assertEqual(m['features'], ['GenHlth', 'HighBP', 'BMI', 'Age'])
        self.assertEqual(m['operation'], 'public-scalar-affine')
        self.assertIs(type(m['input_abs_max']), int)
        self.assertEqual(m['input_abs_max'], 8)
        self.assertEqual(m['output_policy'], 'owner_only_no_cloud_feedback')
        self.assertIs(m['deployment_approved'], False)

    def test_integer_weights(self):
        m = self.model
        self.assertEqual(m['weight_scale'], 1 << 30)
        self.assertEqual(m['output_scale'], 1 << 60)
        self.assertEqual(m['weights_int'], W_INT)
        self.assertEqual(m['bias_int'], B_INT)
        self.assertEqual(m['weights_int'], [round(w * (1 << 30)) for w in m['weights']])  # round-half-even
        self.assertEqual(m['bias_int'], round(m['bias'] * (1 << 60)))
        for w, wi in zip(m['weights'], m['weights_int']):
            self.assertLessEqual(abs(w * (1 << 30) - wi), 0.5)
        bound = 35184372486187 * sum(abs(v) for v in m['weights_int']) + abs(m['bias_int'])
        self.assertEqual(m['no_wrap_coefficient_bound'], str(bound))

    def test_doubles_equal_training_outputs(self):
        out = ROOT / 'training' / 'outputs'
        v4 = json.loads((out / 'model.json').read_text(encoding='utf-8'))
        edge = json.loads((out / 'edge_preprocess.json').read_text(encoding='utf-8'))
        self.assertEqual(self.model['weights'], v4['weights'])
        self.assertEqual(self.model['bias'], v4['bias'])
        self.assertEqual(self.model['mean'], edge['mean'])
        self.assertEqual(self.model['std'], edge['std'])
        self.assertEqual(self.model['features'], edge['features'])


class CatalogueLine(unittest.TestCase):
    def setUp(self):
        self.bytes = raw('catalog_line.hpp')
        self.model = load('cdc-diabetes-lr4-v1.json')

    def test_single_line(self):
        self.assertTrue(self.bytes.endswith(b'},\n'))
        self.assertEqual(self.bytes.count(b'\n'), 1)
        self.assertNotIn(b'\r', self.bytes)

    def test_parses_back_to_the_json_doubles(self):
        found = LINE.fullmatch(self.bytes.decode('ascii').rstrip('\n'))
        self.assertIsNotNone(found, 'Model{...} layout of models.hpp')
        m = self.model
        self.assertEqual(found['id'], m['model_id'])
        self.assertEqual(found['sha'], sha(raw('cdc-diabetes-lr4-v1.json')))
        self.assertEqual([json.loads(s) for s in found['features'].split(',')], m['features'])
        for group, key in (('mean', 'mean'), ('sd', 'std'), ('weights', 'weights')):
            literals = found[group].split(',')
            for literal in literals:
                self.assertRegex(literal, HEX_DOUBLE)
            self.assertEqual([float.fromhex(s) for s in literals], m[key], group)
        self.assertRegex(found['bias'], HEX_DOUBLE)
        self.assertEqual(float.fromhex(found['bias']), m['bias'])
        integers = found['wint'].split(',')
        self.assertTrue(all(s.endswith('LL') for s in integers))
        self.assertEqual([int(s[:-2]) for s in integers], m['weights_int'])
        self.assertEqual(int(found['bint']), m['bias_int'])
        self.assertEqual((found['uci'], found['dictionary']), ('0', '0'))

    def test_staged_catalogue(self):
        before = (V8 / CATALOGUE).read_bytes()
        self.assertEqual(sha(before), CATALOGUE_BEFORE)
        self.assertTrue(before.endswith(b'};}\n'))
        after = before[:-4] + self.bytes + b'};}\n'
        self.assertEqual(sha(after), CATALOGUE_AFTER)
        old = [l for l in before.decode('ascii').splitlines() if l.startswith('Model{')]
        new = [l for l in after.decode('ascii').splitlines() if l.startswith('Model{')]
        self.assertEqual(new[:3], old, 'catalogue()[0] (probe.cpp) and [1] (wire_selftest.cpp) keep their entries')
        self.assertEqual(new[3] + '\n', self.bytes.decode('ascii'))


class Expected(unittest.TestCase):
    def test_overlay_hashes(self):
        e = load('EXPECTED.json')
        self.assertEqual(dump(e), raw('EXPECTED.json'))
        self.assertEqual(e['model_json'], MODEL_SHA)
        self.assertEqual(e['model_json_bytes'], 2247)
        self.assertEqual(e['catalog_line'], sha(raw('catalog_line.hpp')))
        self.assertEqual(e['catalogue_before'], CATALOGUE_BEFORE)
        self.assertEqual(e['catalogue_after'], CATALOGUE_AFTER)
        self.assertEqual(e['profile'], PROFILE)
        self.assertEqual(e['profile_id_hpp'], PROFILE_ID_HPP)
        pid = (V8 / 'software' / 'client' / 'profile_id.hpp').read_bytes()
        self.assertEqual(sha(pid), PROFILE_ID_HPP)
        self.assertEqual(re.findall(rb'profile_hex="([0-9a-f]{64})"', pid), [PROFILE.encode('ascii')])
        self.assertEqual((e['catalogue_index'], e['model_json_staged_path']), (3, STAGED_MODEL))
        self.assertEqual((e['changed'], e['added']), ([CATALOGUE], [STAGED_MODEL]))

    def test_staged_tree_id(self):
        """staged_tree_id = SHA-256 of what the staged tools/update_sums.py writes (sorted paths, LF)."""
        e = load('EXPECTED.json')
        sums = (V8 / 'SHA256SUMS').read_bytes()
        self.assertEqual(sha(sums), e['base_sums'])
        entries = dict(line.split('  ', 1)[::-1] for line in sums.decode('utf-8').splitlines() if line)
        self.assertEqual(len(entries), e['base_tree_files'])
        self.assertNotIn(STAGED_MODEL, entries)
        self.assertFalse((V8 / STAGED_MODEL).exists(), 'v8b_rev5 itself never receives the CDC model')
        entries[CATALOGUE] = CATALOGUE_AFTER
        entries[STAGED_MODEL] = MODEL_SHA
        text = ''.join('%s  %s\n' % (entries[name], name) for name in sorted(entries)).encode('utf-8')
        self.assertEqual(len(entries), e['staged_tree_files'])
        self.assertEqual(sha(text), e['staged_tree_id'])


class FormSchema(unittest.TestCase):
    def setUp(self):
        self.form = load('form_cdc.json')
        self.model = load('cdc-diabetes-lr4-v1.json')
        self.fields = {f['name']: f for f in self.form['fields']}

    def test_serialization_and_model_block(self):
        self.assertEqual(dump(self.form), raw('form_cdc.json'))
        block = self.form['model']
        self.assertEqual(block['sha256'], MODEL_SHA)
        self.assertEqual(block['file'], 'cdc-diabetes-lr4-v1.json')
        self.assertEqual([f['name'] for f in self.form['fields']], self.model['features'])
        self.assertEqual(block['csv_header'], 'GenHlth,HighBP,BMI,Age')

    def test_domains(self):
        options = lambda name: [(o['value'], o['label']) for o in self.fields[name]['options']]
        self.assertEqual(options('GenHlth'), [(1, '極好'), (2, '很好'), (3, '好'), (4, '普通'), (5, '很差')])
        self.assertEqual(options('HighBP'), [(0, '否'), (1, '是')])
        bmi = self.fields['BMI']
        self.assertEqual((bmi['kind'], bmi['minimum'], bmi['maximum'], bmi['decimals']), ('number', 12.0, 81.0, 1))
        age = self.fields['Age']['options']
        self.assertEqual([o['value'] for o in age], list(range(1, 14)))
        self.assertEqual((age[0]['label'], age[1]['label'], age[11]['label'], age[12]['label']),
                         ('18–24 歲', '25–29 歲', '75–79 歲', '80 歲以上'))
        self.assertEqual((age[0]['years_min'], age[12]['years_min'], age[12]['years_max']), (18, 80, None))
        for a, b in zip(age, age[1:]):
            self.assertEqual(a['years_max'] + 1, b['years_min'], 'bands are contiguous')
        for o in age[1:12]:
            self.assertEqual(o['label'], '%d–%d 歲' % (o['years_min'], o['years_max']))
            self.assertEqual(o['years_max'] - o['years_min'], 4)
        refused = self.fields['Age']['refused_options']
        self.assertEqual([(o['value'], o['code'], o['years_max']) for o in refused], [(0, 'AGE_UNDER_18', 17)])

    def test_bmi_normalization_bound(self):
        """|z| <= 8 holds exactly for BMI <= 81.1686 (four decimals); the form stops at 81.0."""
        m = self.model
        self.assertEqual(self.fields['BMI']['client_normalization_max'], 81.1686)
        self.assertAlmostEqual(m['mean'][2] + 8 * m['std'][2], 81.16862, places=5)
        x = mid_values(m)
        for bmi, accepted in ((81.1686, True), (81.1687, False), (81.0, True), (82.0, False), (12.0, True)):
            x[2] = bmi
            self.assertEqual(client_accepts(m, x), accepted, bmi)
        self.assertLessEqual(self.fields['BMI']['maximum'], self.fields['BMI']['client_normalization_max'])

    def test_every_offered_value_normalizes(self):
        m = self.model
        for f, name in enumerate(m['features']):
            spec = self.fields[name]
            values = [o['value'] for o in spec['options']] if spec['kind'] == 'choice' else [spec['minimum'], spec['maximum']]
            for v in values:
                x = mid_values(m)
                x[f] = float(v)
                self.assertTrue(client_accepts(m, x), (name, v))

    def test_presets_match_the_plan(self):
        presets = self.form['presets']
        self.assertEqual([p['id'] for p in presets], [row[0] for row in PLAN])
        band = self.form['levels']['band_abs_logit']
        edges = self.form['levels']['edges_logit']
        for preset, (pid, values, logit6, outcome) in zip(presets, PLAN):
            form_values = tuple(preset['form'][n] for n in self.model['features'])
            self.assertEqual(form_values, values, pid)
            x = [float(v) for v in values]
            expected = preset['expected']
            self.assertEqual(expected['max_abs_z'], max(abs((x[f] - self.model['mean'][f]) / self.model['std'][f]) for f in range(4)))
            if outcome == 'refused':
                self.assertIsNone(expected['logit_plain'])
                continue
            logit = client_logit(self.model, x)
            self.assertLessEqual(abs(logit - logit6), 1e-6, pid)
            self.assertEqual(expected['logit_plain'], logit, pid)
            self.assertIsNone(expected['refused'], pid)
            self.assertTrue(client_accepts(self.model, x), pid)
            if outcome == 'boundary':
                self.assertIsNone(expected['level'], pid)
                self.assertEqual(expected['boundary'], [2, 3], pid)
                self.assertLessEqual(abs(logit - edges[1]), band, pid)
            else:
                self.assertEqual(expected['level'], outcome, pid)
                self.assertIsNone(expected['boundary'], pid)
                self.assertGreater(min(abs(logit - e) for e in edges), band, pid)
                self.assertEqual(1 + sum(1 for e in edges if logit > e), outcome, pid)

    def test_j_is_refused(self):
        j = [p for p in self.form['presets'] if p['id'] == 'J'][0]
        bmi = self.fields['BMI']
        self.assertGreater(j['form']['BMI'], bmi['maximum'])
        self.assertEqual(j['expected']['refused'], bmi['refuse_above'])
        self.assertEqual(j['expected']['refused'], 'BMI_ABOVE_MAX')
        self.assertIsNone(j['expected']['level'])
        x = [float(j['form'][n]) for n in self.model['features']]
        z_bmi = (x[2] - self.model['mean'][2]) / self.model['std'][2]
        self.assertAlmostEqual(z_bmi, 8.126, places=3)
        self.assertEqual(j['expected']['max_abs_z'], z_bmi)
        self.assertFalse(client_accepts(self.model, x), 'the client refuses J as well (NORMALIZATION_REFUSED)')

    def test_level_edges_and_band(self):
        levels = self.form['levels']
        self.assertEqual(levels['space'], 'logit')
        self.assertEqual(levels['edges_p_balanced'], [0.3, 0.5, 0.7])
        edges = levels['edges_logit']
        for p, e in zip(levels['edges_p_balanced'], edges):
            self.assertAlmostEqual(e, math.log(p / (1 - p)), places=15)
        self.assertEqual(edges[1], 0.0)
        self.assertEqual(edges[0], -edges[2])
        self.assertAlmostEqual(edges[2], 0.8473, places=4)
        self.assertEqual(levels['band_abs_logit'], 0.002)
        self.assertEqual(self.form['verification']['max_abs_error'], 0.002)
        items = levels['items']
        self.assertEqual([i['code'] for i in items], ['L1', 'L2', 'L3', 'L4'])
        self.assertEqual([i['logit_lower'] for i in items], [None] + edges)
        self.assertEqual([i['logit_upper'] for i in items], edges + [None])

    def test_levels_never_show_a_probability(self):
        levels = self.form['levels']
        texts = [levels['boundary_text'], levels['observed_caption']]
        texts += [i['name'] + i['label'] for i in levels['items']]
        texts += [p['label'] + p['summary'] for p in self.form['presets']]
        for text in texts:
            self.assertNotIn('機率', text)
            self.assertNotIn('%', text)
            self.assertNotIn('％', text)
        self.assertIn('{lower}', levels['boundary_text'])
        self.assertIn('{upper}', levels['boundary_text'])
        self.assertIn('不判定', levels['boundary_text'])
        notice = ''.join(self.form['notice'])
        for phrase in ('研究', '並非醫療診斷', '虛構', '不顯示機率'):
            self.assertIn(phrase, notice)

    def test_refusal_codes_and_messages(self):
        refusals = self.form['refusals']
        codes = {self.form['refuse_fields']}
        for f in self.form['fields']:
            codes.update(f[k] for k in ('refuse_invalid', 'refuse_below', 'refuse_above') if k in f)
            codes.update(o['code'] for o in f.get('refused_options', []))
        self.assertEqual(codes, set(refusals))
        self.assertIn('81.0', refusals['BMI_ABOVE_MAX'])
        self.assertIn('12.0', refusals['BMI_BELOW_MIN'])
        self.assertIn('18', refusals['AGE_UNDER_18'])

    def test_observed_table_is_consistent(self):
        levels = self.form['levels']
        source = levels['observed_source']
        items = levels['items']
        manifest = json.loads((ROOT / 'data' / 'cdc_diabetes' / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(source['file_sha256'], manifest['splits']['test']['sha256'])
        self.assertEqual(source['rows_total'], manifest['splits']['test']['rows'])
        self.assertEqual(source['edges_p_balanced'], levels['edges_p_balanced'])
        self.assertEqual(source['rows_total'], source['rows_evaluated'] + source['rows_refused_abs_z_above_8'])
        self.assertEqual(sum(i['observed_rows'] for i in items), source['rows_evaluated'])
        self.assertEqual(sum(i['observed_positives'] for i in items), source['positives_evaluated'])
        for i in items:
            self.assertEqual(i['observed_share'], round(i['observed_rows'] / source['rows_evaluated'], 4))
            self.assertEqual(i['observed_rate'], round(i['observed_positives'] / i['observed_rows'], 4))
        rates = [i['observed_rate'] for i in items]
        self.assertEqual(rates, sorted(rates), 'a higher level has a higher observed rate')

    @unittest.skipUnless(SPLIT.is_file(), 'local test split not present (untracked; see data/README.md)')
    def test_level_table_recomputes_from_the_local_split(self):
        data = SPLIT.read_bytes()
        source = self.form['levels']['observed_source']
        self.assertEqual(sha(data), source['file_sha256'])
        edges = self.form['levels']['edges_logit']
        band = self.form['levels']['band_abs_logit']
        rows, positives, refused, in_band = [0, 0, 0, 0], [0, 0, 0, 0], 0, 0
        for record in csv.DictReader(io.StringIO(data.decode('utf-8'))):
            x = [float(record[n]) for n in self.model['features']]
            if not client_accepts(self.model, x):
                refused += 1
                continue
            logit = client_logit(self.model, x)
            k = sum(1 for e in edges if logit > e)
            rows[k] += 1
            positives[k] += int(record['Diabetes_binary'])
            in_band += any(abs(logit - e) <= band for e in edges)
        items = self.form['levels']['items']
        self.assertEqual(rows, [i['observed_rows'] for i in items])
        self.assertEqual(positives, [i['observed_positives'] for i in items])
        self.assertEqual((refused, in_band), (source['rows_refused_abs_z_above_8'], source['rows_in_band']))


def load_converter():
    spec = importlib.util.spec_from_file_location('convert_cdc_to_v8b', str(CONVERTER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReferenceValidation(unittest.TestCase):
    """The form check that derives expected.refused of every preset (the gateway must agree with it)."""

    def test_refusal_codes(self):
        refusal = load_converter().refusal
        form = load('form_cdc.json')
        base = {'GenHlth': 3, 'HighBP': 1, 'BMI': 31.2, 'Age': 9}
        cases = [
            ({}, None), ({'BMI': 12}, None), ({'BMI': 81.0}, None), ({'Age': 13}, None),
            ({'Age': 0}, 'AGE_UNDER_18'), ({'Age': 14}, 'AGE_VALUE'), ({'Age': 9.0}, 'AGE_VALUE'),
            ({'GenHlth': 0}, 'GENHLTH_VALUE'), ({'GenHlth': True}, 'GENHLTH_VALUE'), ({'HighBP': 2}, 'HIGHBP_VALUE'),
            ({'BMI': '31.2'}, 'BMI_FORMAT'), ({'BMI': 31.25}, 'BMI_FORMAT'), ({'BMI': float('nan')}, 'BMI_FORMAT'),
            ({'BMI': True}, 'BMI_FORMAT'), ({'BMI': 11.9}, 'BMI_BELOW_MIN'), ({'BMI': 81.1}, 'BMI_ABOVE_MAX'),
            ({'BMI': 82.0}, 'BMI_ABOVE_MAX'), ({'Smoker': 0}, 'FORM_FIELDS'),
        ]
        for change, code in cases:
            values = dict(base)
            values.update(change)
            self.assertEqual(refusal(form, values), code, change)
        missing = dict(base)
        del missing['Age']
        self.assertEqual(refusal(form, missing), 'FORM_FIELDS')
        self.assertEqual(refusal(form, [3, 1, 31.2, 9]), 'FORM_FIELDS')


class Converter(unittest.TestCase):
    def run_converter(self, *args):
        return subprocess.run([sys.executable, '-I', '-B', str(CONVERTER), '--repo', str(ROOT)] + list(args),
                              capture_output=True, text=True, cwd=str(ROOT))

    def test_check_passes(self):
        r = self.run_converter('--check')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('CDC_MODEL_CHECK_PASS model=%s catalogue_after=%s profile=%s' % (MODEL_SHA, CATALOGUE_AFTER, PROFILE), r.stdout)

    def test_out_reproduces_the_committed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_converter('--out', tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            for name in ('cdc-diabetes-lr4-v1.json', 'catalog_line.hpp', 'EXPECTED.json', 'form_cdc.json'):
                self.assertEqual((Path(tmp) / name).read_bytes(), raw(name), name)

    def test_out_refuses_the_repository(self):
        target = HERE / 'out_must_not_exist'
        r = self.run_converter('--out', str(target))
        self.assertEqual(r.returncode, 1)
        self.assertIn('CDC_MODEL_FAIL', r.stdout)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=2))
