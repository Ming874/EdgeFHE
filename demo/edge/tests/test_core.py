"""Gateway core tests (Python 3.10 standard library only; the run tests need root on Linux, i.e. a container).

usage (PowerShell, repository root):
  docker run --rm -v "${PWD}:/repo" -w /repo python:3.10-slim python -B -m unittest discover -s demo/edge/tests -v

Pure checks (any OS): the Python contracts equal contract_vectors.json, which contract_vector.cpp prints from the
real invoke_permit.hpp; the fake client's own contract port and permit check give the header's verdicts; the form
rules equal demo/model/convert_cdc_to_v8b.refusal; every preset reproduces its expected logit (bit for bit), level
and boundary; CSV text, stable sigmoid, /proc/interrupts parsing, configuration rules, event log and public record
whitelist. Run checks (root, Linux): complete runs through the command line with tests/fake_efnet_client.py:
permit accepted for the matching contract, PL_PERMIT_INVOCATION_BINDING for a flipped contract byte,
PL_PERMIT_IDENTITY for mode 0644, the refusal of J starts no process, the CSV and score are overwritten and
unlinked, a STOP code is reported once (one sentence, then the fixed no-retry line) and never retried, the watchdog
reports without killing, and SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1 and SIGUSR2 wait for the client. Also:
retain_private defaults to false and the wiped event says whether a private record was kept; the command line prints
public events only unless --owner-events; the run lock is one fixed path; DMA holders are found by device number or
inode; a stop request during admission starts no run; a reader thread that cannot start never skips the wait; a
rehearsal gateway refuses the pl backend once a board marker exists. Without --init the test runner is PID 1, so every
permit check runs below a child process: the gateway command line, or this file in 'controller' mode.
"""
import collections
import errno
import hashlib
import importlib.util
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = os.path.dirname(HERE)
DEMO = os.path.dirname(EDGE)
ROOT = os.path.dirname(DEMO)
MODEL_JSON = os.path.join(DEMO, 'model', 'cdc-diabetes-lr4-v1.json')
FORM_JSON = os.path.join(DEMO, 'model', 'form_cdc.json')
CONVERTER = os.path.join(DEMO, 'model', 'convert_cdc_to_v8b.py')
GATEWAY = os.path.join(EDGE, 'edgefhe_gateway.py')
FAKE = os.path.join(HERE, 'fake_efnet_client.py')
VECTORS = os.path.join(HERE, 'contract_vectors.json')
EXAMPLE = os.path.join(EDGE, 'gateway.example.json')
PROFILE_ID_HPP = os.path.join(ROOT, 'v8b_rev5', 'software', 'client', 'profile_id.hpp')

sys.dont_write_bytecode = True
if EDGE not in sys.path:
    sys.path.insert(0, EDGE)
from efgw import config, model, permit, records, runner  # noqa: E402

PROFILE = 'c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c'
PEER = hashlib.sha256(b'EdgeFHE gateway tests: example server certificate').hexdigest()
POSIX = os.name == 'posix'
LINUX_ROOT = sys.platform.startswith('linux') and os.geteuid() == 0
SHM = '/dev/shm' if os.path.isdir('/dev/shm') else tempfile.gettempdir()
M = {'GenHlth': 3, 'HighBP': 1, 'BMI': 31.2, 'Age': 9}
K = {'GenHlth': 4, 'HighBP': 1, 'BMI': 26.0, 'Age': 4}
J = {'GenHlth': 3, 'HighBP': 0, 'BMI': 82.0, 'Age': 7}
INTERRUPTS = (
    '           CPU0       CPU1       CPU2       CPU3       \n'
    ' 11:     565926     840735     571241     580342     GICv2  30 Level     arch_timer\n'
    ' 19:          0          0          0          0     GICv2 156 Level     zynqmp-dma\n'
    ' 55:      45610          0          0          0     GICv2 154 Level     fd4c0000.dma-controller\n'
    ' 61: %10d          1          0          0     GICv2 121 Level     xilinx-dma-controller\n'
    ' 62: %10d          0          1          0     GICv2 136 Level     xilinx-dma-controller\n'
    'IPI0:      1234       5678       9012       3456       Rescheduling interrupts\n'
    'Err:          0\n')


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake = load_module('fake_efnet_client', FAKE)
gateway_cli = load_module('edgefhe_gateway_cli', GATEWAY)


def events_of(stdout):
    return [json.loads(line[4:]) for line in stdout.splitlines() if line.startswith('EVT ')]


def marker(stdout, prefix):
    for line in stdout.splitlines():
        if line.startswith(prefix + ' ') or line == prefix:
            return line
    return None


def fields_of(line):
    return dict(item.split('=', 1) for item in line.split()[1:] if '=' in item)


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ------------------------------------------------------------------------------------------------- the bench
class Bench:
    """A temporary board: the fake client as an executable, a tmpfs work directory, placeholder PKI files, a fake
    /proc/interrupts with the two DMA lines, a fake DMA node and a gateway.json that points at all of them."""

    def __init__(self, **behavior):
        self.base = tempfile.mkdtemp(prefix='efgw-', dir=SHM)                     # tmpfs, noexec
        self.bin = tempfile.mkdtemp(prefix='efgw-bin-', dir=tempfile.gettempdir())  # executable files
        self.client = os.path.join(self.bin, 'efnet_client')
        with open(FAKE, encoding='utf-8') as f:
            body = f.read().split('\n', 1)[1]
        with open(self.client, 'w', encoding='utf-8') as f:
            f.write('#!%s -IB\n%s' % (sys.executable, body))
        os.chmod(self.client, 0o755)
        self.app_dir = os.path.join(self.bin, 'app')
        os.mkdir(self.app_dir)
        for name in ('libef_app_owner.so', 'libef_app_normalize.so'):
            with open(os.path.join(self.app_dir, name), 'wb') as f:
                f.write(b'placeholder library\n')
        self.pki = os.path.join(self.base, 'pki')
        os.mkdir(self.pki, 0o700)
        for name in ('ca.pem', 'client.pem', 'client.key'):
            with open(os.path.join(self.pki, name), 'w', encoding='ascii') as f:
                f.write('placeholder\n')
        os.chmod(os.path.join(self.pki, 'client.key'), 0o600)
        self.workdir = os.path.join(self.base, 'run')
        self.interrupts = os.path.join(self.base, 'interrupts')
        self.set_interrupts(INTERRUPTS % (5, 5))
        self.dma_node = os.path.join(self.base, 'edgefhe_v8b_dma')
        open(self.dma_node, 'w').close()
        self.observe_dir = os.path.join(self.base, 'observe')
        os.mkdir(self.observe_dir)
        self.spawn_log = os.path.join(self.base, 'spawns.log')
        self.signal_log = os.path.join(self.base, 'signals.log')
        self.plugin = os.path.join(self.base, 'plugin.json')
        self.public_log = os.path.join(self.base, 'log', 'public.jsonl')
        self.disclosure_dir = os.path.join(self.base, 'disclosure')
        self.behavior = {'model_json': MODEL_JSON, 'dma_node': self.dma_node, 'interrupts': self.interrupts,
                         'spawn_log': self.spawn_log, 'signal_log': self.signal_log, 'observe_dir': self.observe_dir}
        self.behave(**behavior)
        self.cfg_path = os.path.join(self.base, 'gateway.json')
        self.cfg = {
            'schema': config.SCHEMA, 'client': self.client, 'plugin': self.plugin, 'app_dir': self.app_dir,
            'profile_hex': PROFILE, 'host': '127.0.0.1', 'port': 18443, 'timeout_ms': 1000, 'workers': 3,
            'ca': os.path.join(self.pki, 'ca.pem'), 'cert': os.path.join(self.pki, 'client.pem'),
            'key': os.path.join(self.pki, 'client.key'), 'peer_name': 'edgefhe-cloud', 'peer_sha256': PEER,
            'workdir': self.workdir, 'model_json': MODEL_JSON, 'form_json': FORM_JSON, 'public_log': self.public_log,
            'disclosure_dir': self.disclosure_dir, 'proc_interrupts': self.interrupts, 'dma_node': self.dma_node,
        }
        self.write_config()

    def set_interrupts(self, text):
        with open(self.interrupts, 'w', encoding='ascii') as f:
            f.write(text)

    def behave(self, **changes):
        self.behavior.update(changes)
        with open(self.plugin, 'w', encoding='utf-8') as f:
            json.dump(self.behavior, f)

    def write_config(self, **changes):
        self.cfg.update(changes)
        with open(self.cfg_path, 'w', encoding='utf-8') as f:
            json.dump({k: v for k, v in self.cfg.items() if v is not None}, f)
        os.chmod(self.cfg_path, 0o600)

    def load(self):
        return config.load(self.cfg_path)

    def run(self, *args, timeout=120):
        command = [sys.executable, '-I', '-B', GATEWAY, args[0], '--config', self.cfg_path] + list(args[1:])
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)

    def submit(self, form, *extra, owner_events=True):
        """The command line 'submit'; with owner_events (the tests' default) the owner-class EVT lines as well."""
        return self.run('submit', '--genhlth', str(form['GenHlth']), '--highbp', str(form['HighBP']),
                        '--bmi', str(form['BMI']), '--age', str(form['Age']), *extra,
                        *(('--owner-events',) if owner_events else ()))

    def spawns(self):
        if not os.path.exists(self.spawn_log):
            return []
        with open(self.spawn_log, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def signals(self):
        if not os.path.exists(self.signal_log):
            return []
        with open(self.signal_log, encoding='utf-8') as f:
            return f.read().splitlines()

    def observed(self):
        """{'<client pid>.in.csv' or '<client pid>.score.txt': bytes} of the hard links the fake client left."""
        out = {}
        for name in sorted(os.listdir(self.observe_dir)):
            with open(os.path.join(self.observe_dir, name), 'rb') as f:
                out[name] = f.read()
        return out

    def public_records(self):
        if not os.path.exists(self.public_log):
            return []
        with open(self.public_log, encoding='ascii') as f:
            return [json.loads(line) for line in f if line.strip()]

    def close(self):
        shutil.rmtree(self.base, ignore_errors=True)
        shutil.rmtree(self.bin, ignore_errors=True)


# ------------------------------------------------------------------------------------------ pure: contracts
class ContractVectors(unittest.TestCase):
    """Python contracts == contract_vectors.json == the real invoke_permit.hpp and client.cpp:144-149."""

    @classmethod
    def setUpClass(cls):
        cls.data = read_json(VECTORS)
        c = cls.data['config']
        cls.cfg = {key: c[key] for key in ('workers', 'host', 'port', 'timeout_ms', 'ca', 'cert', 'key', 'peer_name',
                                           'peer_sha256', 'plugin')}
        cls.cfg['profile_hex'] = cls.data['profile_hex']
        cls.vectors = {v['name']: v for v in cls.data['vectors']}

    def test_profile_is_the_compiled_profile(self):
        self.assertEqual(self.data['profile_hex'], PROFILE)
        with open(PROFILE_ID_HPP, encoding='ascii') as f:
            self.assertIn('profile_hex="%s"' % PROFILE, f.read())

    def test_python_contracts_equal_the_header(self):
        for v in self.data['vectors']:
            flags = permit.ClientFlags(mode=v['mode'], **v['flags'])
            paths = (v['input'], v['output']) if v['input'] else None
            got = permit.invocation_contract(self.cfg, self.data['config']['model'], v['rows'], v['requests'],
                                             v['window_ms'], flags, paths)
            self.assertEqual(got['base'], v['base'], v['name'])
            self.assertEqual(got['io'], v['layered'], v['name'])
            self.assertEqual(got['contract'], v['contract'], v['name'])

    def test_vectors_are_the_gateway_invocations(self):
        form, gate, window = self.vectors['form'], self.vectors['synthetic'], self.vectors['pipeline']
        self.assertEqual((form['rows'], form['requests'], form['window_ms'], form['mode']), (1, 1, 0, 'serial'))
        self.assertEqual(dict(form['flags'], mode='serial'), permit.FORM_FLAGS.as_dict())
        self.assertEqual(form['input'], '/run/edgefhe-demo/x/in.csv')
        self.assertEqual(form['output'], '/run/edgefhe-demo/x/score.txt')
        self.assertEqual((gate['rows'], gate['requests'], gate['mode']), (runner.GATE_ROWS, runner.GATE_REQUESTS,
                                                                           'serial'))
        self.assertEqual(dict(window['flags'], mode='pipeline'), permit.PIPELINE_BEST.as_dict())
        self.assertEqual((window['rows'], window['requests'], window['window_ms']),
                         (runner.THROUGHPUT_ROWS, runner.THROUGHPUT_REQUESTS, 30000))
        self.assertEqual(permit.PIPELINE_BEST.argv(), ['--slots', '8', '--duplex', '1', '--hash-io', '1',
                                                       '--decrypt-thread', '1', '--inflight', '8', '--io-cpu', 'auto',
                                                       '--io-isolate', '0'])
        self.assertEqual(permit.FORM_FLAGS.argv(), [])
        self.assertEqual(self.vectors['pipeline_two_slots']['layered'], self.vectors['pipeline_two_slots']['base'])

    def test_permit_text_equals_permit_canonical(self):
        p = self.data['permit']
        text = permit.permit_text(p['boot_id'], p['controller_pid'], p['exe_dev'], p['exe_ino'],
                                  self.vectors[p['vector']]['contract'])
        self.assertEqual(text, p['text'])
        self.assertLess(len(text), permit.PERMIT_MAX_BYTES)

    def test_fake_client_contract_port_equals_the_header(self):
        c = self.data['config']
        for v in self.data['vectors']:
            f = v['flags']
            base = fake.invocation_contract([c['model'], str(v['rows']), v['mode'], str(v['requests']),
                                             str(v['window_ms']), str(c['workers']), c['host'], str(c['port']),
                                             str(c['timeout_ms']), c['ca'], c['cert'], c['key'], c['peer_name'],
                                             c['peer_sha256'], c['plugin'], self.data['profile_hex']])
            capacity = f['slots'] if v['mode'] == 'pipeline' else 1
            io = fake.layered(base, v['mode'], capacity, f['duplex'], f['hash_io'], f['decrypt_thread'], f['inflight'],
                              f['io_cpu'], f['io_isolate'], f['read_every'])
            contract = fake.private_contract(io, v['input'], v['output']) if v['input'] else io
            self.assertEqual((base, io, contract), (v['base'], v['layered'], v['contract']), v['name'])

    def test_fake_client_permit_check_gives_the_header_verdicts(self):
        cases = self.data['permit']['cases']
        self.assertGreaterEqual(len(cases), 15)
        for case in cases:
            try:
                fake.validate_invocation_permit(case['text'].encode('latin-1'), dict(case['identity']), case['contract'])
                code = 'ACCEPTED'
            except fake.Stop as e:
                code = e.args[0]
            self.assertEqual(code, case['code'], case['name'])
        verdicts = {case['name']: case['code'] for case in cases}
        self.assertEqual(verdicts['accepted'], 'ACCEPTED')
        self.assertEqual(verdicts['contract_byte_flipped'], 'PL_PERMIT_INVOCATION_BINDING')
        self.assertEqual(verdicts['mode_0644'], 'PL_PERMIT_IDENTITY')


class PermitRules(unittest.TestCase):
    def test_client_flags_follow_the_client(self):
        bad = [({'mode': 'batch'}, 'MODE_NAME'), ({'slots': 3}, 'SLOTS_PIPELINE_ONLY'),
               ({'duplex': True}, 'DUPLEX_PIPELINE_ONLY'), ({'hash_io': True}, 'O4_O5_PIPELINE_ONLY'),
               ({'mode': 'pipeline', 'inflight': 4}, 'INFLIGHT_REQUIRES_DUPLEX_2_TO_SLOTS'),
               ({'mode': 'pipeline', 'slots': 4, 'duplex': True, 'inflight': 8}, 'INFLIGHT_REQUIRES_DUPLEX_2_TO_SLOTS'),
               ({'read_every': 2}, 'READ_EVERY_REQUIRES_INFLIGHT'), ({'io_isolate': True}, 'IO_ISOLATE_REQUIRES_IO_CPU'),
               ({'io_cpu': 'fast'}, 'IO_CPU_NONE_AUTO_OR_NUMBER'), ({'io_cpu': 1}, 'IO_CPU_NONE_AUTO_OR_NUMBER')]
        for kwargs, code in bad:
            with self.assertRaises(permit.PermitError) as cm:
                permit.ClientFlags(**kwargs)
            self.assertEqual(cm.exception.code, code, kwargs)

    def test_contract_strings_are_checked(self):
        args = ['cdc-diabetes-lr4-v1', 1, 'serial', 1, 0, 3, '192.0.2.10', 18443, 30000, '/a/ca.pem', '/a/c.pem',
                '/a/c.key', 'edgefhe-cloud', PEER, '/a/p.so', PROFILE]
        permit.base_contract(*args)
        for index, value in ((9, '/a/ca\n.pem'), (6, ''), (12, 'edge\0fhe')):
            broken = list(args)
            broken[index] = value
            with self.assertRaises(permit.PermitError) as cm:
                permit.base_contract(*broken)
            self.assertEqual(cm.exception.code, 'PERMIT_CONFIG_STRING')
        with self.assertRaises(permit.PermitError):
            permit.base_contract(*(args[:13] + [PEER.upper()] + args[14:]))
        with self.assertRaises(permit.PermitError) as cm:
            permit.private_contract('a' * 64, '/run/x\n', '/run/y')
        self.assertEqual(cm.exception.code, 'PERMIT_PRIVATE_PATH')

    def test_permit_text_rules(self):
        boot = 'da6d5dd4-c8c1-41fa-bec7-544176e23cc1'
        contract = 'ab' * 32
        self.assertEqual(permit.permit_text(boot, 4242, 1, 2, contract),
                         'EDGEFHE_CPP_MTLS_PL_V1\n%s\n4242\n1\n2\n%s\n' % (boot, contract))
        for args, code in (((boot, 1, 1, 2, contract), 'PL_PERMIT_IDENTITY'),
                           (('not-a-boot-id', 4242, 1, 2, contract), 'PL_PERMIT_BOOT_READ'),
                           ((boot, 4242, 1, 0, contract), 'PL_PERMIT_STAT'),
                           ((boot, 4242, 1, 2, '0' * 64), 'PL_PERMIT_INVOCATION_BINDING')):
            with self.assertRaises(permit.PermitError) as cm:
                permit.permit_text(*args)
            self.assertEqual(cm.exception.code, code)

    @unittest.skipUnless(POSIX, 'POSIX file modes')
    def test_permit_file(self):
        import fcntl
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'permit')
            text = permit.permit_text('da6d5dd4-c8c1-41fa-bec7-544176e23cc1', 4242, 1, 2, 'cd' * 32)
            fd = permit.write_permit(path, text)
            try:
                st = os.fstat(fd)
                self.assertGreaterEqual(fd, 3)
                self.assertEqual((stat.S_IMODE(st.st_mode), st.st_nlink, st.st_size), (0o600, 1, len(text)))
                self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
                self.assertEqual(os.pread(fd, 1024, 0), text.encode('ascii'))
            finally:
                os.close(fd)
            with self.assertRaises(FileExistsError):
                permit.write_permit(path, text)
            link = os.path.join(tmp, 'link')
            os.symlink(os.path.join(tmp, 'elsewhere'), link)
            with self.assertRaises(OSError):
                permit.write_permit(link, text)
            self.assertFalse(os.path.exists(os.path.join(tmp, 'elsewhere')))
        finally:
            shutil.rmtree(tmp)


# --------------------------------------------------------------------------------------------- pure: the form
class FormRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inf = model.Inference.load(MODEL_JSON, FORM_JSON)
        cls.form = read_json(FORM_JSON)
        cls.converter = load_module('convert_cdc_to_v8b', CONVERTER)

    def test_refusal_equals_the_converter(self):
        changes = [{}, {'BMI': 12}, {'BMI': 12.0}, {'BMI': 81.0}, {'BMI': 81}, {'Age': 13}, {'Age': 1}, {'Age': 0},
                   {'Age': 14}, {'Age': -1}, {'Age': 9.0}, {'GenHlth': 0}, {'GenHlth': 6}, {'GenHlth': True},
                   {'GenHlth': 3.0}, {'GenHlth': '3'}, {'HighBP': 2}, {'HighBP': False}, {'HighBP': None},
                   {'BMI': '31.2'}, {'BMI': 31.25}, {'BMI': float('nan')}, {'BMI': float('inf')}, {'BMI': True},
                   {'BMI': None}, {'BMI': 11.9}, {'BMI': -5}, {'BMI': 81.1}, {'BMI': 82.0}, {'Smoker': 0}]
        for change in changes:
            values = dict(M)
            values.update(change)
            self.assertEqual(self.inf.refusal(values), self.converter.refusal(self.form, values), change)
        missing = dict(M)
        del missing['Age']
        for values in (missing, [3, 1, 31.2, 9], None, {}):
            self.assertEqual(self.inf.refusal(values), 'FORM_FIELDS')
            self.assertEqual(self.converter.refusal(self.form, values), 'FORM_FIELDS')
        self.assertEqual(self.inf.refusal(dict(M, BMI=10 ** 400)), 'BMI_ABOVE_MAX')
        self.assertEqual(self.inf.refusal(dict(M, GenHlth=10 ** 400)), 'GENHLTH_VALUE')
        self.assertEqual(self.inf.refusal({1: 3, 'HighBP': 1, 'BMI': 31.2, 'Age': 9}), 'FORM_FIELDS')

    def test_presets_reproduce_their_expected_results(self):
        presets = self.inf.presets()
        self.assertEqual([p['id'] for p in presets], list('ACDEMFGHKJ'))
        for p in presets:
            want = p['expected']
            self.assertEqual(self.inf.refusal(p['form']), want['refused'], p['id'])
            if want['refused']:
                continue
            x = self.inf.csv_values(self.inf.csv_text(p['form']))
            self.assertEqual(x, [float(p['form'][name]) for name in self.inf.features], p['id'])
            logit = self.inf.reference(x)
            self.assertEqual(logit, want['logit_plain'], p['id'])
            self.assertEqual(list(self.inf.classify(logit)), [want['level'], want['boundary']], p['id'])
            self.assertEqual(max(abs(z) for z in self.inf.z_values(x)), want['max_abs_z'], p['id'])
            total = self.inf.bias          # left-to-right adds, as client.cpp:276 (not sum(): 3.12+ compensates)
            for term in self.inf.contributions(x):
                total += term
            self.assertEqual(total, logit, p['id'])

    def test_csv_text_and_every_bmi_value(self):
        self.assertEqual(self.inf.csv_text(M), 'GenHlth,HighBP,BMI,Age\n3,1,31.2,9\n')
        self.assertEqual(self.inf.csv_text(dict(M, BMI=22)), 'GenHlth,HighBP,BMI,Age\n3,1,22.0,9\n')
        for tenths in range(120, 811):
            values = dict(M, BMI=tenths / 10)
            self.assertIsNone(self.inf.refusal(values), tenths)
            self.assertEqual(self.inf.csv_values(self.inf.csv_text(values))[2], tenths / 10)
        with self.assertRaises(model.ModelError):
            self.inf.csv_text(J)

    def test_canonical_json_and_commitment(self):
        text = self.inf.canonical_json(M)
        self.assertEqual(text, '{"Age":9,"BMI":31.2,"GenHlth":3,"HighBP":1}')
        self.assertEqual(self.inf.canonical_json(dict(M, BMI=31)), '{"Age":9,"BMI":31.0,"GenHlth":3,"HighBP":1}')
        salt = 'ab' * 32
        rule = ('EDGEFHE_FORM_COMMIT_V1\n' + salt + '\n' + text + '\n').encode('utf-8')
        self.assertEqual(records.commitment_of(salt, text), hashlib.sha256(rule).hexdigest())
        first, second = records.new_commitment(text), records.new_commitment(text)
        self.assertEqual(len(first[0]), 64)
        self.assertNotEqual(first, second)
        self.assertEqual(records.commitment_of(first[0], text), first[1])
        with self.assertRaises(ValueError):
            records.commitment_of(salt.upper(), text)

    def test_sigmoid_is_stable(self):
        self.assertEqual(model.sigmoid(0.0), 0.5)
        self.assertEqual(model.sigmoid(-1000.0), 0.0)
        self.assertEqual(model.sigmoid(1000.0), 1.0)
        for x in (-30.0, -2.5, -1e-9, 1e-9, 2.5, 30.0):
            self.assertAlmostEqual(model.sigmoid(x) + model.sigmoid(-x), 1.0, places=15)
            self.assertAlmostEqual(model.sigmoid(x), 1.0 / (1.0 + math.exp(-x)), places=15)
        with self.assertRaises(ValueError):
            model.sigmoid(float('nan'))

    def test_evaluate_levels_band_and_failure(self):
        levels = self.form['levels']
        m_logit = 0.6486833051415156
        r = self.inf.evaluate(m_logit + 1e-5, m_logit)
        self.assertEqual((r['level'], r['level_code'], r['boundary'], r['passed'], r['message']), (3, 'L3', None, True,
                                                                                                    None))
        self.assertEqual(r['level_label'], levels['items'][2]['label'])
        self.assertEqual(r['observed_rate'], levels['items'][2]['observed_rate'])
        self.assertAlmostEqual(r['abs_error'], 1e-5, places=12)
        k_logit = 0.00018239930722152575
        b = self.inf.evaluate(k_logit, k_logit)
        self.assertEqual((b['level'], b['boundary'], b['boundary_text']), (None, [2, 3], '介於 L2／L3，不判定'))
        f = self.inf.evaluate(m_logit, m_logit - 0.01)
        self.assertEqual((f['passed'], f['level'], f['boundary']), (False, None, None))
        self.assertEqual(f['message'], self.form['verification']['failed_text'])
        edge = levels['edges_logit'][2]
        self.assertEqual(self.inf.classify(edge + 0.0021), (4, None))
        self.assertEqual(self.inf.classify(edge + 0.0019), (None, [3, 4]))
        self.assertEqual(self.inf.classify(-5.0), (1, None))
        for item in levels['items']:
            self.assertNotIn('機率', item['name'] + item['label'])

    def test_frame_sizes(self):
        self.assertEqual(self.inf.frame_bytes(), (256 + 4 * 98304, 256 + 98304))
        self.assertEqual(self.inf.frame_bytes(), (393472, 98560))


# ------------------------------------------------------------------------------- pure: counters and outputs
class CountersAndOutputs(unittest.TestCase):
    def test_parse_board_interrupt_lines(self):
        lines = runner.parse_interrupts(INTERRUPTS % (5, 7))
        self.assertEqual(sorted(lines), ['61', '62'])
        self.assertEqual(lines['61'], {'count': 6, 'desc': 'GICv2 121 Level xilinx-dma-controller'})
        self.assertEqual(lines['62']['count'], 8)
        later = runner.parse_interrupts(INTERRUPTS % (7, 9))
        self.assertEqual(runner.irq_delta(lines, later), {'61': 2, '62': 2})
        self.assertEqual(runner.parse_interrupts(''), {})

    def test_fake_interrupt_bumps_parse_back(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'interrupts')
            with open(path, 'w', encoding='ascii') as f:
                f.write(INTERRUPTS % (5, 5))
            before = runner.irq_snapshot(path)
            fake.bump_irq({'interrupts': path}, 1)
            fake.bump_irq({'interrupts': path}, 1)
            fake.bump_irq({'interrupts': path}, 0, 3)
            after = runner.irq_snapshot(path)
            self.assertEqual(runner.irq_delta(before, after), {'61': 5, '62': 2})
            self.assertEqual(after['62']['desc'], 'GICv2 136 Level xilinx-dma-controller')
        finally:
            shutil.rmtree(tmp)

    def test_client_output_parsing(self):
        report, stop, detail, problem = runner.parse_client_output(
            1, b'', b'warning\nSTOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY TLS_PEER_PIN\n')
        self.assertEqual((report, stop, detail, problem), (None, 'TLS_PEER_PIN', '', None))
        self.assertEqual(runner.parse_client_output(1, b'', b'STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY std::bad_alloc')[1],
                         'std::bad_alloc')
        self.assertEqual(runner.parse_client_output(1, b'', b'STOP_CPP_E2E_CLIENT_PRESERVE_NO_RETRY $(x)')[1],
                         'UNKNOWN_STOP')
        report = runner.parse_client_output(0, b'noise\n{"status":"S"}\n', b'')[0]
        self.assertEqual(report, {'status': 'S'})
        self.assertIsNotNone(runner.parse_client_output(0, b'{broken', b'')[3])
        self.assertIsNotNone(runner.parse_client_output(0, b'', b'')[3])

    def test_stop_messages(self):
        """Each message is one complete sentence (one 。, at the end) without the no-retry rule, which the owner screen
        and the command line add once as runner.NO_RETRY_LINE; a STOP never claims the system interrupted the run."""
        stops = ('NORMALIZATION_REFUSED', 'TLS_PEER_CHAIN', 'TLS_PEER_PIN', 'TLS_PEER_CERT_REQUIRED', 'SOCKET_CONNECT',
                 'SOCKET_CONNECT_ERROR', 'SOCKET_POLL', 'TLS_DEADLINE', 'TLS_FATAL_OR_UNEXPECTED_EOF',
                 'TLS_HANDSHAKE_EOF', 'TLS_SOMETHING_NEW', 'PL_PERMIT_IDENTITY', 'PL_PERMIT_INVOCATION_BINDING',
                 'PL_ROOT_CONTROLLER_REQUIRED', 'OWNER_CREATE', 'OWNER_ENCRYPT', 'PL_CLOCK_RANGE',
                 'CPU_SPEC_PL_FRAME_ACCOUNTING', 'WIRE_RESPONSE_BINDING', 'RESPONSE_SIZE', 'CSV_OPEN',
                 'PRIVATE_OPEN', 'PERMIT_FORMAT', 'A_NEW_CODE')
        failed_text = read_json(FORM_JSON)['verification']['failed_text']
        for code in stops + tuple(runner.MESSAGES):
            text = runner.describe(code, host='192.0.2.10', port=18443, timeout_ms=30000, text=failed_text)
            self.assertNotIn('不會自動重試', text, code)
            self.assertNotIn('已中止', text, code)
            self.assertNotIn('；', text, code)
            self.assertTrue(text.endswith('。') and text.count('。') == 1, (code, text))
            self.assertNotIn('%(', text, code)
            self.assertNotIn('機率', text, code)
            for word in ('板子', '開發板', '請勿', '請待', '請以', '請關閉'):     # glossary; explicit subjects
                self.assertNotIn(word, text, code)
        for code in ('TLS_PEER_CHAIN', 'TLS_PEER_PIN', 'TLS_DEADLINE', 'TLS_FATAL_OR_UNEXPECTED_EOF', 'TLS_X'):
            self.assertIn('加密程式已自行停止此次推論', runner.describe(code), code)
        self.assertEqual(runner.NO_RETRY_LINE, '本系統不會自動重試，也不會中斷加密程式。')
        self.assertIn('192.0.2.10:18443', runner.describe('SOCKET_CONNECT_ERROR', host='192.0.2.10', port=18443))
        self.assertIn('（30,000 毫秒）', runner.describe('TLS_DEADLINE', timeout_ms=30000))
        self.assertIn('（500 毫秒）', runner.describe('TLS_DEADLINE', timeout_ms=500))
        self.assertIn('PL_PERMIT_STAT', runner.describe('PL_PERMIT_STAT'))
        self.assertIn('操作人員不得手動終止加密程式', runner.describe('CLIENT_OVERDUE', seconds='120'))
        self.assertIn('操作人員應待該筆請求完成後再送出', runner.describe('BUSY'))
        self.assertIn('操作人員應待該程序結束後再送出', runner.describe('PL_LOCK_BUSY', lock=runner.PL_LOCK_PATH))
        self.assertIn('偵測到 KV260 的跡象', runner.describe('REHEARSAL_PL_ON_BOARD', node=runner.DMA_NODE))
        from efgw import httpapi
        for table in (httpapi.MESSAGES, httpapi.SERVE_MESSAGES):
            for code, template in table.items():
                text = template % collections.defaultdict(lambda: '8080', {})
                for word in ('板子', '開發板', '請勿', '請待', '請關閉'):
                    self.assertNotIn(word, text, code)
        for code in httpapi.MESSAGES:                   # the {code, message} refusals of the HTTP API
            text = httpapi.MESSAGES[code] % collections.defaultdict(lambda: '8080', {})
            self.assertTrue(text.endswith('。') and text.count('。') == 1 and '；' not in text, (code, text))
        self.assertIn('未發送任何訊號', runner.describe('CLIENT_SIGNALLED', signal='SIGKILL'))
        for code in runner.HTTP_STATUS:
            self.assertIn(code, runner.MESSAGES, code)

    def test_board_markers(self):
        tmp = tempfile.mkdtemp()
        try:
            compatible = os.path.join(tmp, 'compatible')
            node = os.path.join(tmp, 'edgefhe_v8b_dma')
            cfg = {'dma_node': node}
            missing = os.path.join(tmp, 'missing')
            if not os.path.lexists(runner.DMA_NODE):            # a PC or a container: no V8b driver node
                self.assertEqual(runner.board_markers(cfg, compatible=missing), [])
            with open(compatible, 'wb') as f:
                f.write(b'xlnx,zynqmp-sk-kv260-revB\0xlnx,zynqmp-sk-kv260\0xlnx,zynqmp\0')
            self.assertTrue(any(compatible in m for m in runner.board_markers(cfg, compatible=compatible)))
            with open(compatible, 'wb') as f:
                f.write(b'linux,dummy-virt\0')
            self.assertFalse(any(compatible in m for m in runner.board_markers(cfg, compatible=compatible)))
            open(node, 'w').close()
            self.assertIn(node, runner.board_markers(cfg, compatible=missing))
            self.assertEqual(runner.board_markers({'dma_node': runner.DMA_NODE}, compatible=missing),
                             runner.board_markers(None, compatible=missing))
        finally:
            shutil.rmtree(tmp)

    @unittest.skipUnless(POSIX, 'POSIX symlinks and hard links')
    def test_holder_scan_reads_links_first(self):
        """A descriptor on a network or FUSE file system is never stat()ed (a hung mount would stall the admission);
        the node's own path still matches at once, and another name of the node on a local file system still matches
        by identity. A fake /proc: symlinks stand in for /proc/<pid>/fd/<n>."""
        tmp = tempfile.mkdtemp()
        try:
            local, remote = os.path.join(tmp, 'local'), os.path.join(tmp, 'remote mount')
            os.makedirs(local)
            os.makedirs(remote)
            node = os.path.join(local, 'edgefhe_v8b_dma')
            open(node, 'w').close()
            local_twin, remote_twin = os.path.join(local, 'twin'), os.path.join(remote, 'twin')
            os.link(node, local_twin)
            os.link(node, remote_twin)
            proc = os.path.join(tmp, 'proc')
            os.makedirs(os.path.join(proc, 'self'))
            with open(os.path.join(proc, 'self', 'mounts'), 'w', encoding='utf-8') as f:
                f.write('/dev/root / ext4 rw 0 0\n')
                f.write('host:/export %s nfs4 rw 0 0\n' % remote.replace(' ', '\\040'))
                f.write('tmpfs %s tmpfs rw 0 0\n' % os.path.join(tmp, 'elsewhere'))

            def holder(pid, *targets):
                fd_dir = os.path.join(proc, str(pid), 'fd')
                os.makedirs(fd_dir)
                for index, target in enumerate(targets):
                    os.symlink(target, os.path.join(fd_dir, str(index)))
            holder(101, 'pipe:[1234]', 'socket:[99]', 'anon_inode:[eventfd]', node)
            holder(102, local_twin)
            holder(103, remote_twin)
            holder(104, os.path.join(local, 'missing'))
            with unittest.mock.patch.object(runner.os, 'stat', wraps=os.stat) as spy:
                self.assertEqual(runner.dma_holders(node, proc_root=proc), [101, 102])
            statted = [str(c.args[0]) for c in spy.call_args_list]
            self.assertFalse([p for p in statted if p.startswith(os.path.join(proc, '103') + os.sep)], statted)
            self.assertFalse([p for p in statted if p.startswith(os.path.join(proc, '101') + os.sep)], statted)
            self.assertTrue(runner._on_remote_fs(remote_twin, runner._remote_mounts(proc)))
            self.assertFalse(runner._on_remote_fs(local_twin, runner._remote_mounts(proc)))
            self.assertFalse(runner._on_remote_fs(remote + 'x/twin', runner._remote_mounts(proc)))
        finally:
            shutil.rmtree(tmp)

    def test_cli_writes_the_error_as_on_the_owner_screen(self):
        """ux#14: the command line writes the CKKS error as a×10⁻ⁿ (demo/ui/lib.mjs fmtSci), never in e-notation."""
        sci = gateway_cli.sci
        self.assertEqual(sci(2.4e-06), '2.4×10⁻⁶')
        self.assertEqual(sci(0.002), '2×10⁻³')
        self.assertEqual(sci(2.976e-05), '3×10⁻⁵')
        self.assertEqual(sci(9.96e-04), '1×10⁻³')
        self.assertEqual(sci(1.5e-12), '1.5×10⁻¹²')
        self.assertEqual(sci(31.2), '3.1×10¹')
        self.assertEqual(sci(0.0), '0')
        self.assertEqual(sci(float('nan')), '—')
        self.assertEqual(sci(None), '—')

    @unittest.skipUnless(POSIX, 'POSIX pipes and selectors')
    def test_client_is_waited_for_when_no_reader_thread_starts(self):
        """security#7: a reader thread that cannot start never makes the gateway skip the wait for the client."""
        code = ('import sys; sys.stdout.write("o" * 200000); sys.stdout.flush(); '
                'sys.stderr.write("e" * 200000); sys.stderr.flush()')
        original = threading.Thread.start
        for failing in ({0, 1}, {1}):
            started = []

            def start(thread):
                if getattr(thread, '_target', None) is runner._drain:
                    index = len(started)
                    started.append(index)
                    if index in failing:
                        raise RuntimeError("can't start new thread")
                return original(thread)
            threading.Thread.start = start
            try:
                client = runner.ClientProcess([sys.executable, '-I', '-B', '-c', code])
            finally:
                threading.Thread.start = original
            self.assertFalse(client.exited)
            returncode, out, err, _ = client.wait()     # 200 kB on each pipe: both must be drained, no deadlock
            self.assertEqual((returncode, out, err), (0, b'o' * 200000, b'e' * 200000), failing)
            self.assertTrue(client.exited)

    def test_report_requirements(self):
        plan = {'kind': 'form', 'rows': 1, 'requests': 1, 'window_ms': 0, 'flags': permit.FORM_FLAGS}
        report = {'status': runner.PASS_STATUS['pl'], 'backend': 'pl', 'model': model.MODEL_ID, 'features': 4,
                  'rows': 1, 'mode': 'serial', 'synthetic_input': False, 'automatic_retries': 0, 'real_PL': True,
                  'completed_requests': 1, 'device_calls': 2, 'PL_hz': 199999998,
                  'trace': [{'request_sha256': 'a' * 64, 'response_payload_sha256': 'b' * 64}]}
        self.assertEqual(runner.report_mismatches(report, 'form', 'pl', plan), [])
        for key, value in (('device_calls', 3), ('PL_hz', 100000000), ('rows', 2), ('model', 'other'),
                           ('status', 'CPP_E2E_CPU_SPEC_NUMERIC_AND_DRAIN_PASS'), ('features', True),
                           ('trace', [])):
            broken = dict(report)
            broken[key] = value
            self.assertEqual(runner.report_mismatches(broken, 'form', 'pl', plan)[0][0], key, key)
        cpu = dict(report, status=runner.PASS_STATUS['cpu'], backend='cpu', real_PL=False, device_calls=0, PL_hz=0)
        self.assertEqual(runner.report_mismatches(cpu, 'form', 'cpu', plan), [])


class ConfigRules(unittest.TestCase):
    def example(self):
        raw = read_json(EXAMPLE)
        raw['peer_sha256'] = PEER
        return raw

    def test_example_loads_once_the_pin_is_filled_in(self):
        cfg = config.validate(self.example(), 'example')
        self.assertEqual((cfg['listen'], cfg['default_backend'], cfg['backends']), ('127.0.0.1:8080', 'pl',
                                                                                    ['pl', 'spec', 'cpu']))
        self.assertEqual(cfg['proc_interrupts'], '/proc/interrupts')
        self.assertEqual(cfg['dma_node'], '/dev/edgefhe_v8b_dma')
        self.assertEqual(cfg['host'], '192.0.2.10')
        self.assertEqual(config.parse_listen(cfg['listen']), ('127.0.0.1', 8080))
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(read_json(EXAMPLE))
        self.assertEqual(cm.exception.code, 'CONFIG_PIN')

    def test_rejections(self):
        cases = [('client', 'net/efnet_client', 'CONFIG_PATH'), ('client', '/home/u/../x', 'CONFIG_PATH'),
                 ('client', '/home/u//x', 'CONFIG_PATH'), ('client', '/home/u/a b', 'CONFIG_PATH'),
                 ('ca', '/home/ü/ca.pem', 'CONFIG_PATH'), ('key', '/home/u/k/', 'CONFIG_PATH'),
                 ('workdir', '/run', 'CONFIG_PATH'), ('model_json', 'model.json', 'CONFIG_PATH'),
                 ('host', '192.0.2.010', 'CONFIG_HOST'), ('host', '0.0.0.0', 'CONFIG_HOST'),
                 ('host', 'edgefhe-cloud', 'CONFIG_HOST'), ('host', '224.0.0.1', 'CONFIG_HOST'),
                 ('peer_sha256', PEER.upper(), 'CONFIG_PIN'), ('peer_sha256', '0' * 64, 'CONFIG_PIN'),
                 ('profile_hex', PROFILE[:-1], 'CONFIG_PIN'), ('workers', 2, 'CONFIG_RANGE'),
                 ('workers', True, 'CONFIG_TYPE'), ('port', 0, 'CONFIG_RANGE'), ('port', '18443', 'CONFIG_TYPE'),
                 ('timeout_ms', 99, 'CONFIG_RANGE'), ('peer_name', 'EdgeFHE-Cloud', 'CONFIG_PEER_NAME'),
                 ('backends', ['pl', 'gpu'], 'CONFIG_BACKENDS'), ('backends', [], 'CONFIG_BACKENDS'),
                 ('listen', '127.0.0.1', 'CONFIG_LISTEN'), ('listen', 'localhost:8080', 'CONFIG_LISTEN'),
                 ('progress_ms', 5000, 'CONFIG_RANGE'), ('retain_private', 1, 'CONFIG_TYPE'),
                 ('schema', 'other', 'CONFIG_SCHEMA'), ('surprise', 1, 'CONFIG_UNKNOWN_KEY')]
        for key, value, code in cases:
            raw = self.example()
            raw[key] = value
            with self.assertRaises(config.ConfigError) as cm:
                config.validate(raw)
            self.assertEqual(cm.exception.code, code, (key, value))
        raw = self.example()
        del raw['host']
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(raw)
        self.assertEqual(cm.exception.code, 'CONFIG_MISSING')
        raw = dict(self.example(), backends=['pl'], default_backend='cpu')
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(raw)
        self.assertEqual(cm.exception.code, 'CONFIG_BACKENDS')
        self.assertEqual(config.validate(dict(self.example(), peer_name='192.0.2.10'))['peer_name'], '192.0.2.10')

    def test_duplicate_keys_and_messages(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'gateway.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{"port": 18443, "port": 18444}')
            with self.assertRaises(config.ConfigError) as cm:
                config.load(path)
            self.assertEqual(cm.exception.code, 'CONFIG_DUPLICATE_KEY')
            self.assertIn('port', cm.exception.message())
            with self.assertRaises(config.ConfigError) as cm:
                config.load(os.path.join(tmp, 'missing.json'))
            self.assertEqual(cm.exception.code, 'CONFIG_READ')
        finally:
            shutil.rmtree(tmp)


class Records(unittest.TestCase):
    def test_public_record_whitelist(self):
        fields = {key: 1 for key in records.PUBLIC_FIELDS}
        fields.update({key: 'owner value' for key in records.OWNER_FIELDS})
        out = records.public_record(fields)
        self.assertEqual(list(out), list(records.PUBLIC_FIELDS))
        for key in records.OWNER_FIELDS:
            self.assertNotIn(key, out)
        for key in ('x', 'z', 'logit_ckks', 'logit_plain', 'p_balanced', 'level', 'boundary', 'salt_hex', 'form'):
            self.assertIn(key, records.OWNER_FIELDS)
        trace = records.public_record({'trace': [{'request_sha256': 'a', 'x': 1}]})['trace']
        self.assertEqual(trace, [{'request_sha256': 'a'}])

    def test_event_log(self):
        log = records.EventLog(capacity=3)
        seen = []
        log.add_listener(seen.append)
        a = log.emit('r000000000001', 'form.accepted', 'public', {'commitment': 'c'})
        b = log.emit('r000000000001', 'form.accepted', 'owner', {'x': [1.0]})
        self.assertEqual(set(a), {'v', 'seq', 'run', 'type', 'class', 't_mono_ns', 'data'})
        self.assertEqual((a['v'], a['seq'], b['seq']), (1, 1, 2))
        self.assertEqual([e['seq'] for e in log.since(0, classes=('public',))], [1])
        self.assertEqual(log.wait(2, 0.05), [])
        self.assertEqual(seen, [a, b])
        with self.assertRaises(ValueError):
            log.emit('r000000000001', 'x', 'secret', {})
        with self.assertRaises(ValueError):
            log.emit('r000000000001', 'x', 'public', {'v': float('nan')})
        for _ in range(4):
            log.emit('r000000000002', 'progress.io', 'public', {})
        self.assertEqual([e['seq'] for e in log.since(0)], [4, 5, 6])
        self.assertEqual(log.first_seq(), 4)
        got = []
        waiter = threading.Thread(target=lambda: got.extend(log.wait(6, 5.0)))
        waiter.start()
        time.sleep(0.05)
        log.emit('r000000000002', 'wiped', 'public', {})
        waiter.join(5.0)
        self.assertEqual([e['seq'] for e in got], [7])

    @unittest.skipUnless(POSIX, 'POSIX file modes')
    def test_private_store_disclosure_and_wipe(self):
        tmp = tempfile.mkdtemp()
        try:
            text = '{"Age":9,"BMI":31.2,"GenHlth":3,"HighBP":1}'
            salt, commitment = records.new_commitment(text)
            request = hashlib.sha256(b'request').hexdigest()
            record = {'schema': records.PRIVATE_SCHEMA, 'run': 'r0123456789ab', 'kind': 'form', 'backend': 'pl',
                      'request_sha256': request, 'commitment': commitment, 'salt_hex': salt, 'canonical_json': text,
                      'form': M, 'level': 3}
            store = records.PrivateStore(os.path.join(tmp, 'private'))
            store.save(record)
            path = store.path('r0123456789ab')
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(store.directory).st_mode), 0o700)
            self.assertEqual(store.find('r0123456789ab'), record)
            self.assertEqual(store.find(request), record)
            self.assertIsNone(store.find(hashlib.sha256(b'other').hexdigest()))
            self.assertIsNone(store.find('../r0123456789ab'))
            out, existed = records.write_disclosure(os.path.join(tmp, 'disclosure'), record)
            self.assertEqual((os.path.basename(out), existed), (request + '.json', False))
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
            doc = read_json(out)
            self.assertEqual((doc['fictional'], doc['level'], doc['schema']), (True, 3, records.DISCLOSURE_SCHEMA))
            self.assertEqual(records.commitment_of(doc['salt_hex'], doc['canonical_json']), commitment)
            self.assertEqual(records.write_disclosure(os.path.join(tmp, 'disclosure'), record)[1], True)
            with self.assertRaises(ValueError):
                records.disclosure(dict(record, canonical_json=text.replace('31.2', '31.3')))
            self.assertTrue(store.remove('r0123456789ab'))
            self.assertIsNone(store.find('r0123456789ab'))
        finally:
            shutil.rmtree(tmp)

    @unittest.skipUnless(POSIX, 'POSIX file modes')
    def test_a_failed_private_write_leaves_no_file(self):
        """A private file whose write fails after its exclusive create (ENOSPC on tmpfs) is wiped at once, so no
        partial salt or answers stay behind; the same holds for in.csv and for a disclosure."""
        tmp = tempfile.mkdtemp()
        try:
            text = '{"Age":9,"BMI":31.2,"GenHlth":3,"HighBP":1}'
            salt, commitment = records.new_commitment(text)
            record = {'schema': records.PRIVATE_SCHEMA, 'run': 'r0123456789ab', 'kind': 'form', 'backend': 'pl',
                      'request_sha256': hashlib.sha256(b'request').hexdigest(), 'commitment': commitment,
                      'salt_hex': salt, 'canonical_json': text, 'form': M}
            store = records.PrivateStore(os.path.join(tmp, 'private'))
            written = []

            def full_tmpfs(fd, data):
                written.append(os.write(fd, data[:16]))
                raise OSError(errno.ENOSPC, 'No space left on device')
            with unittest.mock.patch.object(records, '_write_all', full_tmpfs):
                with self.assertRaises(OSError):
                    store.save(record)
                with self.assertRaises(OSError):
                    records.write_disclosure(os.path.join(tmp, 'disclosure'), record)
                with self.assertRaises(OSError):
                    runner.write_private(os.path.join(tmp, 'in.csv'), 'GenHlth,HighBP,BMI,Age\n3,1,31.2,9\n')
            self.assertEqual(written, [16, 16, 16])
            self.assertEqual(os.listdir(store.directory), [])
            self.assertEqual(os.listdir(os.path.join(tmp, 'disclosure')), [])
            self.assertFalse(os.path.lexists(os.path.join(tmp, 'in.csv')))
            store.save(record)                          # the store still works once space is back
            self.assertEqual(store.find('r0123456789ab'), record)
        finally:
            shutil.rmtree(tmp)

    @unittest.skipUnless(POSIX, 'POSIX hard links')
    def test_wipe_overwrites_before_unlink(self):
        tmp = tempfile.mkdtemp()
        try:
            path, observer = os.path.join(tmp, 'in.csv'), os.path.join(tmp, 'observer')
            data = b'GenHlth,HighBP,BMI,Age\n3,1,31.2,9\n'
            with open(path, 'wb') as f:
                f.write(data)
            os.link(path, observer)
            self.assertTrue(runner.wipe_file(path))
            self.assertFalse(os.path.exists(path))
            with open(observer, 'rb') as f:
                self.assertEqual(f.read(), bytes(len(data)))
            self.assertFalse(runner.wipe_file(path))
            target, link = os.path.join(tmp, 'target'), os.path.join(tmp, 'link')
            with open(target, 'wb') as f:
                f.write(b'keep')
            os.symlink(target, link)
            self.assertTrue(runner.wipe_file(link))
            self.assertFalse(os.path.lexists(link))
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'keep')
        finally:
            shutil.rmtree(tmp)


# ------------------------------------------------------------------------------------- runs through the CLI
@unittest.skipUnless(LINUX_ROOT, 'needs root on Linux (run in the python:3.10-slim container)')
class CommandLineRuns(unittest.TestCase):
    def setUp(self):
        self.bench = Bench()

    def tearDown(self):
        self.bench.close()

    def assertZeroed(self, observed, names):
        self.assertEqual(sorted(name.split('.', 1)[1] for name in observed), sorted(names))
        for name, data in observed.items():
            self.assertTrue(data and data == bytes(len(data)), name)

    def test_form_on_pl_permit_accepted_result_wipe_record_and_disclosure(self):
        b = self.bench
        b.write_config(retain_private=True)         # an evidence session: the disclosure needs the private record
        r = b.submit(M, '--backend', 'pl')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_RESULT'))
        self.assertEqual((f['backend'], f['level'], f['boundary'], f['passed']), ('pl', '3', '-', '1'))
        events = events_of(r.stdout)
        self.assertEqual([(e['type'], e['class']) for e in events],
                         [('form.accepted', 'public'), ('form.accepted', 'owner'), ('client.spawned', 'public'),
                          ('client.exited', 'public'), ('irq.delta', 'public'), ('result', 'owner'),
                          ('wiped', 'public')])
        self.assertEqual(len({e['run'] for e in events}), 1)
        self.assertEqual([e['seq'] for e in events], list(range(1, 8)))
        run = events[0]['run']
        commitment = events[0]['data']['commitment']
        self.assertEqual(events[1]['data']['commitment'], commitment)
        self.assertNotIn('x', events[0]['data'])
        self.assertEqual(events[1]['data']['x'], [3.0, 1.0, 31.2, 9.0])
        spawned = events[2]['data']
        self.assertTrue(spawned['permit'])
        self.assertEqual(spawned['argv'][0], b.client)
        self.assertIn('--permit-fd', spawned['argv'])
        self.assertEqual(spawned['argv'][spawned['argv'].index('--input') + 1], os.path.join(b.workdir, run, 'in.csv'))
        exited = events[3]['data']
        self.assertEqual((exited['returncode'], exited['status'], exited['stop']), (0, runner.PASS_STATUS['pl'], None))
        irq = events[4]['data']
        self.assertEqual((irq['delta'], irq['expected'], irq['passed']), ({'61': 2, '62': 2}, 2, True))
        result = events[5]['data']
        self.assertEqual((result['level'], result['boundary'], result['passed']), (3, None, True))
        self.assertLess(result['abs_error'], 2e-3)
        evidence = result['evidence']
        self.assertEqual(evidence['request_sha256'], exited['report']['trace'][0]['request_sha256'])
        self.assertEqual((evidence['request_frame_bytes'], evidence['response_frame_bytes']), (393472, 98560))
        self.assertEqual(evidence['commitment'], commitment)
        self.assertEqual(sorted(events[6]['data']['files']), ['in.csv', 'permit', 'score.txt'])
        self.assertTrue(events[6]['data']['run_dir_removed'])
        self.assertIs(events[6]['data']['retained_private'], True)
        self.assertIn('私有紀錄保存於板端 tmpfs', r.stderr)
        self.assertIn('（容許 2×10⁻³）', r.stderr)           # a×10⁻ⁿ as on the owner screen, no e-notation
        self.assertRegex(r.stderr, r'CKKS 與明文模型相差 (0|[0-9](\.[0-9])?×10[⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+)（')
        spawns = b.spawns()
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0]['pid'], spawned['pid'])
        self.assertZeroed(b.observed(), ['in.csv', 'score.txt'])
        self.assertEqual(sorted(os.listdir(b.workdir)), ['private'])
        self.assertEqual(os.listdir(os.path.join(b.workdir, 'private')), [run + '.json'])
        self.assertEqual(b.signals(), [])
        public = b.public_records()
        self.assertEqual(len(public), 1)
        rec = public[0]
        self.assertTrue(set(rec) <= set(records.PUBLIC_FIELDS), set(rec) - set(records.PUBLIC_FIELDS))
        self.assertEqual((rec['kind'], rec['run'], rec['commitment'], rec['verified']), ('form', run, commitment, True))
        self.assertEqual((rec['request_sha256'], rec['device_calls'], rec['irq_delta']),
                         (evidence['request_sha256'], 2, {'61': 2, '62': 2}))
        d = b.run('disclose', rec['request_sha256'])
        self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
        path = fields_of(marker(d.stdout, 'GATEWAY_DISCLOSURE_WRITTEN'))['path']
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = read_json(path)
        self.assertTrue(doc['fictional'])
        self.assertEqual(records.commitment_of(doc['salt_hex'], doc['canonical_json']), commitment)
        self.assertEqual(json.loads(doc['canonical_json']), M)
        self.assertEqual((doc['level'], doc['request_sha256']), (3, rec['request_sha256']))
        self.assertEqual(os.listdir(os.path.join(b.workdir, 'private')), [])
        again = b.run('disclose', rec['request_sha256'])
        self.assertEqual(fields_of(marker(again.stdout, 'GATEWAY_DISCLOSURE_WRITTEN'))['existed'], '1')

    def test_default_keeps_no_private_record_and_prints_public_events_only(self):
        """security#0 and security#2: retain_private defaults to false, and without --owner-events the command line
        prints no owner-class event (answers, x, z, logits, level) on stdout."""
        b = self.bench
        self.assertIs(b.load()['retain_private'], False)
        r = b.submit(M, '--backend', 'pl', owner_events=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        events = events_of(r.stdout)
        self.assertEqual([(e['type'], e['class']) for e in events],
                         [('form.accepted', 'public'), ('client.spawned', 'public'), ('client.exited', 'public'),
                          ('irq.delta', 'public'), ('wiped', 'public')])
        for line in r.stdout.splitlines():
            if line.startswith('EVT '):
                for key in ('"x":', '"z":', '"values":', '"contributions":', '"logit_ckks":', '"p_balanced":',
                            '"level":', '"salt_hex":'):
                    self.assertNotIn(key, line)
        wiped = events[-1]['data']
        self.assertEqual((wiped['retained_private'], wiped['run_dir_removed']), (False, True))
        self.assertIn('未保存任何私有紀錄', r.stderr)
        self.assertZeroed(b.observed(), ['in.csv', 'score.txt'])
        self.assertEqual(os.listdir(b.workdir), [])
        request = b.public_records()[0]['request_sha256']
        d = b.run('disclose', request)
        self.assertEqual(d.returncode, 1, d.stdout + d.stderr)
        self.assertIsNotNone(marker(d.stdout, 'GATEWAY_DISCLOSURE_NOT_FOUND'))
        self.assertIn('retain_private', d.stderr)
        self.assertFalse(os.path.exists(b.disclosure_dir))
        refused = b.submit(J, '--backend', 'pl', owner_events=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(events_of(refused.stdout), [])                 # form.refused is owner-class
        self.assertEqual(events_of(b.submit(M, '--backend', 'pl', '--quiet').stdout), [])

    def test_boundary_form_shows_no_level(self):
        r = self.bench.submit(K, '--backend', 'pl')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_RESULT'))
        self.assertEqual((f['level'], f['boundary']), ('-', '2/3'))
        result = [e for e in events_of(r.stdout) if e['type'] == 'result'][0]['data']
        self.assertEqual((result['level'], result['boundary'], result['boundary_text']), (None, [2, 3],
                                                                                           '介於 L2／L3，不判定'))

    def test_refused_form_starts_no_process(self):
        b = self.bench
        form = read_json(FORM_JSON)
        for values, code in ((J, 'BMI_ABOVE_MAX'), (dict(M, Age=0), 'AGE_UNDER_18'), (dict(M, BMI='31.25'),
                                                                                       'BMI_FORMAT')):
            r = b.submit(values, '--backend', 'pl')
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_REFUSED'))['code'], code)
            events = events_of(r.stdout)
            self.assertEqual([(e['type'], e['class']) for e in events], [('form.refused', 'owner')])
            self.assertEqual(events[0]['data']['message'], form['refusals'][code])
            self.assertIn(form['refusals'][code], r.stderr)
        self.assertEqual(b.spawns(), [])
        self.assertFalse(os.path.exists(b.workdir))
        self.assertEqual(b.public_records(), [])

    def test_stop_code_reported_once_and_files_wiped(self):
        b = self.bench
        for code, needle in (('TLS_PEER_PIN', '釘選值'), ('SOCKET_CONNECT_ERROR', '127.0.0.1:18443')):
            b.behave(stop=code)
            r = b.submit(M, '--backend', 'pl')
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_STOP'))['code'], code)
            events = events_of(r.stdout)
            errors = [e['data'] for e in events if e['type'] == 'error']
            self.assertEqual([(e['code'], e['fatal'], e['stop']) for e in errors], [(code, True, True)])
            self.assertIn(needle, errors[0]['message'])
            self.assertNotIn('不會自動重試', errors[0]['message'])
            self.assertEqual(r.stderr.count(runner.NO_RETRY_LINE), 1, r.stderr)
            self.assertIn(errors[0]['message'] + '\n' + runner.NO_RETRY_LINE, r.stderr)
            self.assertNotIn('result', [e['type'] for e in events])
            self.assertEqual(events[-1]['type'], 'wiped')
            self.assertIs(events[-1]['data']['retained_private'], False)
        self.assertEqual(len(b.spawns()), 2)
        self.assertEqual(len(b.observed()), 2)
        for data in b.observed().values():
            self.assertEqual(data, bytes(len(data)))
        self.assertEqual([(p['stop'], p['request_sha256']) for p in b.public_records()],
                         [('TLS_PEER_PIN', None), ('SOCKET_CONNECT_ERROR', None)])
        self.assertEqual(os.listdir(os.path.join(b.workdir)), [])

    def test_verification_failure_shows_no_level(self):
        b = self.bench
        b.behave(logit_offset=0.01)
        r = b.submit(M, '--backend', 'pl')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_FAILED'))['code'], 'VERIFICATION_FAILED')
        result = [e for e in events_of(r.stdout) if e['type'] == 'result'][0]['data']
        failed_text = read_json(FORM_JSON)['verification']['failed_text']
        self.assertEqual((result['passed'], result['level'], result['boundary'], result['message']),
                         (False, None, None, failed_text))
        self.assertIn(failed_text, r.stderr)
        self.assertEqual((b.public_records()[0]['verified'], b.public_records()[0]['error']),
                         (False, 'VERIFICATION_FAILED'))

    def test_report_and_interrupt_mismatches(self):
        b = self.bench
        for change, code in (({'device_calls_offset': 1}, 'CLIENT_REPORT_MISMATCH'),
                             ({'device_calls_offset': 0, 'irq_extra': 1}, 'IRQ_DELTA_MISMATCH')):
            b.behave(**change)
            r = b.submit(M, '--backend', 'pl')
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_FAILED'))['code'], code)
            self.assertNotIn('result', [e['type'] for e in events_of(r.stdout)])
        b.behave(irq_extra=0)
        b.set_interrupts(INTERRUPTS.replace(' 62: %10d          0          1          0     GICv2 136 Level     '
                                            'xilinx-dma-controller\n', '') % (5,))
        r = b.submit(M, '--backend', 'pl')
        self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_FAILED'))['code'], 'IRQ_LINES')

    def test_cpu_and_spec_take_no_permit(self):
        b = self.bench
        for backend, calls in (('spec', 2), ('cpu', 0)):
            r = b.submit(M, '--backend', backend)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            events = events_of(r.stdout)
            spawned = [e['data'] for e in events if e['type'] == 'client.spawned'][0]
            self.assertFalse(spawned['permit'])
            self.assertNotIn('--permit-fd', spawned['argv'])
            exited = [e['data'] for e in events if e['type'] == 'client.exited'][0]
            self.assertEqual(exited['report']['device_calls'], calls)
            irq = [e['data'] for e in events if e['type'] == 'irq.delta'][0]
            self.assertEqual((irq['expected'], irq['passed']), (None, None))

    def test_device_held_by_another_process(self):
        b = self.bench
        holder = subprocess.Popen([sys.executable, '-c', 'import sys, time; f = open(sys.argv[1]); '
                                   'print("held", flush=True); time.sleep(60)', b.dma_node],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            r = b.submit(M, '--backend', 'pl')
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_RUN_NOT_STARTED'))['code'], 'PL_DEVICE_BUSY')
            self.assertIn(str(holder.pid), r.stderr)
        finally:
            holder.kill()           # the test's own helper process, never a client
            holder.wait()
            holder.stdout.close()
        self.assertEqual(b.spawns(), [])

    def test_lock_held_by_another_process(self):
        """security#10: the lock path is fixed, so a gateway with another work directory is excluded as well."""
        import fcntl
        b = self.bench
        fd = os.open(runner.PL_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for workdir in (b.workdir, os.path.join(b.base, 'other-workdir')):
                b.write_config(workdir=workdir)
                r = b.submit(M, '--backend', 'pl')
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                f = fields_of(marker(r.stdout, 'GATEWAY_RUN_NOT_STARTED'))
                self.assertEqual(f['code'], 'PL_LOCK_BUSY')
                self.assertIn(runner.PL_LOCK_PATH, r.stderr)
        finally:
            os.close(fd)
        self.assertEqual(b.spawns(), [])
        b.write_config(workdir=b.workdir)
        self.assertEqual(b.submit(M, '--backend', 'cpu').returncode, 0)

    def test_device_held_through_another_node_of_the_same_device(self):
        """security#10: holders are matched by device number (st_rdev), not by path; a regular test node by inode."""
        b = self.bench
        twin = os.path.join(b.base, 'twin_of_null')
        try:
            os.mknod(twin, stat.S_IFCHR | 0o600, os.makedev(1, 3))         # the numbers of /dev/null
        except OSError:
            twin = None
        if twin is not None:
            holder = subprocess.Popen([sys.executable, '-c', 'import sys, time; f = open("/dev/null"); '
                                       'print("held", flush=True); time.sleep(60)'], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'held')
                self.assertIn(holder.pid, runner.dma_holders([os.path.join(b.base, 'missing'), twin]))
            finally:
                holder.kill()           # the test's own helper process, never a client
                holder.wait()
                holder.stdout.close()
        link = os.path.join(b.base, 'node_link')
        os.link(b.dma_node, link)
        holder = subprocess.Popen([sys.executable, '-c', 'import sys, time; f = open(sys.argv[1]); '
                                   'print("held", flush=True); time.sleep(60)', link], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            self.assertEqual(runner.dma_holders(b.dma_node), [holder.pid])          # opened under another name
            r = b.submit(M, '--backend', 'pl')
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_RUN_NOT_STARTED'))['code'], 'PL_DEVICE_BUSY')
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual(runner.dma_holders(b.dma_node), [])
        self.assertEqual(runner.dma_holders(os.path.join(b.base, 'missing')), [])
        self.assertEqual(b.spawns(), [])

    def test_backend_not_allowed(self):
        b = self.bench
        b.write_config(backends=['spec', 'cpu'], default_backend='spec')
        r = b.submit(M, '--backend', 'pl')
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_RUN_NOT_STARTED'))['code'], 'BACKEND_NOT_ALLOWED')
        r = b.submit(M)
        self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_SUBMIT_RESULT'))['backend'], 'spec')

    def test_gates_on_every_backend(self):
        b = self.bench
        r = b.run('gates', '--backends', 'cpu,spec,pl')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = fields_of(marker(r.stdout, 'GATEWAY_GATES_PASS'))
        self.assertEqual((f['backends'], f['spec.device_calls'], f['pl.device_calls'], f['pl.irq_delta'],
                          f['pl.PL_hz']), ('cpu,spec,pl', '21', '21', '+21/+21', '199999998'))
        self.assertEqual(len([line for line in r.stdout.splitlines() if line.startswith('GATEWAY_GATE ')]), 3)
        argvs = [s['argv'] for s in b.spawns()]
        self.assertEqual(len(argvs), 3)
        for argv, backend in zip(argvs, ('cpu', 'spec', 'pl')):
            self.assertEqual(argv[argv.index('--backend') + 1], backend)
            self.assertEqual(argv[argv.index('--rows') + 1], '2048')
            self.assertEqual(argv[argv.index('--requests') + 1], '20')
            self.assertEqual('--permit-fd' in argv, backend == 'pl')
        public = b.public_records()
        self.assertEqual([(p['kind'], p['backend'], len(p['trace'])) for p in public],
                         [('gate', 'cpu', 20), ('gate', 'spec', 20), ('gate', 'pl', 20)])

    def test_expect_owner_create_before_the_pl_is_loaded(self):
        b = self.bench
        os.unlink(b.dma_node)
        r = b.run('gates', '--backends', 'pl', '--expect', 'OWNER_CREATE')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(marker(r.stdout, 'GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE'))
        exited = [e['data'] for e in events_of(r.stdout) if e['type'] == 'client.exited'][0]
        self.assertEqual(exited['stop'], 'OWNER_CREATE')
        r = b.submit(M, '--backend', 'pl', '--expect', 'OWNER_CREATE')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(marker(r.stdout, 'GATEWAY_PERMIT_ACCEPTED_OWNER_CREATE'))
        self.assertNotIn('result', [e['type'] for e in events_of(r.stdout)])
        self.assertZeroed(b.observed(), ['in.csv'])
        open(b.dma_node, 'w').close()
        r = b.run('gates', '--backends', 'pl', '--expect', 'OWNER_CREATE')
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_RUN_NOT_STARTED'))['code'], 'EXPECT_REQUIRES_NO_DEVICE')
        self.assertEqual(len(b.spawns()), 2)
        self.assertEqual(b.run('gates', '--backends', 'cpu', '--expect', 'OWNER_CREATE').returncode, 4)

    def test_throughput_window_with_the_layered_contract(self):
        b = self.bench
        r = b.run('throughput', '--backend', 'pl', '--seconds', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = fields_of(marker(r.stdout, 'GATEWAY_THROUGHPUT_PASS'))
        self.assertEqual((f['completed_requests'], f['device_calls'], f['irq_delta'], f['window_ms'], f['slots'],
                          f['inflight']), ('40', '41', '+41/+41', '1000', '8', '8'))
        argv = b.spawns()[0]['argv']
        for flag, value in (('--mode', 'pipeline'), ('--slots', '8'), ('--duplex', '1'), ('--hash-io', '1'),
                            ('--decrypt-thread', '1'), ('--inflight', '8'), ('--io-cpu', 'auto'),
                            ('--io-isolate', '0'), ('--window-ms', '1000'), ('--requests', '100000')):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        public = b.public_records()[0]
        self.assertEqual((public['kind'], public['trace_count'], public['flags']['slots']), ('throughput', 40, 8))
        self.assertEqual(b.run('throughput', '--seconds', '61').returncode, 4)

    def test_stop_signals_wait_for_the_client(self):
        b = self.bench
        b.behave(sleep_s=2.0)
        command = [sys.executable, '-I', '-B', GATEWAY, 'submit', '--config', b.cfg_path, '--genhlth', '3',
                   '--highbp', '1', '--bmi', '31.2', '--age', '9', '--backend', 'pl']
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not b.spawns() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(b.spawns(), 'the client did not start')
        started = time.time()
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2):
            proc.send_signal(number)
            time.sleep(0.05)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertGreater(time.time() - started, 1.0, 'the gateway returned before the client finished')
        self.assertIsNotNone(marker(out, 'GATEWAY_SUBMIT_RESULT'))
        self.assertIn('GATEWAY_STOP_REQUESTED signal=SIGTERM', out)
        self.assertEqual(err.count('GATEWAY_STOP_REQUESTED signal='), 6, err)
        self.assertEqual(b.signals(), [])
        self.assertZeroed(b.observed(), ['in.csv', 'score.txt'])
        self.assertEqual(os.listdir(b.workdir), [])
        self.assertEqual(tuple(runner.STOP_SIGNALS), ('SIGTERM', 'SIGINT', 'SIGHUP', 'SIGQUIT', 'SIGUSR1', 'SIGUSR2'))

    def test_preflight(self):
        b = self.bench
        openssl = shutil.which('openssl')
        if openssl is None:
            self.skipTest('openssl is not installed')
        for cert in ('ca.pem', 'client.pem'):
            subprocess.run([openssl, 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                            '-noenc', '-keyout', os.path.join(b.pki, 'client.key'), '-out', os.path.join(b.pki, cert),
                            '-subj', '/CN=edgefhe-test', '-days', '2'], check=True, capture_output=True)
        os.chmod(os.path.join(b.pki, 'client.key'), 0o600)
        with open(os.path.join(b.pki, 'server.pin'), 'w', encoding='ascii') as f:
            f.write(PEER + '\n')
        overlay = os.path.join(b.base, 'src.OVERLAY_RECORD.json')
        with open(overlay, 'w', encoding='utf-8') as f:
            json.dump({'staged_tree_id': hashlib.sha256(b'tree').hexdigest(), 'profile_hex': PROFILE,
                       'model_id': model.MODEL_SHA256}, f)
        b.write_config(overlay_record=overlay)
        r = b.run('preflight', '--backend', 'cpu')
        checks = {}
        for line in r.stdout.splitlines():
            if line.startswith('PREFLIGHT_'):
                verdict, name = line.split()[:2]
                checks[name] = verdict
        for name in ('uid', 'python', 'config_file', 'workdir_tmpfs', 'boot_id', 'binaries', 'overlay_record',
                     'model_and_presets', 'pki', 'pins', 'certificates'):
            self.assertEqual(checks.get(name), 'PREFLIGHT_OK', name + '\n' + r.stdout)
        if any(os.access(os.path.join(d, 'ip'), os.X_OK) for d in ('/usr/sbin', '/usr/bin', '/sbin', '/bin')):
            self.assertIsNotNone(marker(r.stdout, 'GATEWAY_PREFLIGHT_PASS'), r.stdout)
        else:
            self.assertEqual(checks['route'], 'PREFLIGHT_FAIL')
            self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_PREFLIGHT_FAIL'))['failed'], 'route')
        self.assertNotIn('dma', checks)
        r = b.run('preflight', '--backend', 'pl')
        self.assertIn('PREFLIGHT_FAIL dma', r.stdout)
        with open(os.path.join(b.pki, 'server.pin'), 'w', encoding='ascii') as f:
            f.write('0' * 63 + '1\n')
        self.assertIn('PREFLIGHT_FAIL pins', b.run('preflight', '--backend', 'cpu').stdout)

    def test_config_errors_exit_4(self):
        b = self.bench
        b.write_config(host='192.0.2.010')
        r = b.submit(M)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(fields_of(marker(r.stdout, 'GATEWAY_CONFIG_INVALID'))['code'], 'CONFIG_HOST')
        self.assertEqual(b.spawns(), [])


# --------------------------------------------------------------------------- permits from a test controller
def controller_main(spec_path):
    """Act as the client's direct parent with a deliberately altered permit (see Controller)."""
    spec = read_json(spec_path)
    cfg = config.load(spec['config'])
    mutation = spec['mutation']
    run_dir = spec['run_dir']
    os.mkdir(run_dir, 0o700)
    inp, out = os.path.join(run_dir, 'in.csv'), os.path.join(run_dir, 'score.txt')
    runner.write_private(inp, 'GenHlth,HighBP,BMI,Age\n3,1,31.2,9\n')
    contract = permit.invocation_contract(cfg, model.MODEL_ID, 1, 1, 0, permit.FORM_FLAGS, (inp, out))['contract']
    exe_dev, exe_ino = permit.executable_identity(cfg['client'])
    pid = os.getpid() + (1 if mutation == 'pid' else 0)
    text = permit.permit_text(permit.read_boot_id(), pid, exe_dev, exe_ino, contract)
    if mutation == 'flip':
        i = len(text) - 65
        text = text[:i] + ('1' if text[i] == '0' else '0') + text[i + 1:]
    path = os.path.join(run_dir, 'permit')
    fd = permit.write_permit(path, text)
    if mutation == 'mode0644':
        os.chmod(path, 0o644)
    elif mutation == 'link2':
        os.link(path, path + '.2')
    elif mutation == 'uid1000':
        os.chown(path, 1000, 1000)
    elif mutation == 'rdwr':
        os.close(fd)
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    argv = runner.Gateway(cfg).client_argv('pl', ['--mode', 'serial', '--input', inp, '--scores-output', out,
                                                  '--permit-fd', str(fd)])
    returncode, stdout, stderr, _ = runner.ClientProcess(argv, (fd,)).wait()
    os.close(fd)
    for name in os.listdir(run_dir):
        runner.wipe_file(os.path.join(run_dir, name))
    os.rmdir(run_dir)
    report, stop, _, _ = runner.parse_client_output(returncode, stdout, stderr)
    print(json.dumps({'returncode': returncode, 'stop': stop, 'status': report.get('status') if report else None,
                      'controller_pid': os.getpid()}))
    return 0


@unittest.skipUnless(LINUX_ROOT, 'needs root on Linux (run in the python:3.10-slim container)')
class Controller(unittest.TestCase):
    """The permit binds the contract, the file attributes and the parent: each alteration is refused by the client
    before it opens the device, with the code of invoke_permit.hpp."""

    def setUp(self):
        self.bench = Bench()

    def tearDown(self):
        self.bench.close()

    def control(self, mutation):
        spec = os.path.join(self.bench.base, 'controller-%s.json' % mutation)
        with open(spec, 'w', encoding='utf-8') as f:
            json.dump({'config': self.bench.cfg_path, 'mutation': mutation,
                       'run_dir': os.path.join(self.bench.base, 'controller-' + mutation)}, f)
        r = subprocess.run([sys.executable, '-I', '-B', os.path.abspath(__file__), 'controller', spec],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_matching_contract_is_accepted(self):
        out = self.control('none')
        self.assertEqual((out['returncode'], out['stop'], out['status']), (0, None, runner.PASS_STATUS['pl']))
        self.assertEqual(self.bench.spawns()[0]['ppid'], out['controller_pid'])

    def test_flipped_contract_byte(self):
        self.assertEqual(self.control('flip')['stop'], 'PL_PERMIT_INVOCATION_BINDING')

    def test_mode_0644(self):
        self.assertEqual(self.control('mode0644')['stop'], 'PL_PERMIT_IDENTITY')

    def test_other_alterations(self):
        for mutation, code in (('link2', 'PL_PERMIT_IDENTITY'), ('uid1000', 'PL_PERMIT_IDENTITY'),
                               ('rdwr', 'PL_PERMIT_IDENTITY'), ('pid', 'PL_PERMIT_INVOCATION_BINDING')):
            self.assertEqual(self.control(mutation)['stop'], code, mutation)
        self.assertEqual(len(self.bench.spawns()), 4)


# ------------------------------------------------------------------------------------------- in-process runs
@unittest.skipUnless(LINUX_ROOT, 'needs root on Linux (run in the python:3.10-slim container)')
class InProcess(unittest.TestCase):
    """The Gateway object as the HTTP API uses it (cpu backend: no permit, so PID 1 is acceptable here)."""

    def setUp(self):
        self.bench = Bench()
        self.gateway = runner.Gateway(self.bench.load())

    def tearDown(self):
        self.bench.close()

    def types(self, run):
        return [(e['type'], e['class']) for e in self.gateway.events.since(0, run=run)]

    def test_watchdog_reports_without_kill(self):
        self.bench.behave(sleep_s=1.2)
        self.gateway.overdue_s = 0.3
        started = time.monotonic()
        outcome = self.gateway.run_form(M, 'cpu')
        self.assertTrue(outcome['ok'], outcome)
        self.assertTrue(outcome['overdue'])
        self.assertEqual(outcome['returncode'], 0)
        self.assertGreaterEqual(time.monotonic() - started, 1.2)
        events = self.gateway.events.since(0, run=outcome['run'])
        overdue = [e for e in events if e['type'] == 'error' and e['data']['code'] == 'CLIENT_OVERDUE']
        self.assertEqual(len(overdue), 1)
        self.assertFalse(overdue[0]['data']['fatal'])
        exited = [e for e in events if e['type'] == 'client.exited'][0]
        self.assertLess(overdue[0]['seq'], exited['seq'])
        self.assertTrue(exited['data']['overdue'])
        self.assertEqual(outcome['result']['level'], 3)
        self.assertEqual(self.bench.signals(), [])

    def test_pid_1_cannot_issue_a_permit(self):
        self.gateway.getpid = lambda: 1
        with self.assertRaises(runner.GatewayError) as cm:
            self.gateway.run_form(M, 'pl')
        self.assertEqual(cm.exception.code, 'GATEWAY_PID1')
        self.assertEqual(self.bench.spawns(), [])

    def test_progress_hook_and_event_order(self):
        calls = []

        def factory(pid, emit, cfg):
            calls.append(pid)
            emit('progress.io', {'wchar': 0, 'rchar': 0})

            def stop():
                calls.append('stop')
            return stop
        self.gateway.progress = factory
        outcome = self.gateway.run_form(M, 'cpu')
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(self.types(outcome['run']),
                         [('form.accepted', 'public'), ('form.accepted', 'owner'), ('client.spawned', 'public'),
                          ('progress.io', 'public'), ('client.exited', 'public'), ('irq.delta', 'public'),
                          ('result', 'owner'), ('wiped', 'public')])
        spawned = [e for e in self.gateway.events.since(0) if e['type'] == 'client.spawned'][0]
        self.assertEqual(calls, [spawned['data']['pid'], 'stop'])

    def test_http_submit_flow(self):
        gw, b = self.gateway, self.bench
        status, body = gw.submit(J, 'cpu')
        self.assertEqual((status, body['code']), (400, 'BMI_ABOVE_MAX'))
        self.assertEqual(self.types(body['run']), [('form.refused', 'owner')])
        status, body = gw.submit(M, 'gpu')
        self.assertEqual((status, body['code']), (400, 'BACKEND_NOT_ALLOWED'))
        b.behave(sleep_s=0.6)
        status, body = gw.submit(M, 'cpu')
        self.assertEqual(status, 202, body)
        run = body['run']
        self.assertRegex(run, r'^r[0-9a-f]{12}$')
        status2, body2 = gw.submit(M, 'cpu')
        self.assertEqual((status2, body2['code']), (409, 'BUSY'))
        self.assertTrue(gw.status()['busy'])
        self.assertTrue(gw.wait_idle(30))
        events = gw.events.since(0, run=run)
        self.assertEqual(events[-1]['type'], 'wiped')
        self.assertEqual([e['data']['level'] for e in events if e['type'] == 'result'], [3])
        status = gw.status()
        self.assertFalse(status['busy'])
        self.assertEqual([p['id'] for p in status['form']['presets']], list('ACDEMFGHKJ'))
        self.assertEqual((status['sizes']['request_frame_bytes'], status['cloud']['host']), (393472, '127.0.0.1'))
        self.assertIs(status['retain_private'], False)
        gw.request_stop()
        status, body = gw.submit(M, 'cpu')
        self.assertEqual((status, body['code']), (503, 'GATEWAY_STOPPING'))
        self.assertEqual(len(b.spawns()), 1)

    def test_retained_private_record_is_reported(self):
        b = self.bench
        b.write_config(retain_private=True)
        gw = runner.Gateway(b.load())
        self.assertIs(gw.status()['retain_private'], True)
        outcome = gw.run_form(M, 'cpu')
        self.assertTrue(outcome['ok'], outcome)
        events = gw.events.since(0, run=outcome['run'])
        self.assertEqual(events[-1]['type'], 'wiped')
        self.assertIs(events[-1]['data']['retained_private'], True)
        self.assertTrue(os.path.isfile(os.path.join(b.workdir, 'private', outcome['run'] + '.json')))
        b.behave(stop='TLS_DEADLINE')           # no request reached the wire: nothing to disclose, nothing kept
        outcome = gw.run_form(M, 'cpu')
        self.assertEqual(outcome['code'], 'TLS_DEADLINE')
        last = gw.events.since(0, run=outcome['run'])[-1]
        self.assertIs(last['data']['retained_private'], False)
        error = [e['data'] for e in gw.events.since(0, run=outcome['run']) if e['type'] == 'error'][0]
        self.assertIn('（1,000 毫秒）', error['message'])         # the bench's timeout_ms, with a separator
        self.assertEqual(len(os.listdir(os.path.join(b.workdir, 'private'))), 1)

    def test_wiped_is_emitted_when_a_record_fails(self):
        """A private save that fails on a full tmpfs, or any unexpected error while writing the records, still ends
        the run with wiped; retained_private is then false and no partial private file is left."""
        b = self.bench
        b.write_config(retain_private=True)
        gw = runner.Gateway(b.load())
        real_save = gw.private.save

        def save_on_full_tmpfs(record):
            def full(fd, data):
                os.write(fd, data[:16])
                raise OSError(errno.ENOSPC, 'No space left on device')
            with unittest.mock.patch.object(records, '_write_all', full):
                return real_save(record)
        gw.private.save = save_on_full_tmpfs
        outcome = gw.run_form(M, 'cpu')
        self.assertTrue(outcome['ok'], outcome)
        self.assertIs(outcome['retained_private'], False)
        events = gw.events.since(0, run=outcome['run'])
        self.assertEqual(events[-1]['type'], 'wiped')
        self.assertIs(events[-1]['data']['retained_private'], False)
        errors = [(e['data']['code'], e['data']['fatal']) for e in events if e['type'] == 'error']
        self.assertEqual(errors, [('RECORD_WRITE', False)])
        self.assertEqual(os.listdir(os.path.join(b.workdir, 'private')), [])

        def unexpected(*args):
            raise RuntimeError('unexpected')
        gw.private.save = real_save
        gw._records = unexpected
        outcome = gw.run_form(M, 'cpu')
        events = gw.events.since(0, run=outcome['run'])
        self.assertEqual(events[-1]['type'], 'wiped')
        self.assertIs(events[-1]['data']['retained_private'], False)
        self.assertEqual([(e['data']['code'], e['data']['fatal']) for e in events if e['type'] == 'error'],
                         [('RECORD_WRITE', False)])
        self.assertEqual(sorted(events[-1]['data']['files']), ['in.csv', 'score.txt'])
        self.assertTrue(gw.wait_idle(0))
        self.assertFalse(gw.status()['busy'])
        self.assertEqual(self.bench.signals(), [])

    def test_a_stop_request_during_admission_starts_no_run(self):
        """correctness#4: request_stop() between the first check and _idle.clear() can no longer let a run start
        after the stopping caller's wait_idle() returned."""
        gw = self.gateway

        def getpid_while_stopping():
            gw.request_stop()
            self.assertTrue(gw.wait_idle(0))     # the stopping caller sees the gateway idle at this moment
            return 4242
        gw.getpid = getpid_while_stopping
        with self.assertRaises(runner.GatewayError) as cm:
            gw.run_form(M, 'pl')
        self.assertEqual(cm.exception.code, 'GATEWAY_STOPPING')
        self.assertTrue(gw.wait_idle(0))
        self.assertFalse(gw.status()['busy'])
        lock = runner.PlLock(gw.lock_path)
        self.assertTrue(lock.acquire(), 'the run lock was not released')
        lock.release()
        self.assertEqual(self.bench.spawns(), [])
        self.assertEqual(os.listdir(self.bench.workdir), [])

    def test_the_form_worker_is_never_a_daemon(self):
        """security#8: Gateway.submit itself creates a non-daemon worker, whatever thread calls it."""
        self.bench.behave(sleep_s=0.5)
        gw, box = self.gateway, {}
        caller = threading.Thread(target=lambda: box.update(reply=gw.submit(M, 'cpu')), daemon=True)
        caller.start()
        caller.join(30)
        status, body = box['reply']
        self.assertEqual(status, 202, body)
        workers = [t for t in threading.enumerate() if t.name == 'edgefhe-run-' + body['run']]
        self.assertEqual(len(workers), 1)
        self.assertFalse(workers[0].daemon)
        self.assertTrue(gw.wait_idle(60))

    def test_rehearsal_refuses_pl_once_a_board_marker_exists(self):
        """security#3: a rehearsal gateway never drives the PL, even when the node appeared after it started."""
        gw = self.gateway
        gw.getpid = lambda: 4242
        gw.rehearsal = True
        with self.assertRaises(runner.GatewayError) as cm:          # the bench's DMA node exists
            gw.run_form(M, 'pl')
        self.assertEqual((cm.exception.code, cm.exception.http_status), ('REHEARSAL_PL_ON_BOARD', 503))
        self.assertIn(self.bench.dma_node, cm.exception.message())
        self.assertTrue(gw.run_form(M, 'cpu')['ok'])
        self.assertEqual(len(self.bench.spawns()), 1)
        self.assertTrue(gw.wait_idle(0))

    def test_an_unsafe_lock_path_is_refused(self):
        gw = self.gateway
        target = os.path.join(self.bench.base, 'lock-target')
        open(target, 'w').close()
        gw.lock_path = os.path.join(self.bench.base, 'lock-link')
        os.symlink(target, gw.lock_path)
        with self.assertRaises(runner.GatewayError) as cm:
            gw.run_form(M, 'cpu')
        self.assertEqual(cm.exception.code, 'PL_LOCK_UNSAFE')
        os.unlink(gw.lock_path)
        os.mkfifo(gw.lock_path, 0o600)
        with self.assertRaises(runner.GatewayError) as cm:
            gw.run_form(M, 'cpu')
        self.assertEqual(cm.exception.code, 'PL_LOCK_UNSAFE')
        self.assertTrue(gw.wait_idle(0))
        self.assertFalse(gw.status()['busy'])
        self.assertEqual(self.bench.spawns(), [])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'controller':
        sys.exit(controller_main(sys.argv[2]))
    unittest.main(verbosity=2)
