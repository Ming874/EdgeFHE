"""Tests of demo/overlay/make_overlay.py: determinism, refusals and no write inside the repository.

Every overlay is built in a temporary directory outside the repository and removed afterwards. The tampering tests
run against copies of v8b_rev5 and demo/model in that temporary directory, never against the repository itself.

usage (from the repository root; Python 3.10+ standard library only):
  python -B demo/overlay/test_overlay.py
"""
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # before loading make_overlay, so the repository stays clean

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPT = HERE / 'make_overlay.py'
V8 = REPO / 'v8b_rev5'
MODEL_DIR = REPO / 'demo' / 'model'
MODEL_INPUTS = ('catalog_line.hpp', 'cdc-diabetes-lr4-v1.json', 'EXPECTED.json')
CATALOGUE = 'software/client/models/model_catalog.hpp'
STAGED_MODEL = 'models/zoo19/models_release_v1/cdc-diabetes-lr4-v1.json'
RECORD_SUFFIX = '.OVERLAY_RECORD.json'
WATCHED = (V8, MODEL_DIR, HERE)   # what the overlay reads, and its own directory
TIMEOUT_S = 900
READY_RE = re.compile(r'EDGEFHE_DEMO_OVERLAY_READY out=(.+) staged_tree_id=([0-9a-f]{64}) catalogue=([0-9a-f]{8}) '
                      r'profile=([0-9a-f]{8}) changed=(\d+) added=(\d+)')
SERIAL = itertools.count(1)


def load_module():
    spec = importlib.util.spec_from_file_location('make_overlay_under_test', str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MO = load_module()
EXPECTED = json.loads((MODEL_DIR / 'EXPECTED.json').read_bytes().decode('utf-8'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree_hashes(root):
    """Relative POSIX path -> SHA-256 of every file below root."""
    found = {}
    for folder, _dirs, files in os.walk(str(root)):
        for name in files:
            path = Path(folder) / name
            found[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    return found


def snapshot():
    return {str(root): tree_hashes(root) for root in WATCHED}


def pycache_dirs():
    return sorted(str(p) for root in WATCHED for p in root.rglob('__pycache__'))


SNAPSHOT = snapshot()


def new_temp(label):
    path = MO.absolute(tempfile.mkdtemp(prefix='edgefhe_t2_%s_' % label))
    assert not MO.inside(path, MO.absolute(REPO)), 'the temporary directory lies inside the repository'
    return path


def decode(data):
    return data.decode('utf-8', 'replace').replace('\r\n', '\n')   # a Windows console stream writes CRLF


def run_overlay(args, repo=REPO, env=None, cwd=None):
    command = [sys.executable, '-I', '-B', str(SCRIPT), '--repo', str(repo)] + [str(a) for a in args]
    r = subprocess.run(command, cwd=None if cwd is None else str(cwd), env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_S)
    return r.returncode, decode(r.stdout), decode(r.stderr)


def run_tool(script):
    r = subprocess.run([sys.executable, '-I', '-B', str(script)], cwd=str(script.parent.parent),
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_S)
    return r.returncode, decode(r.stdout).strip()


def temp_env(tmp):
    env = dict(os.environ)
    for name in ('TMPDIR', 'TEMP', 'TMP'):
        env[name] = str(tmp)
    return env


def last_line(text):
    lines = text.strip().splitlines()
    return lines[-1] if lines else ''


class UnitTests(unittest.TestCase):
    def test_sums_round_trip(self):
        data = (V8 / 'SHA256SUMS').read_bytes()
        entries = MO.parse_sums(data, 'X')
        self.assertEqual(len(entries), EXPECTED['base_tree_files'])
        self.assertEqual(MO.sums_text(entries), data)
        self.assertEqual(sha(data), EXPECTED['base_sums'])

    def test_parse_sums_refuses_malformed(self):
        good = ('%s  a.txt\n' % ('0' * 64)).encode('ascii')
        self.assertEqual(MO.parse_sums(good, 'X'), {'a.txt': '0' * 64})
        for bad in (good.replace(b'\n', b'\r\n'), good[:-1], good + good, good.replace(b'  ', b' '),
                    good.replace(b'0', b'A')):
            with self.assertRaises(MO.OverlayError):
                MO.parse_sums(bad, 'X')

    def test_manifest_diff(self):
        base = {'a': '1', 'b': '2', 'c': '3'}
        self.assertEqual(MO.manifest_diff(base, {'a': '1', 'b': '9', 'd': '4'}), (['b'], ['d'], ['c']))
        self.assertEqual(MO.manifest_diff(base, dict(base)), ([], [], []))

    def test_inside(self):
        root = MO.absolute(REPO)
        self.assertTrue(MO.inside(root, root))
        self.assertTrue(MO.inside(MO.absolute(REPO / 'demo' / 'x'), root))
        self.assertTrue(MO.inside(MO.absolute(str(REPO / 'demo') + os.sep + '..' + os.sep + 'x'), root))
        self.assertFalse(MO.inside(root.parent, root))
        self.assertFalse(MO.inside(root.with_name(root.name + '_sibling'), root))

    def test_commit_pattern(self):
        for good in ('dev', '33d448a', 'a' * 40, '0' * 64, 'v1.2_rc-3'):
            self.assertIsNotNone(MO.COMMIT_RE.fullmatch(good), good)
        for bad in ('', 'a b', '-x', 'x;y', '$(id)', chr(0xe9), 'a' * 65, '.x'):
            self.assertIsNone(MO.COMMIT_RE.fullmatch(bad), bad)

    def test_catalogue_and_line(self):
        before = (V8 / CATALOGUE).read_bytes()
        line = (MODEL_DIR / 'catalog_line.hpp').read_bytes()
        self.assertEqual(MO.catalogue_entries(before, 'X'), EXPECTED['catalogue_entries_before'])
        MO.check_line(line, EXPECTED['model_json'], before)
        for bad in (line.replace(b'},\n', b'},\r\n'), line + line, line[:-1], line.replace(b'"cdc-', b'"cdx-'),
                    line.replace(EXPECTED['model_json'][:8].encode('ascii'), b'00000000')):
            with self.assertRaises(MO.OverlayError):
                MO.check_line(bad, EXPECTED['model_json'], before)
        with self.assertRaises(MO.OverlayError):    # already present in the catalogue
            MO.check_line(line, EXPECTED['model_json'], before[:-4] + line + before[-4:])

    def test_compare_expected(self):
        record = dict((field, EXPECTED[key]) for key, field in MO.EXPECTED_FIELDS)
        self.assertEqual(MO.compare_expected(record, EXPECTED), [])
        changed = dict(record, staged_tree_id='0' * 64)
        self.assertEqual(len(MO.compare_expected(changed, EXPECTED)), 1)
        self.assertIn('staged_tree_id', MO.compare_expected(changed, EXPECTED)[0])
        self.assertEqual(len(MO.compare_expected(dict(record, catalogue_index=3.0), EXPECTED)), 1)   # type counts

    def test_expected_file_is_accepted(self):
        self.assertEqual(MO.load_expected((MODEL_DIR / 'EXPECTED.json').read_bytes()), EXPECTED)
        for key in ('staged_tree_id', 'schema'):
            with self.assertRaises(MO.OverlayError):
                MO.load_expected(MO.dump_json(dict((k, v) for k, v in EXPECTED.items() if k != key)))
        with self.assertRaises(MO.OverlayError):
            MO.load_expected(MO.dump_json(dict(EXPECTED, extra_field=1)))
        with self.assertRaises(MO.OverlayError):
            MO.load_expected(MO.dump_json(dict(EXPECTED, profile_option='P2b')))


class RealRepoOverlayTests(unittest.TestCase):
    """Two overlays of the repository itself give the same bytes and the staged_tree_id of EXPECTED.json."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = new_temp('real')
        cls.runs = []
        for i in (1, 2):
            out = cls.tmp / ('run%d' % i) / 'src'
            rc, stdout, stderr = run_overlay(['--out', out, '--commit', 'dev'])
            cls.runs.append({'out': out, 'rc': rc, 'stdout': stdout, 'stderr': stderr})

    @classmethod
    def tearDownClass(cls):
        MO.remove_tree(cls.tmp)

    def setUp(self):
        for run in self.runs:
            self.assertEqual(run['rc'], 0, run['stdout'] + run['stderr'])

    def test_ready_line(self):
        for run in self.runs:
            m = READY_RE.fullmatch(last_line(run['stdout']))
            self.assertIsNotNone(m, run['stdout'])
            self.assertEqual(m.group(1), str(run['out']))
            self.assertEqual(m.group(2), EXPECTED['staged_tree_id'])
            self.assertEqual(m.groups()[2:], (EXPECTED['catalogue_after'][:8], EXPECTED['profile'][:8], '1', '1'))
            self.assertEqual(run['stderr'], '')

    def test_two_runs_give_the_same_tree_and_record(self):
        first, second = (tree_hashes(run['out']) for run in self.runs)
        self.assertEqual(first, second)
        self.assertEqual(sha((self.runs[0]['out'] / 'SHA256SUMS').read_bytes()), EXPECTED['staged_tree_id'])
        records = [Path(str(run['out']) + RECORD_SUFFIX).read_bytes() for run in self.runs]
        self.assertEqual(records[0], records[1])

    def test_staged_tree_is_base_plus_catalogue_line_plus_model(self):
        base = MO.parse_sums((V8 / 'SHA256SUMS').read_bytes(), 'X')
        staged = tree_hashes(self.runs[0]['out'])
        self.assertEqual(set(staged), set(base) | {'SHA256SUMS', STAGED_MODEL})
        self.assertEqual(len(staged) - 1, EXPECTED['staged_tree_files'])
        for name, digest in base.items():
            if name != CATALOGUE:
                self.assertEqual(staged[name], digest, name)
        self.assertEqual(staged[CATALOGUE], EXPECTED['catalogue_after'])
        self.assertEqual(staged[STAGED_MODEL], EXPECTED['model_json'])
        self.assertEqual((self.runs[0]['out'] / STAGED_MODEL).read_bytes(),
                         (MODEL_DIR / 'cdc-diabetes-lr4-v1.json').read_bytes())

    def test_crlf_and_binary_bytes_preserved(self):
        crlf = [name for name in MO.parse_sums((V8 / 'SHA256SUMS').read_bytes(), 'X')
                if b'\r\n' in (V8 / name).read_bytes()]
        self.assertTrue(crlf, 'the base tree has no file with CRLF bytes to check')
        for name in crlf:
            self.assertEqual((self.runs[0]['out'] / name).read_bytes(), (V8 / name).read_bytes(), name)

    def test_cdc_entry_is_catalogue_index_3(self):
        before = (V8 / CATALOGUE).read_bytes().split(b'\n')
        after = (self.runs[0]['out'] / CATALOGUE).read_bytes().split(b'\n')
        line = (MODEL_DIR / 'catalog_line.hpp').read_bytes()
        # pragma, opening line and entries 0-2 (probe.cpp:11 and wire_selftest.cpp:29 use entries 0 and 1)
        self.assertEqual(after[:5], before[:5])
        self.assertEqual(after[5] + b'\n', line)
        self.assertEqual(after[6:], [b'};}', b''])
        body = [entry for entry in after if entry.startswith(b'Model{')]
        self.assertEqual(len(body), 4)
        self.assertTrue(body[EXPECTED['catalogue_index']].startswith(b'Model{"cdc-diabetes-lr4-v1","63cee36f'))

    def test_staged_tools_verify_the_staged_tree(self):
        rc, out = run_tool(self.runs[0]['out'] / 'tools' / 'verify_repo.py')
        self.assertEqual((rc, out), (0, 'REPO_VERIFY_PASS %d []' % EXPECTED['staged_tree_files']))
        self.assertEqual(list(self.runs[0]['out'].rglob('__pycache__')), [])

    def test_record_next_to_the_staged_tree(self):
        out = self.runs[0]['out']
        path = Path(str(out) + RECORD_SUFFIX)
        self.assertEqual(path.parent, out.parent)
        self.assertFalse((out / ('src' + RECORD_SUFFIX)).exists())
        data = path.read_bytes()
        self.assertTrue(data.endswith(b'}\n') and b'\r' not in data)
        record = json.loads(data.decode('utf-8'))
        self.assertEqual(data, MO.dump_json(record))
        self.assertEqual(record['schema'], 'edgefhe.demo.overlay-record.v1')
        self.assertEqual((record['repo_commit'], record['repo_commit_source']), ('dev', 'argument'))
        self.assertEqual(record['staged_tree_id'], EXPECTED['staged_tree_id'])
        self.assertEqual(record['profile_hex'], EXPECTED['profile'])
        self.assertEqual(record['model_id'], EXPECTED['model_json'])
        self.assertEqual(record['model_name'], EXPECTED['model_id'])
        self.assertEqual(record['base_sums'], EXPECTED['base_sums'])
        self.assertEqual((record['catalogue_before'], record['catalogue_after']),
                         (EXPECTED['catalogue_before'], EXPECTED['catalogue_after']))
        self.assertEqual((record['changed'], record['added'], record['removed']),
                         (EXPECTED['changed'], EXPECTED['added'], []))
        self.assertEqual(record['tool_sha256'], sha(SCRIPT.read_bytes()))
        self.assertEqual(MO.compare_expected(record, EXPECTED), [])
        text = data.decode('utf-8')
        for local in (str(self.tmp), str(REPO), self.tmp.as_posix(), REPO.as_posix()):
            self.assertNotIn(local, text)
        self.assertNotRegex(text, r'[A-Za-z]:\\|"/(home|root|tmp|Users)/')

    def test_without_commit_uses_git_head_read_only(self):
        out = self.tmp / 'nocommit' / 'src'
        rc, stdout, stderr = run_overlay(['--out', out])
        self.assertEqual(rc, 0, stdout + stderr)
        record = json.loads(Path(str(out) + RECORD_SUFFIX).read_bytes().decode('utf-8'))
        if record['repo_commit_source'] == 'git':
            self.assertRegex(record['repo_commit'], '^[0-9a-f]{40}$')
        else:   # no git on this machine, or a checkout git does not trust
            self.assertEqual((record['repo_commit'], record['repo_commit_source']), (None, 'unavailable'))
        self.assertEqual(record['staged_tree_id'], EXPECTED['staged_tree_id'])


class RefusalTests(unittest.TestCase):
    """Refusals that happen before anything is written."""

    def setUp(self):
        self.tmp = new_temp('refuse')
        self.addCleanup(MO.remove_tree, self.tmp)

    def assert_refused(self, args, code, out=None, cwd=None, env=None, repo=REPO):
        rc, stdout, stderr = run_overlay(args, repo=repo, cwd=cwd, env=env)
        self.assertEqual(rc, 1, stdout + stderr)
        self.assertTrue(last_line(stdout).startswith('EDGEFHE_DEMO_OVERLAY_FAIL %s:' % code), stdout + stderr)
        self.assertNotIn('EDGEFHE_DEMO_OVERLAY_READY', stdout)
        self.assertNotIn('OVERLAY_COPIED', stdout)
        return stdout

    def test_refuses_out_inside_the_repository(self):
        probe = '_t2_overlay_refusal_probe_%d' % os.getpid()
        targets = [(REPO, None), (REPO / 'demo' / 'overlay' / probe, None), (V8 / 'software' / probe, None),
                   (V8 / probe, None), (Path(str(REPO / 'demo') + os.sep + '..' + os.sep + probe), None),
                   (Path('demo') / 'overlay' / probe, REPO), (Path('.'), REPO)]
        for target, cwd in targets:
            resolved = MO.absolute(target if cwd is None else Path(cwd) / target)
            record = str(resolved) + RECORD_SUFFIX
            existed, record_existed = os.path.lexists(str(resolved)), os.path.lexists(record)
            try:
                self.assert_refused(['--out', target, '--commit', 'dev'], 'OUT_INSIDE_REPO', cwd=cwd)
                self.assertEqual(os.path.lexists(str(resolved)), existed, str(target))
                self.assertEqual(os.path.lexists(record), record_existed, str(target))
            finally:   # remove only what a broken refusal would have created
                if not existed and os.path.lexists(str(resolved)):
                    MO.remove_tree(resolved)
                if not record_existed and os.path.lexists(record):
                    os.remove(record)

    def test_refuses_an_existing_out(self):
        empty = self.tmp / 'exists'
        empty.mkdir()
        self.assert_refused(['--out', empty], 'OUT_EXISTS')
        self.assertEqual(list(empty.iterdir()), [])
        self.assertFalse(os.path.lexists(str(empty) + RECORD_SUFFIX))
        plain = self.tmp / 'file'
        plain.write_bytes(b'keep\n')
        self.assert_refused(['--out', plain], 'OUT_EXISTS')
        self.assertEqual(plain.read_bytes(), b'keep\n')

    def test_refuses_an_existing_record(self):
        out = self.tmp / 'src'
        record = Path(str(out) + RECORD_SUFFIX)
        record.write_bytes(b'{}\n')
        self.assert_refused(['--out', out], 'RECORD_EXISTS')
        self.assertEqual(record.read_bytes(), b'{}\n')
        self.assertFalse(os.path.lexists(str(out)))

    def test_refuses_a_malformed_commit(self):
        for value in ('a b', 'x;y', '', '$(id)'):
            out = self.tmp / ('c%d' % next(SERIAL)) / 'src'
            self.assert_refused(['--out', out, '--commit=' + value], 'COMMIT_FORMAT')
            self.assertFalse(os.path.lexists(str(out.parent)))

    def test_usage_errors(self):
        for args in ([], ['--check', '--commit', 'dev'], ['--check', '--keep-on-failure'],
                     ['--check', '--out', self.tmp / 'x']):
            rc, stdout, stderr = run_overlay(args)
            self.assertEqual(rc, 2, stdout + stderr)
        self.assertEqual(list(self.tmp.iterdir()), [])


class CheckModeTests(unittest.TestCase):
    def test_check_passes_and_removes_its_temporary_tree(self):
        tmp = new_temp('check')
        self.addCleanup(MO.remove_tree, tmp)
        rc, stdout, stderr = run_overlay(['--check'], env=temp_env(tmp))
        want = 'OVERLAY_CHECK_PASS staged_tree_id=%s\n' % EXPECTED['staged_tree_id']
        self.assertEqual((rc, stdout, stderr), (0, want, ''))
        self.assertEqual(list(tmp.iterdir()), [])


class FakeRepoTests(unittest.TestCase):
    """Tampered inputs, on copies of v8b_rev5 and demo/model outside the repository."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = new_temp('fake')
        cls.pristine = cls.tmp / 'pristine'
        shutil.copytree(str(V8), str(cls.pristine / 'v8b_rev5'))
        (cls.pristine / 'demo' / 'model').mkdir(parents=True)
        for name in MODEL_INPUTS:
            shutil.copyfile(str(MODEL_DIR / name), str(cls.pristine / 'demo' / 'model' / name))

    @classmethod
    def tearDownClass(cls):
        MO.remove_tree(cls.tmp)

    def fake_repo(self):
        repo = self.tmp / ('repo%02d' % next(SERIAL))
        shutil.copytree(str(self.pristine), str(repo))
        return repo

    def out_dir(self):
        return self.tmp / ('out%02d' % next(SERIAL)) / 'src'

    @staticmethod
    def resum(repo):
        rc, out = run_tool(repo / 'v8b_rev5' / 'tools' / 'update_sums.py')
        assert rc == 0 and out.startswith('SHA256SUMS_UPDATED'), out

    @staticmethod
    def edit(path, old, new):
        data = path.read_bytes()
        assert data.count(old) >= 1, 'tamper target not found in %s' % path
        path.write_bytes(data.replace(old, new, 1))

    def assert_refused(self, repo, code, extra=(), keep=False):
        out = self.out_dir()
        rc, stdout, stderr = run_overlay(['--out', out] + list(extra), repo=repo)
        self.assertEqual(rc, 1, stdout + stderr)
        self.assertTrue(last_line(stdout).startswith('EDGEFHE_DEMO_OVERLAY_FAIL %s:' % code), stdout + stderr)
        self.assertNotIn('EDGEFHE_DEMO_OVERLAY_READY', stdout)
        self.assertFalse(os.path.lexists(str(out) + RECORD_SUFFIX), 'a refused overlay wrote a record')
        if not keep:
            self.assertFalse(os.path.lexists(str(out)), 'a refused overlay left its staged tree')
        return out, stdout

    def test_pristine_copy_builds_the_expected_tree(self):
        repo = self.fake_repo()
        out = self.out_dir()
        rc, stdout, stderr = run_overlay(['--out', out], repo=repo)
        self.assertEqual(rc, 0, stdout + stderr)
        self.assertEqual(READY_RE.fullmatch(last_line(stdout)).group(2), EXPECTED['staged_tree_id'])
        record = json.loads(Path(str(out) + RECORD_SUFFIX).read_bytes().decode('utf-8'))
        self.assertEqual((record['repo_commit'], record['repo_commit_source']), (None, 'unavailable'))   # no .git
        rc, stdout, stderr = run_overlay(['--check'], repo=repo)
        want = 'OVERLAY_CHECK_PASS staged_tree_id=' + EXPECTED['staged_tree_id']
        self.assertEqual((rc, last_line(stdout)), (0, want))

    def test_unsealed_change_fails_base_verification(self):
        repo = self.fake_repo()
        self.edit(repo / 'v8b_rev5' / 'README.md', b'#', b'##')
        self.assert_refused(repo, 'BASE_VERIFY')

    def test_stray_bytecode_fails_base_verification(self):
        repo = self.fake_repo()
        stray = repo / 'v8b_rev5' / 'tools' / '__pycache__'
        stray.mkdir()
        (stray / 'verify_repo.cpython-310.pyc').write_bytes(b'\0')
        self.assert_refused(repo, 'BASE_VERIFY')

    def test_resealed_catalogue_or_profile_change_is_refused(self):
        for name in (CATALOGUE, 'software/client/profile_id.hpp'):   # LF and CRLF files
            repo = self.fake_repo()
            self.edit(repo / 'v8b_rev5' / name, b'#pragma once', b'#pragma  once')
            self.resum(repo)
            self.assert_refused(repo, 'BASE_HASH')

    def test_resealed_other_change_differs_from_expected(self):
        repo = self.fake_repo()
        self.edit(repo / 'v8b_rev5' / 'README.md', b'#', b'##')
        self.resum(repo)
        _out, stdout = self.assert_refused(repo, 'EXPECTED_MISMATCH')
        self.assertIn('OVERLAY_REMOVED_INCOMPLETE', stdout)
        self.assertIn('base_sums', last_line(stdout))
        self.assertIn('staged_tree_id', last_line(stdout))
        out, stdout = self.assert_refused(repo, 'EXPECTED_MISMATCH', extra=['--keep-on-failure'], keep=True)
        self.assertIn('OVERLAY_KEPT_ON_FAILURE', stdout)
        self.assertTrue((out / STAGED_MODEL).is_file() and (out / 'SHA256SUMS').is_file())
        tmp = new_temp('checkfail')
        self.addCleanup(MO.remove_tree, tmp)
        rc, stdout, stderr = run_overlay(['--check'], repo=repo, env=temp_env(tmp))
        self.assertEqual(rc, 1, stdout + stderr)
        self.assertTrue(last_line(stdout).startswith('OVERLAY_CHECK_FAIL EXPECTED_MISMATCH:'), stdout)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_model_already_in_the_base_tree_is_refused(self):
        repo = self.fake_repo()
        shutil.copyfile(str(MODEL_DIR / 'cdc-diabetes-lr4-v1.json'), str(repo / 'v8b_rev5' / STAGED_MODEL))
        self.resum(repo)
        self.assert_refused(repo, 'BASE_TREE')

    def test_git_entry_in_the_base_tree_is_refused(self):
        repo = self.fake_repo()
        (repo / 'v8b_rev5' / '.git').mkdir()   # verify_repo.py would skip it
        self.assert_refused(repo, 'BASE_TREE')

    def test_link_in_the_base_tree_is_refused(self):
        repo = self.fake_repo()
        try:
            os.symlink(str(repo / 'v8b_rev5' / 'README.md'), str(repo / 'v8b_rev5' / 'docs' / 'link.md'))
        except (OSError, NotImplementedError):
            self.skipTest('creating a symbolic link needs a privilege on this system')
        self.assert_refused(repo, 'BASE_TREE')

    def test_tampered_catalogue_line_is_refused(self):
        cases = ((b'749408897LL', b'749408898LL', 'STAGED_CATALOGUE'), (b'},\n', b'},\r\n', 'CATALOGUE_LINE'),
                 (b'"cdc-diabetes-lr4-v1"', b'"cdc-diabetes-lr4-v2"', 'CATALOGUE_LINE'))
        for old, new, code in cases:
            repo = self.fake_repo()
            self.edit(repo / 'demo' / 'model' / 'catalog_line.hpp', old, new)
            self.assert_refused(repo, code)

    def test_tampered_model_json_is_refused(self):
        repo = self.fake_repo()
        self.edit(repo / 'demo' / 'model' / 'cdc-diabetes-lr4-v1.json', b'{', b'{ ')
        self.assert_refused(repo, 'MODEL_JSON')

    def test_tampered_expected_is_refused(self):
        cases = ((dict(EXPECTED, staged_tree_id='0' * 64), 'EXPECTED_MISMATCH'),
                 (dict(EXPECTED, added=[]), 'EXPECTED_MISMATCH'),
                 (dict(EXPECTED, extra_field=1), 'EXPECTED_FORMAT'),
                 (dict(EXPECTED, profile_option='P2b'), 'EXPECTED_FORMAT'))
        for expected, code in cases:
            repo = self.fake_repo()
            (repo / 'demo' / 'model' / 'EXPECTED.json').write_bytes(MO.dump_json(expected))
            self.assert_refused(repo, code)
        repo = self.fake_repo()
        (repo / 'demo' / 'model' / 'EXPECTED.json').write_bytes(b'{"schema": ')
        self.assert_refused(repo, 'EXPECTED_FORMAT')

    def test_missing_input_is_refused(self):
        repo = self.fake_repo()
        (repo / 'demo' / 'model' / 'catalog_line.hpp').unlink()
        self.assert_refused(repo, 'NOT_REPO_ROOT')

    def test_out_inside_the_given_repository_is_refused(self):
        repo = self.fake_repo()
        before = tree_hashes(repo)
        rc, stdout, stderr = run_overlay(['--out', repo / 'demo' / 'staged'], repo=repo)
        self.assertEqual(rc, 1, stdout + stderr)
        self.assertTrue(last_line(stdout).startswith('EDGEFHE_DEMO_OVERLAY_FAIL OUT_INSIDE_REPO:'), stdout)
        self.assertEqual(tree_hashes(repo), before)
        self.assertFalse((repo / 'demo' / 'staged').exists())

    def test_check_refuses_a_temporary_directory_inside_the_repository(self):
        repo = self.fake_repo()
        tmp = repo / 'demo' / 'tmp'
        tmp.mkdir()
        rc, stdout, stderr = run_overlay(['--check'], repo=repo, env=temp_env(tmp))
        self.assertEqual(rc, 1, stdout + stderr)
        self.assertTrue(last_line(stdout).startswith('OVERLAY_CHECK_FAIL OUT_INSIDE_REPO:'), stdout)
        self.assertEqual(list(tmp.iterdir()), [])


class ZRepositoryUnchangedTest(unittest.TestCase):
    """Runs last (classes run in name order): the overlays left no byte in the repository."""

    def test_repository_unchanged(self):
        self.assertEqual(snapshot(), SNAPSHOT)
        self.assertEqual(pycache_dirs(), [])
        want = 'REPO_VERIFY_PASS %d []' % EXPECTED['base_tree_files']
        self.assertEqual(run_tool(V8 / 'tools' / 'verify_repo.py'), (0, want))


def main():
    program = unittest.main(exit=False, verbosity=2)
    result = program.result
    if result.wasSuccessful():
        print('OVERLAY_TESTS_PASS tests=%d' % result.testsRun)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
