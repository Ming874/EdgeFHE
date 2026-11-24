"""Build the demo overlay: a staged copy of v8b_rev5 that adds the CDC model, always outside the repository.

No byte under v8b_rev5/ changes. The overlay
   1. refuses an output directory DIR that exists or lies inside the repository, and an existing record;
   2. runs v8b_rev5/tools/verify_repo.py and requires REPO_VERIFY_PASS;
   3. requires the pinned base hashes of software/client/models/model_catalog.hpp and software/client/profile_id.hpp;
   4. copies the verified v8b_rev5 tree byte for byte (shutil.copytree; CRLF and binary files stay as they are);
   5. inserts demo/model/catalog_line.hpp before the final '};}' line of the staged catalogue, so that the CDC entry
      becomes catalogue()[3] (probe.cpp:11 and wire_selftest.cpp:29 use entries 0 and 1), and requires the pinned
      staged catalogue hash;
   6. adds demo/model/cdc-diabetes-lr4-v1.json as models/zoo19/models_release_v1/cdc-diabetes-lr4-v1.json;
   7. runs the staged tools/update_sums.py and tools/verify_repo.py (282 files);
   8. computes staged_tree_id = SHA-256 of the staged SHA256SUMS, which is the same on the PC, in the images and on
      the board whenever the inputs are the same;
   9. compares every result with demo/model/EXPECTED.json and writes the record DIR.OVERLAY_RECORD.json next to DIR
      (never inside it, so the staged tree still verifies);
  10. prints EDGEFHE_DEMO_OVERLAY_READY.
On a failure it prints EDGEFHE_DEMO_OVERLAY_FAIL <CODE>: <reason> as its last line, writes no record and removes
the directory it created (unless --keep-on-failure is given). --check builds the same tree in a temporary
directory, compares it with demo/model/EXPECTED.json, removes it and prints OVERLAY_CHECK_PASS (no git, no network).

The record holds no absolute path and no time stamp. Its model_id is the wire model_id (SHA-256 of the model JSON,
which EXPECTED.json calls model_json), and its model_name is the catalogue name.

usage (from the repository root; Python 3.10+ standard library only):
  python -B demo/overlay/make_overlay.py --repo . --out DIR [--commit SHA] [--keep-on-failure]
  python -B demo/overlay/make_overlay.py --repo . --check
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve()
TOOL = 'demo/overlay/make_overlay.py'
RECORD_SCHEMA = 'edgefhe.demo.overlay-record.v1'
EXPECTED_SCHEMA = 'edgefhe.demo.overlay-expected.v1'
RECORD_SUFFIX = '.OVERLAY_RECORD.json'
PROFILE_OPTION = 'P1'   # keep the profile id; the descriptor stays as it is (known deviation, see EXPECTED.json)

SUBPROJECT = 'v8b_rev5'
MODEL_DIR = 'demo/model'
MODEL_NAME = 'cdc-diabetes-lr4-v1'
MODEL_FILE = MODEL_NAME + '.json'
LINE_FILE = 'catalog_line.hpp'
EXPECTED_FILE = 'EXPECTED.json'

# Paths inside v8b_rev5, which is also the root of the staged tree.
CATALOGUE = 'software/client/models/model_catalog.hpp'
PROFILE_ID = 'software/client/profile_id.hpp'
DESCRIPTOR = 'software/client/PROFILE_DESCRIPTOR.json'
STAGED_MODEL = 'models/zoo19/models_release_v1/' + MODEL_FILE
SUMS = 'SHA256SUMS'
UPDATE_SUMS = 'tools/update_sums.py'
VERIFY = 'tools/verify_repo.py'
CATALOGUE_HEAD = (b'#pragma once', b'inline std::vector<Model> model_catalog(){return {')
CATALOGUE_TAIL = b'};}\n'

# Frozen identities, as in demo/model/convert_cdc_to_v8b.py. Another value means another V8b base or another model;
# the dissector, the gateway configuration and the permits change with it, so the overlay stops instead.
PINNED = {
    'catalogue_before': 'f66e4d760b659f2e44bb7fa96b144ab9febac65838a5f3e71113814cdd04850d',
    'profile_id_hpp': '01938b185c68248dc3fd8bb98a6a250dcd2fda0efacdc48bddce490aa24be482',
    'profile_hex': 'c01b69cf3da9fb260dbd4ac15080bd6c1c425d90bfb9015c8bacc16c061fa05c',
    'catalogue_after': '840bab207b516e3a36a3bd4407679684b73f2b040c960b4bb4791c5a334d3425',
    'model_id': '63cee36f9ac68b8e274dc45b4fa2fee0aeae5c4089b3d18fe51d7cf0c588c106',
}

# EXPECTED.json field -> record field; every one must be equal (same JSON type and value).
EXPECTED_FIELDS = (
    ('model_id', 'model_name'),
    ('model_json', 'model_id'),
    ('model_json_bytes', 'model_json_bytes'),
    ('model_json_staged_path', 'model_json_staged_path'),
    ('catalog_line', 'catalog_line'),
    ('catalog_line_bytes', 'catalog_line_bytes'),
    ('catalogue_path', 'catalogue_path'),
    ('catalogue_before', 'catalogue_before'),
    ('catalogue_after', 'catalogue_after'),
    ('catalogue_entries_before', 'catalogue_entries_before'),
    ('catalogue_index', 'catalogue_index'),
    ('profile', 'profile_hex'),
    ('profile_id_hpp', 'profile_id_hpp'),
    ('profile_descriptor', 'profile_descriptor'),
    ('base_tree_files', 'base_tree_files'),
    ('base_sums', 'base_sums'),
    ('staged_tree_files', 'staged_tree_files'),
    ('staged_tree_id', 'staged_tree_id'),
    ('changed', 'changed'),
    ('added', 'added'),
)
EXPECTED_FIXED = {'schema': EXPECTED_SCHEMA, 'profile_option': PROFILE_OPTION}
EXPECTED_TEXT = ('generator', 'profile_descriptor_note', 'staged_tree_id_rule')   # descriptive, not compared

COMMIT_RE = re.compile(r'[0-9A-Za-z][0-9A-Za-z._-]{0,63}')   # a git object name, or a label such as 'dev'
TOOL_TIMEOUT_S = 900   # verify_repo.py and update_sums.py hash about 10 MB; they never touch a device


class OverlayError(Exception):
    """A refusal. code is a stable upper-case word for scripts and tests."""

    def __init__(self, code, message):
        Exception.__init__(self, '%s: %s' % (code, message))
        self.code = code


def need(condition, code, message):
    if not condition:
        raise OverlayError(code, message)


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def absolute(path):
    """Absolute path with symbolic links resolved, also for a path that does not exist yet."""
    return Path(os.path.abspath(str(path))).resolve()


def inside(path, root):
    """True when the (resolved) path is root or lies below it."""
    return path == root or root in path.parents


def clip(text, limit=400):
    text = ' | '.join(line.strip() for line in str(text).splitlines() if line.strip())
    return text if len(text) <= limit else text[:limit] + '...'


def dump_json(obj):
    """Serialization of every JSON file of the demo: indent 2, UTF-8, LF."""
    return (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def same(a, b):
    return type(a) is type(b) and a == b


def show(value):
    text = json.dumps(value)
    return text if len(text) <= 80 else text[:80] + '...'


def remove_tree(path):
    """Remove a directory tree (clearing read-only attributes); True when nothing is left."""
    def retry(function, name, _info):
        try:
            os.chmod(name, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            function(name)
        except OSError:
            pass
    path = str(path)
    if not os.path.lexists(path):
        return True
    if os.path.isdir(path) and not os.path.islink(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    else:
        os.remove(path)
    return not os.path.lexists(path)


# ------------------------------------------------------------------------------------------------- manifests
def parse_sums(data, code):
    """The entries of a SHA256SUMS written by tools/update_sums.py: path -> lowercase SHA-256."""
    need(data.endswith(b'\n') and b'\r' not in data, code, 'SHA256SUMS must end with LF and contain no CR')
    entries = {}
    for line in data.decode('utf-8').split('\n')[:-1]:
        digest, sep, name = line.partition('  ')
        need(sep == '  ' and re.fullmatch('[0-9a-f]{64}', digest) is not None and name and name not in entries,
             code, 'malformed SHA256SUMS line: %r' % line[:120])
        entries[name] = digest
    return entries


def sums_text(entries):
    """The bytes tools/update_sums.py writes for this file set (sorted POSIX paths, two spaces, LF)."""
    return ''.join('%s  %s\n' % (entries[name], name) for name in sorted(entries)).encode('utf-8')


def manifest_diff(base, staged):
    """(changed, added, removed) path lists between two manifests, each sorted."""
    changed = sorted(name for name in set(base) & set(staged) if base[name] != staged[name])
    return changed, sorted(set(staged) - set(base)), sorted(set(base) - set(staged))


# ------------------------------------------------------------------------------------------------ the inputs
def run_python(script, cwd, code):
    """Run one of the subproject's own tools (isolated mode, no bytecode written)."""
    need(bool(sys.executable), code, 'no Python interpreter path (sys.executable is empty)')
    try:
        r = subprocess.run([sys.executable, '-I', '-B', str(script)], cwd=str(cwd), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TOOL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise OverlayError(code, 'cannot run %s: %s' % (script, e))
    return r.returncode, r.stdout.decode('utf-8', 'replace').strip(), r.stderr.decode('utf-8', 'replace').strip()


def verify_tree(root, code, label):
    """Run root/tools/verify_repo.py; returns (file count, its output line)."""
    rc, out, err = run_python(root / VERIFY, root, code)
    m = re.fullmatch(r'REPO_VERIFY_PASS (\d+) \[\]', out)
    need(rc == 0 and m is not None, code, '%s tools/verify_repo.py did not pass: %s' % (label, clip(out or err)))
    return int(m.group(1)), out


def update_sums(root):
    rc, out, err = run_python(root / UPDATE_SUMS, root, 'STAGED_SUMS')
    m = re.fullmatch(r'SHA256SUMS_UPDATED files=(\d+)', out)
    need(rc == 0 and m is not None, 'STAGED_SUMS', 'staged tools/update_sums.py failed: %s' % clip(out or err))
    return int(m.group(1))


def scan_tree(root):
    """Refuse what verify_repo.py does not see or follows: a .git entry and symbolic links."""
    need(not os.path.lexists(str(root / '.git')), 'BASE_TREE',
         'v8b_rev5 contains a .git entry, which verify_repo.py skips; the overlay copies only verified files')
    for folder, dirs, files in os.walk(str(root)):
        for name in dirs + files:
            path = os.path.join(folder, name)
            need(not os.path.islink(path), 'BASE_TREE',
                 'symbolic link in v8b_rev5: %s' % Path(path).relative_to(root).as_posix())


def profile_hex_of(data):
    found = re.findall(rb'profile_hex="([0-9a-f]{64})"', data)
    need(len(found) == 1, 'BASE_HASH', 'cannot read profile_hex from ' + PROFILE_ID)
    return found[0].decode('ascii')


def catalogue_entries(data, code):
    """Number of Model{...} lines of a catalogue with the layout of models/zoo19/qa_native_zoo.py."""
    need(data.endswith(CATALOGUE_TAIL) and b'\r' not in data, code,
         CATALOGUE + ' must end with the line "};}" and contain no CR')
    lines = data[:-1].split(b'\n')
    body = lines[2:-1]
    need(len(lines) >= 3 and tuple(lines[:2]) == CATALOGUE_HEAD and lines[-1] == CATALOGUE_TAIL[:-1]
         and all(line.startswith(b'Model{') and line.endswith(b'},') for line in body)
         and data.count(b'Model{') == len(body), code, 'unexpected layout of ' + CATALOGUE)
    return len(body)


def check_line(line, model_id, before):
    """The catalogue line: one ASCII Model{...}, line + LF that names the model JSON by its SHA-256."""
    need(line.startswith(b'Model{') and line.endswith(b'},\n') and line.count(b'\n') == 1 and b'\r' not in line
         and line.count(b'Model{') == 1 and max(line) < 0x80, 'CATALOGUE_LINE',
         'demo/model/%s must be one ASCII line Model{...}, followed by LF' % LINE_FILE)
    m = re.match(rb'Model\{"([A-Za-z0-9._-]+)","([0-9a-f]{64})",', line)
    need(m is not None and m.group(1) == MODEL_NAME.encode('ascii') and m.group(2) == model_id.encode('ascii'),
         'CATALOGUE_LINE', 'demo/model/%s must name %s with source_sha256 = SHA-256 of %s' % (
             LINE_FILE, MODEL_NAME, MODEL_FILE))
    need(('"%s"' % MODEL_NAME).encode('ascii') not in before and model_id.encode('ascii') not in before,
         'CATALOGUE_LINE', MODEL_NAME + ' is already in the base catalogue')


def load_expected(data):
    try:
        expected = json.loads(data.decode('utf-8'))
    except ValueError as e:
        raise OverlayError('EXPECTED_FORMAT', '%s/%s is not UTF-8 JSON: %s' % (MODEL_DIR, EXPECTED_FILE, e))
    need(isinstance(expected, dict), 'EXPECTED_FORMAT', '%s/%s must hold a JSON object' % (MODEL_DIR, EXPECTED_FILE))
    compared = [key for key, _ in EXPECTED_FIELDS]
    unknown = sorted(set(expected) - set(compared) - set(EXPECTED_FIXED) - set(EXPECTED_TEXT))
    missing = sorted((set(compared) | set(EXPECTED_FIXED)) - set(expected))
    need(not unknown and not missing, 'EXPECTED_FORMAT', '%s/%s: unknown fields %s, missing fields %s' % (
        MODEL_DIR, EXPECTED_FILE, unknown, missing))
    for key, value in EXPECTED_FIXED.items():
        need(same(expected[key], value), 'EXPECTED_FORMAT', '%s/%s: %s is %s; this overlay implements %s' % (
            MODEL_DIR, EXPECTED_FILE, key, show(expected[key]), show(value)))
    return expected


def compare_expected(record, expected):
    """The fields where the overlay differs from EXPECTED.json (empty when it matches)."""
    return ['%s (EXPECTED.json %s, overlay %s)' % (key, show(expected[key]), show(record[field]))
            for key, field in EXPECTED_FIELDS if not same(expected[key], record[field])]


def repo_commit(repo, given):
    """(repo_commit, source): the --commit value, else git HEAD (read only), else None."""
    if given is not None:
        need(COMMIT_RE.fullmatch(given) is not None, 'COMMIT_FORMAT',
             '--commit must match %s (a git object name or a label such as dev)' % COMMIT_RE.pattern)
        return given, 'argument'
    if not os.path.lexists(str(repo / '.git')):
        return None, 'unavailable'
    try:
        r = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', 'HEAD'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, 'unavailable'
    value = r.stdout.decode('ascii', 'replace').strip()
    if r.returncode == 0 and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', value):
        return value, 'git'
    return None, 'unavailable'


def check_target(repo, out, record_path):
    """Step 1: the output directory and its record lie outside the repository and do not exist yet."""
    roots = [repo, absolute(repo / SUBPROJECT), absolute(repo / MODEL_DIR)]   # also where links would lead
    for root in roots:
        for path in (out, record_path):
            need(not inside(path, root), 'OUT_INSIDE_REPO',
                 '%s lies inside %s; the overlay writes only outside the repository' % (path, root))
    need(not os.path.lexists(str(out)), 'OUT_EXISTS', 'output directory exists: %s (choose a new one)' % out)
    need(not os.path.lexists(str(record_path)), 'RECORD_EXISTS', 'record exists: %s (remove it first)' % record_path)


# ------------------------------------------------------------------------------------------------- the build
def build(repo, out, record_path, commit, commit_source, state, log):
    """Steps 2-9. state['out_created'] / state['record_created'] tell the caller what to remove on a failure."""
    v8 = repo / SUBPROJECT
    model_dir = repo / MODEL_DIR
    for path in (v8 / SUMS, v8 / VERIFY, v8 / UPDATE_SUMS, v8 / CATALOGUE, v8 / PROFILE_ID, v8 / DESCRIPTOR,
                 model_dir / LINE_FILE, model_dir / MODEL_FILE, model_dir / EXPECTED_FILE):
        need(path.is_file(), 'NOT_REPO_ROOT', 'missing %s (--repo must be the repository root: %s)' % (
            path.relative_to(repo).as_posix(), repo))
    expected_bytes = (model_dir / EXPECTED_FILE).read_bytes()
    expected = load_expected(expected_bytes)

    # (2) the base tree verifies against its own SHA256SUMS
    scan_tree(v8)
    base_files, base_verify = verify_tree(v8, 'BASE_VERIFY', 'v8b_rev5')
    base_sums = (v8 / SUMS).read_bytes()
    base = parse_sums(base_sums, 'BASE_VERIFY')
    need(len(base) == base_files and sums_text(base) == base_sums, 'BASE_VERIFY',
         'v8b_rev5/SHA256SUMS is not in the form tools/update_sums.py writes')
    log('OVERLAY_BASE_VERIFIED files=%d sums=%s' % (base_files, sha256_hex(base_sums)))

    # (3) the pinned base hashes
    before = (v8 / CATALOGUE).read_bytes()
    pid = (v8 / PROFILE_ID).read_bytes()
    descriptor = (v8 / DESCRIPTOR).read_bytes()
    for name, data, key in ((CATALOGUE, before, 'catalogue_before'), (PROFILE_ID, pid, 'profile_id_hpp')):
        need(sha256_hex(data) == PINNED[key] == base.get(name), 'BASE_HASH',
             'v8b_rev5/%s has SHA-256 %s; the overlay needs %s' % (name, sha256_hex(data), PINNED[key]))
    need(base.get(DESCRIPTOR) == sha256_hex(descriptor), 'BASE_HASH',
         'v8b_rev5/%s differs from SHA256SUMS' % DESCRIPTOR)
    profile_hex = profile_hex_of(pid)
    need(profile_hex == PINNED['profile_hex'], 'BASE_HASH', 'profile_hex is %s; the overlay keeps %s' % (
        profile_hex, PINNED['profile_hex']))
    need(STAGED_MODEL not in base, 'BASE_TREE', 'v8b_rev5/%s already exists' % STAGED_MODEL)
    entries = catalogue_entries(before, 'CATALOGUE_LAYOUT')

    # the inputs of demo/model
    line = (model_dir / LINE_FILE).read_bytes()
    model_bytes = (model_dir / MODEL_FILE).read_bytes()
    model_id = sha256_hex(model_bytes)
    need(model_id == PINNED['model_id'], 'MODEL_JSON', 'demo/model/%s has SHA-256 %s; the overlay needs %s' % (
        MODEL_FILE, model_id, PINNED['model_id']))
    try:
        model = json.loads(model_bytes.decode('utf-8'))
    except ValueError as e:
        raise OverlayError('MODEL_JSON', 'demo/model/%s is not UTF-8 JSON: %s' % (MODEL_FILE, e))
    need(isinstance(model, dict) and model.get('model_id') == MODEL_NAME, 'MODEL_JSON',
         'demo/model/%s does not describe %s' % (MODEL_FILE, MODEL_NAME))
    check_line(line, model_id, before)
    after = before[:-len(CATALOGUE_TAIL)] + line + CATALOGUE_TAIL
    need(sha256_hex(after) == PINNED['catalogue_after'], 'STAGED_CATALOGUE',
         'the catalogue with demo/model/%s has SHA-256 %s; the overlay needs %s' % (
             LINE_FILE, sha256_hex(after), PINNED['catalogue_after']))
    need(catalogue_entries(after, 'STAGED_CATALOGUE') == entries + 1, 'STAGED_CATALOGUE',
         'the staged catalogue must hold one entry more than the base catalogue')

    # (4) byte copy of the verified tree into a directory this run creates
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(str(out))
    except FileExistsError:
        raise OverlayError('OUT_EXISTS', 'output directory exists: %s (choose a new one)' % out)
    state['out_created'] = True
    shutil.copytree(str(v8), str(out), dirs_exist_ok=True)
    log('OVERLAY_COPIED out=%s' % out)

    # (5) the CDC line before the final '};}' line: catalogue()[entries]
    staged_catalogue = out / CATALOGUE
    need(staged_catalogue.read_bytes() == before, 'STAGED_COPY', 'the copied catalogue differs from the verified one')
    os.chmod(str(staged_catalogue), stat.S_IMODE(os.stat(str(staged_catalogue)).st_mode) | stat.S_IWUSR)
    staged_catalogue.write_bytes(after)
    need(sha256_hex(staged_catalogue.read_bytes()) == PINNED['catalogue_after'], 'STAGED_CATALOGUE',
         'the staged catalogue does not read back')
    log('OVERLAY_CATALOGUE_LINE_INSERTED index=%d catalogue=%s' % (entries, PINNED['catalogue_after']))

    # (6) the frozen model JSON
    staged_model = out / STAGED_MODEL
    need(staged_model.parent.is_dir(), 'STAGED_COPY', 'missing directory %s in the staged tree' % STAGED_MODEL)
    with open(str(staged_model), 'xb') as f:
        f.write(model_bytes)
    need(sha256_hex(staged_model.read_bytes()) == model_id, 'STAGED_COPY', 'the staged model JSON does not read back')
    log('OVERLAY_MODEL_ADDED path=%s model_id=%s' % (STAGED_MODEL, model_id))

    # (7) the staged tree seals and verifies itself with its own tools
    sums_files = update_sums(out)
    staged_files, staged_verify = verify_tree(out, 'STAGED_VERIFY', 'staged')
    staged_sums = (out / SUMS).read_bytes()
    staged = parse_sums(staged_sums, 'STAGED_SUMS')
    need(sums_files == staged_files == len(staged) == base_files + 1, 'STAGED_SUMS',
         'staged file counts: update_sums %d, verify_repo %d, SHA256SUMS %d; expected %d' % (
             sums_files, staged_files, len(staged), base_files + 1))
    changed, added, removed = manifest_diff(base, staged)
    need(changed == [CATALOGUE] and added == [STAGED_MODEL] and not removed, 'TREE_DIFF',
         'the staged tree must differ from v8b_rev5 only by the catalogue and the model: changed=%s added=%s '
         'removed=%s' % (changed, added, removed))
    predicted = dict(base)
    predicted[CATALOGUE] = PINNED['catalogue_after']
    predicted[STAGED_MODEL] = model_id
    need(sums_text(predicted) == staged_sums, 'STAGED_SUMS', 'the staged SHA256SUMS is not the predicted one')

    # (8) the identity of the staged tree
    staged_tree_id = sha256_hex(staged_sums)
    log('OVERLAY_STAGED_VERIFIED files=%d staged_tree_id=%s' % (staged_files, staged_tree_id))

    # (9) compare with EXPECTED.json, then write the record next to the staged tree
    record = {
        'schema': RECORD_SCHEMA,
        'tool': TOOL,
        'tool_sha256': sha256_hex(SCRIPT.read_bytes()),
        'repo_commit': commit,
        'repo_commit_source': commit_source,
        'expected_file': MODEL_DIR + '/' + EXPECTED_FILE,
        'expected_sha256': sha256_hex(expected_bytes),
        'base_tree_files': base_files,
        'base_sums': sha256_hex(base_sums),
        'base_verify': base_verify,
        'catalogue_path': CATALOGUE,
        'catalogue_before': sha256_hex(before),
        'catalogue_entries_before': entries,
        'catalogue_index': entries,
        'catalog_line': sha256_hex(line),
        'catalog_line_bytes': len(line),
        'catalogue_after': sha256_hex(after),
        'profile_option': PROFILE_OPTION,
        'profile_id_hpp': sha256_hex(pid),
        'profile_hex': profile_hex,
        'profile_descriptor': sha256_hex(descriptor),
        'model_name': MODEL_NAME,
        'model_id': model_id,
        'model_json_bytes': len(model_bytes),
        'model_json_staged_path': STAGED_MODEL,
        'staged_tree_files': staged_files,
        'staged_tree_id': staged_tree_id,
        'staged_tree_id_rule': 'SHA-256 of the SHA256SUMS that the staged tools/update_sums.py writes',
        'staged_verify': staged_verify,
        'changed': changed,
        'added': added,
        'removed': removed,
    }
    bad = compare_expected(record, expected)
    need(not bad, 'EXPECTED_MISMATCH', 'the overlay differs from %s/%s in %s' % (
        MODEL_DIR, EXPECTED_FILE, '; '.join(bad)))
    data = dump_json(record)
    try:
        handle = open(str(record_path), 'xb')
    except FileExistsError:
        raise OverlayError('RECORD_EXISTS', 'record exists: %s (remove it first)' % record_path)
    state['record_created'] = True
    with handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    need(record_path.read_bytes() == data, 'RECORD', 'the record does not read back: %s' % record_path)
    log('OVERLAY_RECORD_WRITTEN path=%s' % record_path)
    return record


def record_path_of(out):
    return out.with_name(out.name + RECORD_SUFFIX)


def discard(path):
    """Remove what this run created; a cleanup problem is reported, never raised over the original failure."""
    try:
        return remove_tree(path)
    except OSError:
        return False


def no_log(_text):
    pass


def print_log(text):
    print(text, flush=True)


def overlay_to(repo, out_arg, commit_arg, keep):
    out = absolute(out_arg)
    record_path = record_path_of(out)
    state = {'out_created': False, 'record_created': False}
    ok = False
    error = 'interrupted'
    try:
        check_target(repo, out, record_path)
        commit, source = repo_commit(repo, commit_arg)
        record = build(repo, out, record_path, commit, source, state, print_log)
        ok = True
    except OverlayError as e:
        error = str(e)
    except (OSError, ValueError) as e:
        error = 'IO: %s: %s' % (type(e).__name__, e)
    finally:
        if not ok:
            if state['record_created'] and not discard(record_path):
                print('OVERLAY_CLEANUP_INCOMPLETE record=%s (remove it by hand)' % record_path, flush=True)
            if state['out_created']:
                if keep:
                    print('OVERLAY_KEPT_ON_FAILURE out=%s (incomplete; no record was written)' % out, flush=True)
                elif discard(out):
                    print('OVERLAY_REMOVED_INCOMPLETE out=%s' % out, flush=True)
                else:
                    print('OVERLAY_CLEANUP_INCOMPLETE out=%s (remove it by hand)' % out, flush=True)
    if not ok:
        print('EDGEFHE_DEMO_OVERLAY_FAIL ' + error, flush=True)
        return 1
    print('EDGEFHE_DEMO_OVERLAY_READY out=%s staged_tree_id=%s catalogue=%s profile=%s changed=%d added=%d' % (
        out, record['staged_tree_id'], record['catalogue_after'][:8], record['profile_hex'][:8],
        len(record['changed']), len(record['added'])), flush=True)
    return 0


def check(repo):
    """Build in a fresh temporary directory, compare with EXPECTED.json, remove the directory."""
    tmp = None
    ok = False
    error = 'interrupted'
    try:
        tmp = absolute(tempfile.mkdtemp(prefix='edgefhe_overlay_check_'))
        out = tmp / 'src'
        record_path = record_path_of(out)
        check_target(repo, out, record_path)
        state = {'out_created': False, 'record_created': False}
        record = build(repo, out, record_path, None, 'check', state, no_log)
        need(json.loads(record_path.read_bytes().decode('utf-8')) == record, 'RECORD', 'the record does not read back')
        ok = True
    except OverlayError as e:
        error = str(e)
    except (OSError, ValueError) as e:
        error = 'IO: %s: %s' % (type(e).__name__, e)
    finally:
        if tmp is not None and not discard(tmp):
            print('OVERLAY_CLEANUP_INCOMPLETE tmp=%s (remove it by hand)' % tmp, flush=True)
    if not ok:
        print('OVERLAY_CHECK_FAIL ' + error, flush=True)
        return 1
    print('OVERLAY_CHECK_PASS staged_tree_id=%s' % record['staged_tree_id'], flush=True)
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors='backslashreplace')
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--repo', default=str(SCRIPT.parent.parent.parent),
                    help='repository root (default: two levels above this script directory)')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--out', metavar='DIR', help='staged tree to create; it must not exist and must lie outside '
                      'the repository; the record is written to DIR.OVERLAY_RECORD.json')
    mode.add_argument('--check', action='store_true',
                      help='build in a temporary directory, compare with demo/model/EXPECTED.json, remove it')
    ap.add_argument('--commit', metavar='SHA', help='repository commit for the record (default: git HEAD if available)')
    ap.add_argument('--keep-on-failure', action='store_true', help='keep an incomplete staged tree for inspection')
    a = ap.parse_args(argv)
    if a.check and (a.commit is not None or a.keep_on_failure):
        ap.error('--commit and --keep-on-failure apply only with --out')
    repo = absolute(a.repo)
    if a.check:
        return check(repo)
    return overlay_to(repo, a.out, a.commit, a.keep_on_failure)


if __name__ == '__main__':
    sys.exit(main())
