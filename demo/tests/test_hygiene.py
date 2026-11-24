#!/usr/bin/env python3
"""Hygiene checks for the demo/ tree: run them before every commit and before tools/update_manifest.py.

The repository is public, and CI checks every commit: tools/verify_repository.py on Python 3.10 (Python syntax,
Markdown links, private keys, release/SHA256SUMS) and gitleaks. tools/update_manifest.py hashes every inventory file
of the working tree, whether git tracks it or not. This test finds, with the Python 3.10 standard library only, what
would leak a secret, break those checks or make release/SHA256SUMS disagree with the commit:

  STRAY_FILE         a file that must not exist under demo/, even when .gitignore hides it: anything under a directory
                     named build, local, runs, __pycache__, node_modules, .venv*, .git, .next, .Xil or .vscode
                     (tools/inventory.py skips build and local), captures, logs, keys, certificates, signing requests,
                     pins, key stores, build output, archives, real configuration files and evidence outputs
  GITIGNORED         a file that git ignores although tools/inventory.py would hash it into release/SHA256SUMS
  NOT_IN_INVENTORY   a file that git would commit although tools/inventory.py leaves it out of release/SHA256SUMS
  UNTRACKED_OUTSIDE  an inventory file outside demo/ that git does not track (update_manifest.py would list it)
  SYMLINK            a symbolic link (verify_repository.py refuses links in the manifest)
  LARGE_FILE         a file larger than 2 MiB
  ENCODING           a text file that is not UTF-8, starts with a BOM or contains a CR byte (.gitattributes stores the
                     bytes as written, and a CR breaks a shell script in the containers and on the board)
  PRIVATE_KEY        a PEM private-key header, in any file under demo/ (ignored ones included)
  PEM_BLOCK          a complete PEM block: the certificates and requests of the demo PKI stay outside the repository
  SECRET_VALUE       what the gitleaks rule generic-api-key reports: a value of 10 to 150 characters with a Shannon
                     entropy of at least 3.5, assigned to an identifier that contains access, auth, api, credential,
                     creds, key, passwd, password, secret or token (with the allowlist of that rule)
  HEX64_SECRET       a 64-digit hexadecimal value on a line where an identifier contains key, token, secret, api, auth,
                     passwd, credential or creds
  REAL_IP            an IPv4 literal other than 0.0.0.0, 127.0.0.0/8, the RFC 5737 documentation ranges
                     (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), multicast and 255.255.255.255
  LOCAL_PATH         a path of a particular PC: a Windows user profile, AppData, a checkout under GitHub, a scratch
                     directory
  PRIVATE_LINK       a link into Notion, Google Drive or Docs, claude.ai, Dropbox, OneDrive or SharePoint, or a Slack or
                     Discord webhook
  MD_LINK            a relative Markdown link of demo/**/*.md that does not resolve inside the repository (the same
                     parsing as verify_repository.py)
  PY310_SYNTAX       Python that Python 3.10 cannot parse: ast.parse with feature_version (3, 10), and on Python 3.12
                     or later also the f-string forms of PEP 701 that 3.10 rejects (a reused enclosing quote, a
                     backslash, a '#', or a single-quoted f-string over several lines)
  PY310_API          a standard-library name that Python 3.10 lacks (datetime.UTC, tomllib, ExceptionGroup,
                     typing.Self, enum.StrEnum and similar)
  STDLIB_ONLY        an import outside the standard library in board-side or container-side code: demo/edge,
                     demo/model, demo/overlay, demo/owner/serial_relay.py, demo/sim/drive_presets.py and this test
  V8B_CACHE          a __pycache__ directory or .pyc file inside v8b_rev5 (verify_repo.py and the overlay refuse it)
  CONFIG             demo/.gitignore, demo/.editorconfig or demo/README.md lacks a required rule or is missing
  DETECTOR_SELFTEST  a detector of this test misses a built-in positive sample or flags a negative one

git is used read-only when it is available (git check-ignore --no-index and git ls-files, with --no-optional-locks);
without git, the ignore rules of demo/.gitignore are applied directly and UNTRACKED_OUTSIDE is not checked.

usage: python -B demo/tests/test_hygiene.py [--repo ROOT]
Output: one line 'HYGIENE_FINDING <CHECK> <path>[:<line>] <zh-TW reason>' per finding, then
'HYGIENE_PASS files=N text=N python=N markdown_links=N git=yes|no' (exit 0) or
'HYGIENE_FAIL findings=N checks=A,B' (exit 1). The test reads files only and writes nothing.
"""
import argparse
import ast
import fnmatch
import hashlib
import importlib.util
import io
import ipaddress
import math
import os
import re
import shutil
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

sys.dont_write_bytecode = True  # before tools/inventory.py is imported, so the tree stays clean

MAX_BYTES = 2 * 1024 * 1024
BINARY_SUFFIXES = frozenset(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp', '.pdf', '.woff', '.woff2', '.ttf'))
INVENTORY_SKIPPED_HINT = '名為 build 或 local 的目錄會被 tools/inventory.py 略過，git 與 release/SHA256SUMS 因此不一致'
STRAY_DIRS = {
    'build': INVENTORY_SKIPPED_HINT,
    'local': INVENTORY_SKIPPED_HINT,
    'runs': '執行紀錄與證據應放在 repository 之外',
    '__pycache__': 'Python 快取；所有 Python 程式都應以 python -B 執行',
    'node_modules': '本機安裝的相依套件',
    '.next': '本機建置產物',
    '.Xil': '本機建置產物',
    '.vscode': '編輯器設定',
    '.git': '巢狀的 git 目錄',
}
STRAY_SUFFIXES = {
    '.pcap': '封包擷取檔', '.pcapng': '封包擷取檔',
    '.log': '紀錄檔（TLS 金鑰紀錄亦屬此類）',
    '.key': '金鑰或憑證檔', '.pem': '金鑰或憑證檔', '.csr': '憑證簽署請求', '.srl': 'CA 序號檔',
    '.pin': '憑證釘選值檔', '.p12': '金鑰庫', '.pfx': '金鑰庫', '.jks': '金鑰庫',
    '.crt': '憑證檔', '.cer': '憑證檔', '.der': '憑證檔',
    '.pyc': 'Python 快取', '.pyo': 'Python 快取',
    '.o': '建置產物', '.obj': '建置產物', '.a': '建置產物', '.so': '建置產物', '.dll': '建置產物',
    '.exe': '建置產物', '.ko': '建置產物', '.mod': '建置產物', '.class': '建置產物',
    '.jou': '建置產物', '.str': '建置產物', '.tsbuildinfo': '建置產物',
    '.tar': '封存檔或映像備份', '.tgz': '封存檔', '.gz': '封存檔', '.zip': '封存檔', '.7z': '封存檔',
    '.jsonl': '公開紀錄或證據資料',
}
STRAY_NAMES = {
    'gateway.json': '實際的閘道設定檔應位於 /etc/edgefhe-demo',
    'console.json': '實際的主控台設定檔應位於 repository 之外',
    'join_config.json': '本機設定檔', '.env': '本機環境設定',
    'id_rsa': 'SSH 私鑰', 'id_ecdsa': 'SSH 私鑰', 'id_ed25519': 'SSH 私鑰',
    'capture.sha256': '證據資料夾的封存紀錄', 'capinfos.txt': '證據資料夾的擷取資訊',
    'verify_report.json': '證據資料夾的驗證報告', 'MANIFEST.sha256': '證據資料夾的清單',
    'probe.csv': '能耗探針的量測資料', 'BUNDLE_MANIFEST.json': '上板套件的清單',
}
STRAY_NAME_SUFFIXES = {'.OVERLAY_RECORD.json': '疊加程式的紀錄應位於 repository 之外'}
GITIGNORE_REQUIRED = ('local/', 'build/', 'runs/', '__pycache__/', '*.pyc', '*.pcap', '*.pcapng', '*.log',
                      '*keys*.log', '*.key', '*.pem', '*.csr', '*.srl', '*.pin')
STDLIB_ONLY_PATHS = ('demo/edge/', 'demo/model/', 'demo/overlay/', 'demo/owner/serial_relay.py',
                     'demo/sim/drive_presets.py', 'demo/tests/')
# Standard-library modules that Python 3.10 has although newer versions removed them, and the reverse.
STDLIB_REMOVED_AFTER_310 = frozenset((
    'asynchat', 'asyncore', 'distutils', 'imp', 'smtpd', 'aifc', 'audioop', 'cgi', 'cgitb', 'chunk', 'crypt',
    'imghdr', 'mailcap', 'msilib', 'nis', 'nntplib', 'ossaudiodev', 'pipes', 'sndhdr', 'spwd', 'sunau', 'telnetlib',
    'uu', 'xdrlib', 'lib2to3'))
STDLIB_ADDED_AFTER_310 = frozenset(('tomllib',))
PY311_NAMES = {  # module -> names that Python 3.10 lacks
    'datetime': {'UTC'},
    'typing': {'Self', 'LiteralString', 'Never', 'assert_never', 'assert_type', 'reveal_type', 'Required',
               'NotRequired', 'Unpack', 'TypeVarTuple', 'dataclass_transform', 'override', 'TypeAliasType',
               'get_overloads', 'clear_overloads', 'ReadOnly', 'TypeIs'},
    'enum': {'StrEnum', 'ReprEnum', 'verify', 'EnumCheck', 'FlagBoundary', 'member', 'nonmember', 'global_enum'},
    'asyncio': {'TaskGroup', 'timeout', 'timeout_at', 'Runner', 'Barrier'},
    'hashlib': {'file_digest'},
    'contextlib': {'chdir'},
    'itertools': {'batched'},
    'math': {'cbrt', 'exp2', 'sumprod'},
    'operator': {'call'},
    'sys': {'exception', 'monitoring'},
    'os': {'process_cpu_count'},
    'logging': {'getLevelNamesMapping'},
}
PY311_BUILTINS = frozenset(('ExceptionGroup', 'BaseExceptionGroup'))

A = re.ASCII  # Go's regexp classes (\w, \s, \d, \b) are ASCII-only; Python's are Unicode without this flag
HEX64 = re.compile(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])')
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]*')
SECRET_WORD = re.compile(r'(?i)key|token|secret|api|auth|passw|credential|creds')
# gitleaks v8.28.0, rule generic-api-key (cmd/generate/config/rules/generic.go): regex, entropy 3.5, allowlists.
# gitleaks applies it to the whole file content, so the separators may span a line break.
GENERIC_API_KEY = re.compile(
    r'(?i)[\w.-]{0,50}?(?:access|auth|(?-i:[Aa]pi|API)|credential|creds|key|passw(?:or)?d|secret|token)'
    r'(?:[ \t\w.-]{0,20})[\s\'"]{0,3}(?:=|>|:{1,3}=|\|\||:|=>|\?=|,)[\x60\'"\s=]{0,5}'
    r'([\w.=-]{10,150}|[a-z0-9][a-z0-9+/]{11,}={0,3})(?:[\x60\'"\s;]|\\[nr]|$)', A)
GENERIC_ALLOW_SECRET = re.compile(r'^[a-zA-Z_.-]+$')
GENERIC_ALLOW_MATCH = re.compile(
    r'(?i)(?:access(?:ibility|or)|access[_.-]?id|random[_.-]?access|api[_.-]?(?:id|name|version)|rapid|capital'
    r'|[a-z0-9-]*?api[a-z0-9-]*?:jar:|author|X-MS-Exchange-Organization-Auth|Authentication-Results'
    r'|(?:credentials?[_.-]?id|withCredentials)'
    r'|(?:bucket|foreign|hot|idx|natural|primary|pub(?:lic)?|schema|sequence)[_.-]?key|(?:turkey)'
    r'|key[_.-]?(?:alias|board|code|frame|id|length|mesh|name|pair|press(?:ed)?|ring|selector|signature|size'
    r'|stone|storetype|word|up|down|left|right)|key[_.-]?vault[_.-]?(?:id|name)|keyVaultToStoreSecrets'
    r'|key(?:store|tab)[_.-]?(?:file|path)|issuerkeyhash|(?-i:[DdMm]onkey|[DM]ONKEY)|keying'
    r'|(?:secret)[_.-]?(?:length|name|size)|UserSecretsId|(?:csrf)[_.-]?token'
    r'|(?:io\.jsonwebtoken[ \t]?:[ \t]?[\w-]+)|(?:api|credentials|token)[_.-]?(?:endpoint|ur[il])'
    r'|public[_.-]?token|(?:key|token)[_.-]?file|(?-i:<[A-Z]+_TOKEN>))', A)
PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z0-9 ]*' + 'PRIVATE ' + r'KEY(?: BLOCK)?-----')
PEM_BLOCK = re.compile(r'-----BEGIN ([A-Z0-9 ]{1,40})-----[ \t]*\r?\n(?:[A-Za-z0-9+/=]{16,}[ \t]*\r?\n)+'
                       r'[A-Za-z0-9+/=]*\r?\n?-----END \1-----')
IPV4 = re.compile(r'(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])', A)
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '0.0.0.0/32', '127.0.0.0/8', '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4',
    '255.255.255.255/32'))
SEP = r'(?:\\\\|\\|/)+'
LOCAL_PATHS = (
    (re.compile(r'(?i)(?<![A-Za-z0-9])[a-z]:' + SEP + r'users' + SEP + r'[^\\/\s\'"]+', A), 'Windows 使用者目錄'),
    (re.compile(r'(?i)(?<![\w.:])/(?:mnt/)?[a-z]/users/[^/\s\'"]+', A), 'Windows 使用者目錄'),
    (re.compile(r'(?<![\w.:])/Users/[A-Za-z][^/\s\'"]*/', A), 'macOS 使用者目錄'),
    (re.compile(r'(?i)(?<![A-Za-z0-9])AppData' + SEP + r'(?:Local|Roaming|LocalLow)\b', A), 'AppData 目錄'),
    (re.compile(r'(?i)(?<![A-Za-z0-9])[a-z]:' + SEP + r'GitHub' + SEP, A), '本機的 repository 位置'),
    (re.compile(r'(?i)(?<![\w.:])/[a-z]/GitHub/', A), '本機的 repository 位置'),
    (re.compile('(?i)' + 'scratch' + 'pad', A), '暫存工作目錄'),
)
PRIVATE_LINK = re.compile(
    r'(?i)\b(notion\.so|notion\.site|drive\.google\.com|docs\.google\.com|claude\.ai/(?:artifact|chat|project|share)'
    r'|dropbox\.com|1drv\.ms|onedrive\.live\.com|sharepoint\.com|hooks\.slack\.com'
    r'|discord(?:app)?\.com/api/webhooks)', A)
MD_LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
MD_FENCE = re.compile(r'```.*?```', re.S)


class Report:
    def __init__(self, root):
        self.root = root
        self.findings = []

    def add(self, check, path, line, reason):
        where = path if line is None else '%s:%d' % (path, line)
        self.findings.append((check, where, reason))


def entropy(text):
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def rel_posix(root, path):
    return os.path.relpath(path, root).replace(os.sep, '/')


# ----------------------------------------------------------------------------------------------- line detectors
def secret_values(text):
    """[(offset, match)] of gitleaks' generic-api-key rule in a text (a whole file, like gitleaks)."""
    found = []
    for m in GENERIC_API_KEY.finditer(text):
        value = m.group(1)
        if entropy(value) < 3.5 or GENERIC_ALLOW_SECRET.match(value) or GENERIC_ALLOW_MATCH.search(m.group(0)):
            continue
        found.append((m.start(), ' '.join(m.group(0).split())[:60]))
    return found


def hex64_secret(line):
    """The secret-like identifiers of a line that also carries a 64-digit hex value."""
    if not HEX64.search(line):
        return []
    words = [w for w in IDENTIFIER.findall(HEX64.sub(' ', line)) if SECRET_WORD.search(w)]
    return [w for w in words if not GENERIC_ALLOW_MATCH.search(w)]


def real_ips(line):
    found = []
    for m in IPV4.finditer(line):
        try:
            ip = ipaddress.IPv4Address(m.group(1))
        except ValueError:
            continue
        if not any(ip in net for net in ALLOWED_NETWORKS):
            found.append(str(ip))
    return found


def local_paths(line):
    found = []
    for pattern, what in LOCAL_PATHS:
        if pattern.search(line) and what not in found:
            found.append(what)
    return found


def py310_syntax(source, filename):
    """Problems that keep Python 3.10 from parsing source: [(line, message)]."""
    try:
        ast.parse(source, filename=filename, feature_version=(3, 10))
    except SyntaxError as e:
        return [(e.lineno or 0, e.msg)]
    start = getattr(tokenize, 'FSTRING_START', None)
    if start is None:  # Python 3.10 and 3.11: ast.parse has already applied the 3.10 f-string grammar
        return []
    problems, stack = [], []   # stack: (quote, start line) of every open f-string
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as e:
        return [(0, 'tokenize: %s' % e)]

    def clashes(literal):
        for quote, _ in stack:
            if (len(quote) == 1 and literal[0] == quote) or (len(quote) == 3 and literal.startswith(quote)):
                return True
        return False

    for tok in tokens:
        if tok.type == start:
            quote = tok.string.lstrip('rRfFbBuU')
            if stack and clashes(quote):
                problems.append((tok.start[0], 'nested f-string reuses an enclosing quote %s' % quote))
            stack.append((quote, tok.start[0]))
        elif tok.type == tokenize.FSTRING_END:
            if stack:
                stack.pop()
        elif stack and tok.type == tokenize.NL and len(stack[-1][0]) == 1:
            problems.append((stack[-1][1], 'a replacement field of a single-quoted f-string spans several lines'))
        elif stack and tok.type == tokenize.STRING:
            body = tok.string.lstrip('rRbBuU')
            if clashes(body):
                problems.append((tok.start[0], 'string in an f-string expression reuses an enclosing quote'))
            if '\\' in tok.string:
                problems.append((tok.start[0], 'backslash in an f-string expression'))
            if '#' in tok.string:
                problems.append((tok.start[0], "'#' in an f-string expression"))
        elif stack and tok.type == tokenize.COMMENT:
            problems.append((tok.start[0], 'comment in an f-string expression'))
    return problems


def py310_api(tree):
    """[(line, name)] of standard-library names that Python 3.10 lacks."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[0] in STDLIB_ADDED_AFTER_310:
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split('.')[0]
            if top in STDLIB_ADDED_AFTER_310:
                found.append((node.lineno, node.module))
            for alias in node.names:
                if alias.name in PY311_NAMES.get(node.module, ()):
                    found.append((node.lineno, '%s.%s' % (node.module, alias.name)))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.attr in PY311_NAMES.get(node.value.id, ()):
                found.append((node.lineno, '%s.%s' % (node.value.id, node.attr)))
        elif isinstance(node, ast.Name) and node.id in PY311_BUILTINS:
            found.append((node.lineno, node.id))
    return found


def foreign_imports(tree, local_modules):
    """[(line, module)] of absolute imports that are neither standard library (3.10) nor demo modules."""
    stdlib = (set(getattr(sys, 'stdlib_module_names', ())) | STDLIB_REMOVED_AFTER_310) - STDLIB_ADDED_AFTER_310
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            top = name.split('.')[0]
            if top not in stdlib and top not in local_modules:
                found.append((node.lineno, name))
    return found


# --------------------------------------------------------------------------------------------- detector selftest
def detector_selftest():
    """Names of the failed cases. Every sample is assembled at run time, so this file itself stays clean."""
    digest = hashlib.sha256(b'edgefhe-hygiene-sample').hexdigest()
    failed = []

    def expect(name, value, wanted):
        if bool(value) != wanted:
            failed.append(name)

    expect('generic_api_key_positive', secret_values('api' + '_key = "%s"' % digest), True)
    expect('generic_api_key_token', secret_values('auth' + '_token: ' + digest), True)
    expect('generic_api_key_two_lines', secret_values('pass' + 'word =\n  "%s"\n' % digest), True)
    expect('generic_api_key_negative', secret_values('peer' + '_sha256 = "%s"' % digest), False)
    expect('generic_api_key_allowlist', secret_values('key' + '_id = "%s"' % digest), False)
    expect('generic_api_key_path', secret_values('key' + ': "/home/ubuntu/edgefhe-demo/pki/client.key"'), False)
    expect('generic_api_key_cjk', secret_values('金鑰' + 'key: 雲端伺服器以示範專用的墊片寫出每一條連線的工作階段金鑰'), False)
    expect('hex64_positive', hex64_secret('secret' + '_hex = "%s"' % digest), True)
    expect('hex64_negative', hex64_secret('staged_tree_id = "%s"' % digest), False)
    expect('private_key', PRIVATE_KEY.search('-----BEGIN ' + 'EC ' + 'PRIVATE KEY-----'), True)
    body = '\n'.join([digest[:48].upper() + 'Aa+/'] * 3)
    block = '-----BEGIN ' + 'CERTIFICATE-----\n' + body + '\n-----END ' + 'CERTIFICATE-----\n'
    expect('pem_block_positive', PEM_BLOCK.search(block), True)
    expect('pem_block_marker_only', PEM_BLOCK.search("b'-----BEGIN ' + 'CERTIFICATE REQUEST-----'"), False)
    expect('real_ip_positive', real_ips('host ' + '.'.join(('10', '20', '30', '40'))), True)
    expect('real_ip_documentation', real_ips('host ' + '.'.join(('192', '0', '2', '10'))), False)
    expect('real_ip_loopback', real_ips('listen ' + '.'.join(('127', '0', '0', '1')) + ':8080'), False)
    expect('local_path_profile', local_paths('C:' + '\\' + 'Users' + '\\' + 'someone' + '\\x'), True)
    expect('local_path_board', local_paths('/home/ubuntu/edgefhe-demo/pki/ca.pem'), False)
    expect('private_link', PRIVATE_LINK.search('https://' + 'www.notion' + '.so/page'), True)
    expect('public_link', PRIVATE_LINK.search('https://github.com/openfheorg/openfhe-development.git'), False)
    expect('py310_api', py310_api(ast.parse('import toml' + 'lib\nfrom datetime import ' + 'UTC\n')), True)
    expect('py310_api_negative', py310_api(ast.parse('import json\nfrom datetime import timezone\n')), False)
    expect('foreign_import', foreign_imports(ast.parse('import ser' + 'ial\n'), set()), True)
    expect('stdlib_import', foreign_imports(ast.parse('import json, ssl, fcntl\n'), set()), False)
    expect('py310_syntax_match', py310_syntax('match' + ' x:\n    case 1:\n        pass\n', 's.py'), False)
    if getattr(tokenize, 'FSTRING_START', None) is not None:
        quote = "'"
        expect('py310_fstring_quote', py310_syntax('x = f' + quote + '{' + quote + 'a' + quote + '}' + quote + '\n',
                                                   's.py'), True)
        expect('py310_fstring_ok', py310_syntax('x = f"{' + quote + 'a' + quote + '}"\n', 's.py'), False)
    return failed


# ------------------------------------------------------------------------------------------------- tree checks
def load_inventory(root):
    path = os.path.join(root, 'tools', 'inventory.py')
    spec = importlib.util.spec_from_file_location('edgefhe_inventory', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_output(root, args, stdin=None):
    exe = shutil.which('git')
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, '--no-optional-locks', '-C', root] + args, input=stdin, capture_output=True,
                           timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode not in (0, 1):   # check-ignore exits 1 when no path is ignored
        return None
    return r.stdout


def git_work_tree(root):
    out = git_output(root, ['rev-parse', '--show-toplevel'])
    if not out:
        return False
    top = out.decode('utf-8', 'replace').strip()
    return os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(root))


def gitignore_patterns(path, report):
    """Patterns of demo/.gitignore (directory patterns end with '/'); unsupported forms are reported."""
    patterns = []
    if not os.path.isfile(path):
        return patterns
    with open(path, encoding='utf-8') as f:
        for number, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            body = line[:-1] if line.endswith('/') else line
            if line.startswith('!') or '/' in body or '**' in body:
                report.add('CONFIG', 'demo/.gitignore', number,
                           '本測試只支援不含斜線與否定的簡單規則（%s），以便與 tools/inventory.py 逐一對照。' % line)
                continue
            patterns.append(line)
    return patterns


def emulated_ignored(rel_in_demo, patterns):
    parts = rel_in_demo.split('/')
    for pattern in patterns:
        if pattern.endswith('/'):
            if any(fnmatch.fnmatchcase(p, pattern[:-1]) for p in parts[:-1]):
                return True
        elif any(fnmatch.fnmatchcase(p, pattern) for p in parts):
            return True
    return False


def stray_reason(rel):
    """Why a demo/ path must not exist, or None."""
    parts = rel.split('/')
    for part in parts[:-1]:
        if part in STRAY_DIRS:
            return '位於名為 %s 的目錄內：%s' % (part, STRAY_DIRS[part])
        if part.startswith('.venv'):
            return '位於 Python 虛擬環境目錄 %s 內' % part
    name = parts[-1]
    if name in STRAY_NAMES:
        return STRAY_NAMES[name]
    for suffix, why in STRAY_NAME_SUFFIXES.items():
        if name.endswith(suffix):
            return why
    suffix = os.path.splitext(name)[1].lower()
    if suffix in STRAY_SUFFIXES:
        return STRAY_SUFFIXES[suffix]
    return None


def walk_files(top, report, root):
    """Regular files under top (sorted); symbolic links are reported, never followed."""
    found = []
    for folder, dirs, files in os.walk(top):
        for d in list(dirs):
            if os.path.islink(os.path.join(folder, d)):
                report.add('SYMLINK', rel_posix(root, os.path.join(folder, d)), None,
                           '本目錄是符號連結；verify_repository.py 不接受清單中的符號連結。')
                dirs.remove(d)
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            if os.path.islink(path):
                report.add('SYMLINK', rel_posix(root, path), None, '本檔案是符號連結；verify_repository.py 不接受清單中的符號連結。')
                continue
            found.append(path)
    return found


def check_config(root, report):
    gitignore = os.path.join(root, 'demo', '.gitignore')
    if not os.path.isfile(gitignore):
        report.add('CONFIG', 'demo/.gitignore', None, '本檔案不存在；demo/ 需要自己的 .gitignore。')
    else:
        with open(gitignore, encoding='utf-8') as f:
            lines = {line.strip() for line in f}
        for rule in GITIGNORE_REQUIRED:
            if rule not in lines:
                report.add('CONFIG', 'demo/.gitignore', None, '本檔案缺少必要的規則 %s。' % rule)
    editorconfig = os.path.join(root, 'demo', '.editorconfig')
    if not os.path.isfile(editorconfig):
        report.add('CONFIG', 'demo/.editorconfig', None, '本檔案不存在。')
    else:
        with open(editorconfig, encoding='utf-8') as f:
            text = f.read()
        section = re.search(r'(?ms)^\[\*\][ \t]*\n(.*?)(?=^\[|\Z)', text)
        if not section or not re.search(r'(?m)^end_of_line\s*=\s*lf\s*$', section.group(1)):
            report.add('CONFIG', 'demo/.editorconfig', None, '本檔案須在 [*] 區段設定 end_of_line = lf。')
    if not os.path.isfile(os.path.join(root, 'demo', 'README.md')):
        report.add('CONFIG', 'demo/README.md', None, '本檔案不存在；demo/ 的操作手冊須存在。')


def check_markdown(root, rel, text, report):
    links = 0
    stripped = MD_FENCE.sub('', text)
    base = os.path.dirname(os.path.join(root, rel))
    real_root = os.path.normcase(os.path.realpath(root))
    for link in MD_LINK.findall(stripped):
        if link.startswith(('http:', 'https:', '#', 'mailto:')):
            continue
        target = unquote(link.split('#')[0].strip('<>'))
        resolved = os.path.normcase(os.path.realpath(os.path.join(base, target)))
        links += 1
        if not os.path.exists(os.path.join(base, target)):
            report.add('MD_LINK', rel, None, '相對連結 %s 指向不存在的路徑；verify_repository.py 會因此失敗。' % link)
        elif resolved != real_root and not resolved.startswith(real_root + os.sep):
            report.add('MD_LINK', rel, None, '相對連結 %s 指向 repository 之外。' % link)
    return links


def check_text(rel, text, report):
    for offset, found in secret_values(text):
        report.add('SECRET_VALUE', rel, text.count('\n', 0, offset) + 1,
                   'gitleaks 的 generic-api-key 規則會把此行視為金鑰（%s）；CI 的祕密掃描會因此失敗。' % found)
    for number, line in enumerate(text.split('\n'), 1):
        words = hex64_secret(line)
        if words:
            report.add('HEX64_SECRET', rel, number,
                       '此行在含有 %s 的識別字旁出現 64 位十六進位值；gitleaks 會把它視為金鑰。' % ', '.join(words[:3]))
        for ip in real_ips(line):
            report.add('REAL_IP', rel, number, '此行含有 IPv4 位址 %s；本 repository 只能使用 127.0.0.1、0.0.0.0 或 '
                       'RFC 5737 的範例位址（例如 192.0.2.10）。' % ip)
        for what in local_paths(line):
            report.add('LOCAL_PATH', rel, number, '此行含有特定電腦的路徑（%s）；公開的 repository 只能使用相對路徑或'
                       '展示約定的路徑。' % what)
        m = PRIVATE_LINK.search(line)
        if m:
            report.add('PRIVATE_LINK', rel, number, '此行含有私人文件或服務的連結（%s）；公開的 repository 不得包含此類'
                       '連結。' % m.group(1))
    m = PEM_BLOCK.search(text)
    if m:
        report.add('PEM_BLOCK', rel, text.count('\n', 0, m.start()) + 1,
                   '本檔案含有完整的 PEM 區塊（%s）；示範 PKI 的憑證與請求只能放在 repository 之外。' % m.group(1))


def check_python(rel, text, local_modules, report):
    for line, message in py310_syntax(text, rel):
        report.add('PY310_SYNTAX', rel, line, 'Python 3.10 無法解析本檔案：%s。' % message)
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError:
        return
    for line, name in py310_api(tree):
        report.add('PY310_API', rel, line, '本檔案使用 Python 3.11 以後才有的 %s；CI 與 KV260 使用 Python 3.10。' % name)
    if rel.startswith(STDLIB_ONLY_PATHS):
        for line, name in foreign_imports(tree, local_modules):
            report.add('STDLIB_ONLY', rel, line, '板端或容器端的程式匯入了標準函式庫以外的模組 %s；KV260 與容器只有 Python '
                       '3.10 的標準函式庫。' % name)


def run(root):
    report = Report(root)
    demo = os.path.join(root, 'demo')
    counts = Counter()
    failed = detector_selftest()
    for case in failed:
        report.add('DETECTOR_SELFTEST', 'demo/tests/test_hygiene.py', None, '本測試的偵測規則未通過自我檢查（%s）。' % case)
    if not os.path.isdir(demo) or not os.path.isfile(os.path.join(root, 'tools', 'inventory.py')):
        report.add('CONFIG', rel_posix(root, demo), None, '本測試須在 repository 根目錄下找到 demo/ 與 tools/inventory.py。')
        return report, counts, False
    inventory = load_inventory(root)
    check_config(root, report)
    patterns = gitignore_patterns(os.path.join(demo, '.gitignore'), report)
    files = walk_files(demo, report, root)
    rels = [rel_posix(root, p) for p in files]
    use_git = git_work_tree(root)
    ignored = None
    if use_git:
        out = git_output(root, ['check-ignore', '--no-index', '-z', '--stdin'], stdin='\0'.join(rels).encode('utf-8'))
        if out is not None:
            ignored = {p for p in out.decode('utf-8', 'replace').split('\0') if p}
    if ignored is None:
        use_git = False
        ignored = {rel for rel in rels if emulated_ignored(rel[len('demo/'):], patterns)}
    local_modules = set()
    for rel in rels:
        if rel.endswith('.py'):
            local_modules.add(rel.rsplit('/', 1)[-1][:-3])
        if rel.endswith('/__init__.py'):
            local_modules.add(rel.split('/')[-2])

    for path, rel in zip(files, rels):
        counts['files'] += 1
        with open(path, 'rb') as f:
            data = f.read()
        m = PRIVATE_KEY.search(data.decode('latin-1'))
        if m:
            report.add('PRIVATE_KEY', rel, data.count(b'\n', 0, m.start()) + 1,
                       '本檔案含有私鑰區塊；任何私鑰都不得放在 repository 之內（demo/local 亦同）。')
        why = stray_reason(rel)
        if why:
            report.add('STRAY_FILE', rel, None, '本檔案不得位於 demo/ 之下（%s）；操作人員應刪除本檔案，或移到 repository 之外'
                       '（例如 C:\\EdgeFHE_evidence 或 C:\\EdgeFHE_demo）。' % why)
            continue
        if rel in ignored:
            report.add('GITIGNORED', rel, None, 'git 不會提交本檔案（.gitignore），但 tools/update_manifest.py 仍會把它寫入 '
                       'release/SHA256SUMS；操作人員應先刪除或移出本檔案。')
            continue
        if not inventory.tracked(rel):
            report.add('NOT_IN_INVENTORY', rel, None, 'git 會提交本檔案，但 tools/inventory.py 不把它列入 release/SHA256SUMS；'
                       '操作人員應更改檔名或位置。')
        if len(data) > MAX_BYTES:
            report.add('LARGE_FILE', rel, None, '本檔案大於 2 MiB（%d 位元組）；大型資料與擷取檔應放在 repository 之外。'
                       % len(data))
        if os.path.splitext(rel)[1].lower() in BINARY_SUFFIXES:
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError as e:
            report.add('ENCODING', rel, data.count(b'\n', 0, e.start) + 1, '本檔案不是 UTF-8 文字（%s）。' % e.reason)
            continue
        counts['text'] += 1
        if data.startswith(b'\xef\xbb\xbf'):
            report.add('ENCODING', rel, 1, '本檔案以 BOM 開頭；文字檔必須是無 BOM 的 UTF-8。')
        if b'\r' in data:
            report.add('ENCODING', rel, data.count(b'\n', 0, data.index(b'\r')) + 1,
                       '本檔案含有 CR 位元組；.gitattributes 依原樣保存位元組，文字檔只能使用 LF 換行。')
        check_text(rel, text, report)
        if rel.endswith('.md'):
            counts['markdown_links'] += check_markdown(root, rel, text, report)
        if rel.endswith('.py'):
            counts['python'] += 1
            check_python(rel, text, local_modules, report)

    v8b = os.path.join(root, 'v8b_rev5')
    for folder, dirs, names in os.walk(v8b):
        for d in dirs:
            if d == '__pycache__':
                report.add('V8B_CACHE', rel_posix(root, os.path.join(folder, d)), None,
                           'v8b_rev5 內出現 Python 快取目錄；tools/verify_repo.py 與疊加程式會因此拒絕整個樹，'
                           '操作人員應刪除此目錄。')
        for name in names:
            if name.endswith(('.pyc', '.pyo')):
                report.add('V8B_CACHE', rel_posix(root, os.path.join(folder, name)), None,
                           'v8b_rev5 內出現 Python 快取檔；操作人員應刪除此檔案。')

    if use_git:
        out = git_output(root, ['ls-files', '-z'])
        if out is None:
            use_git = False
        else:
            tracked = {p for p in out.decode('utf-8', 'replace').split('\0') if p}
            manifest = Path(root) / 'release' / 'SHA256SUMS'   # inventory.files compares Path objects
            for path in inventory.files(Path(root), exclude={manifest}):
                rel = rel_posix(root, path)
                if rel.startswith('demo/') or rel in tracked:
                    continue
                report.add('UNTRACKED_OUTSIDE', rel, None, 'git 未追蹤本檔案，但 tools/update_manifest.py 會把它寫入 '
                           'release/SHA256SUMS，CI 因此失敗；操作人員應先移出本檔案，或另行提交。')
    return report, counts, use_git


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='EdgeFHE demo hygiene checks (reads files only)')
    parser.add_argument('--repo', default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    args = parser.parse_args(argv)
    root = os.path.abspath(args.repo)
    report, counts, used_git = run(root)
    for check, where, reason in report.findings:
        print('HYGIENE_FINDING %s %s %s' % (check, where, reason))
    if report.findings:
        checks = sorted({check for check, _, _ in report.findings})
        print('HYGIENE_FAIL findings=%d checks=%s' % (len(report.findings), ','.join(checks)))
        return 1
    print('HYGIENE_PASS files=%d text=%d python=%d markdown_links=%d git=%s'
          % (counts['files'], counts['text'], counts['python'], counts['markdown_links'], 'yes' if used_git else 'no'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
