"""CDC form rules, CSV text, plaintext reference logit, stable sigmoid and risk levels (standard library only).

Every rule comes from demo/model/form_cdc.json and the frozen model JSON, whose SHA-256 is the wire model_id, so the
gateway decides exactly like demo/model/convert_cdc_to_v8b.py, which derived the presets (refusal, reference_logit,
classify there). The reference logit repeats the client's own arithmetic and order (client.cpp:276):
bias + sum over the features of weights[f] * ((x[f] - mean[f]) / std[f]), with the model JSON doubles, which equal the
catalogue hex doubles bit for bit. A level is decided on the CKKS logit; within band_abs_logit of an edge the result is
the boundary between the two adjacent levels and no level is shown. The owner screen shows a level, never a
probability: p_balanced (sigmoid of a class-balanced fit) is owner-only technical detail.
"""
import hashlib
import json
import math

MODEL_ID = 'cdc-diabetes-lr4-v1'
MODEL_SHA256 = '63cee36f9ac68b8e274dc45b4fa2fee0aeae5c4089b3d18fe51d7cf0c588c106'
MODEL_SCHEMA = 'edgefhe.affine-research.v1'
FORM_SCHEMA = 'edgefhe.demo.form.v1'
HEADER_BYTES = 256          # wire.hpp:5
CT_BYTES = 6 * 4096 * 4     # one ciphertext: 2 polynomials x 3 RNS limbs x 4096 words of 4 bytes (wire.hpp:5)


class ModelError(Exception):
    pass


def _need(condition, message):
    if not condition:
        raise ModelError(message)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def sigmoid(x):
    """1 / (1 + e^-x) without overflow for any finite x."""
    if not math.isfinite(x):
        raise ValueError('sigmoid of a non-finite value')
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def classify(logit, edges, band):
    """(level, boundary) exactly as convert_cdc_to_v8b.classify: within band of edge i -> boundary [i+1, i+2] and no
    level; otherwise level = 1 + number of edges below the logit."""
    for i, edge in enumerate(edges):
        if abs(logit - edge) <= band:
            return None, [i + 1, i + 2]
    return 1 + sum(1 for edge in edges if logit > edge), None


class Inference:
    """The frozen CDC model plus the form schema; immutable after load()."""

    def __init__(self, model, form, model_sha256):
        self.model = model
        self.form = form
        self.model_sha256 = model_sha256
        self.features = list(model['features'])
        self.fields = list(form['fields'])
        self.mean = list(model['mean'])
        self.std = list(model['std'])
        self.weights = list(model['weights'])
        self.bias = model['bias']
        levels = form['levels']
        self.edges = list(levels['edges_logit'])
        self.band = levels['band_abs_logit']
        self.items = list(levels['items'])
        self.max_abs_error = form['verification']['max_abs_error']
        self._check()

    @classmethod
    def load(cls, model_path, form_path):
        with open(model_path, 'rb') as f:
            model_bytes = f.read()
        with open(form_path, 'rb') as f:
            form_bytes = f.read()
        digest = hashlib.sha256(model_bytes).hexdigest()
        _need(digest == MODEL_SHA256, 'model JSON SHA-256 %s is not the catalogue model %s' % (digest, MODEL_SHA256))
        try:
            model = json.loads(model_bytes.decode('utf-8'))
            form = json.loads(form_bytes.decode('utf-8'))
        except ValueError as e:
            raise ModelError('model or form JSON: %s' % e)
        return cls(model, form, digest)

    def _check(self):
        m, form = self.model, self.form
        f_count = len(self.features)
        _need(m.get('schema') == MODEL_SCHEMA and m.get('model_id') == MODEL_ID, 'model schema or model_id')
        _need(form.get('schema') == FORM_SCHEMA, 'form schema')
        block = form['model']
        _need(block['model_id'] == MODEL_ID and block['sha256'] == self.model_sha256, 'form model block')
        _need(block['features'] == self.features == [f['name'] for f in self.fields], 'feature order')
        _need(block['csv_header'] == ','.join(self.features), 'csv_header')
        _need(len(self.mean) == len(self.std) == len(self.weights) == f_count == 4, 'model dimensions')
        _need(all(_finite(v) for v in self.mean + self.weights + [self.bias]), 'model doubles')
        _need(all(_finite(v) and v > 0 for v in self.std), 'model std')
        _need(all(_finite(e) for e in self.edges) and self.edges == sorted(set(self.edges)), 'level edges')
        _need(_finite(self.band) and self.band >= 0, 'band')
        _need([i['level'] for i in self.items] == list(range(1, len(self.edges) + 2)), 'level items')
        _need(form['levels'].get('decide_on') == 'logit_ckks', 'levels decide on the CKKS logit')
        _need(_finite(self.max_abs_error) and self.max_abs_error > 0, 'verification bound')
        codes = {form['refuse_fields']}
        for f in self.fields:
            _need(f['kind'] in ('choice', 'number'), 'field kind')
            codes.update(f[k] for k in ('refuse_invalid', 'refuse_below', 'refuse_above') if k in f)
            codes.update(o['code'] for o in f.get('refused_options', []))
        _need(codes <= set(form['refusals']), 'every refusal code has a message')

    # ------------------------------------------------------------------------------------------- validation
    def refusal(self, values):
        """The refusal code, or None when the form is accepted (same rules as convert_cdc_to_v8b.refusal)."""
        names = [f['name'] for f in self.fields]
        if (not isinstance(values, dict) or not all(type(k) is str for k in values)
                or sorted(values) != sorted(names)):
            return self.form['refuse_fields']
        for f in self.fields:
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
                if type(v) is int:
                    finite = True
                elif type(v) is float:
                    finite = math.isfinite(v)
                else:
                    return f['refuse_invalid']
                if not finite or round(v, f['decimals']) != v:
                    return f['refuse_invalid']
                if v < f['minimum']:
                    return f['refuse_below']
                if v > f['maximum']:
                    return f['refuse_above']
        return None

    def refusal_message(self, code):
        return self.form['refusals'].get(code, code)

    def refusal_field(self, code):
        for f in self.fields:
            if code in (f.get('refuse_invalid'), f.get('refuse_below'), f.get('refuse_above')):
                return f['name']
            if code in [o['code'] for o in f.get('refused_options', [])]:
                return f['name']
        return None

    # ---------------------------------------------------------------------------------------------- encoding
    def canonical(self, values):
        """Accepted form values in model order: int for choices, float for numbers."""
        _need(self.refusal(values) is None, 'canonical() needs an accepted form')
        out = {}
        for f in self.fields:
            v = values[f['name']]
            out[f['name']] = int(v) if f['kind'] == 'choice' else float(v)
        return out

    def canonical_json(self, values):
        """The committed text: sorted keys, no spaces, ASCII (records.commitment_of)."""
        return json.dumps(self.canonical(values), sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                          allow_nan=False)

    def csv_text(self, values):
        """client.cpp:39-55: the header is exactly the model features, LF line ends, one row of from_chars numbers."""
        canonical = self.canonical(values)
        row = []
        for f in self.fields:
            v = canonical[f['name']]
            row.append('%d' % v if f['kind'] == 'choice' else '%.*f' % (f['decimals'], v))
        return self.form['model']['csv_header'] + '\n' + ','.join(row) + '\n'

    def csv_values(self, text):
        """The doubles that the client parses from csv_text (std::from_chars and float() both round correctly)."""
        lines = text.split('\n')
        _need(len(lines) == 3 and lines[0] == ','.join(self.features) and lines[2] == '', 'csv layout')
        fields = lines[1].split(',')
        _need(len(fields) == len(self.features), 'csv dimensions')
        return [float(s) for s in fields]

    # -------------------------------------------------------------------------------------------- arithmetic
    def z_values(self, x):
        return [(x[f] - self.mean[f]) / self.std[f] for f in range(len(self.features))]

    def contributions(self, x):
        """weights[f] * z[f], the terms that the client adds to the bias (client.cpp:276)."""
        return [self.weights[f] * ((x[f] - self.mean[f]) / self.std[f]) for f in range(len(self.features))]

    def reference(self, x):
        """Plaintext logit in the client's order and double precision (client.cpp:276)."""
        acc = self.bias
        for f in range(len(self.features)):
            acc += self.weights[f] * ((x[f] - self.mean[f]) / self.std[f])
        return acc

    def classify(self, logit):
        return classify(logit, self.edges, self.band)

    def level_item(self, level):
        for item in self.items:
            if item['level'] == level:
                return item
        return None

    def boundary_text(self, boundary):
        return self.form['levels']['boundary_text'].format(lower=boundary[0], upper=boundary[1])

    def evaluate(self, logit_ckks, logit_plain):
        """The owner-only result of one form: verification, level or boundary, and the display fields."""
        abs_error = abs(logit_ckks - logit_plain)
        passed = math.isfinite(abs_error) and abs_error <= self.max_abs_error
        level = boundary = None
        if passed:
            level, boundary = self.classify(logit_ckks)
        plain_level, plain_boundary = self.classify(logit_plain)
        item = self.level_item(level) if level is not None else None
        levels = self.form['levels']
        return {
            'logit_ckks': logit_ckks,
            'logit_plain': logit_plain,
            'abs_error': abs_error,
            'max_abs_error': self.max_abs_error,
            'passed': passed,
            'p_balanced': sigmoid(logit_ckks),
            'level': level,
            'level_code': item['code'] if item else None,
            'level_name': item['name'] if item else None,
            'level_label': item['label'] if item else None,
            'observed_rate': item['observed_rate'] if item else None,
            'observed_caption': levels['observed_caption'] if item else None,
            'boundary': boundary,
            'boundary_text': self.boundary_text(boundary) if boundary else None,
            'plain_level': plain_level,
            'plain_boundary': plain_boundary,
            'message': None if passed else self.form['verification']['failed_text'],
        }

    def frame_bytes(self):
        """(request, response) frame bytes: 256-byte header plus 4 ciphertexts up, 1 down (wire.hpp:5, 29)."""
        return HEADER_BYTES + len(self.features) * CT_BYTES, HEADER_BYTES + CT_BYTES

    def presets(self):
        return list(self.form.get('presets', []))
