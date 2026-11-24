"""Load and validate the gateway configuration (gateway.json); Python 3.10 standard library only.

The configuration supplies every string that the PL invocation permit binds (invoke_permit.hpp:9-10) and every
argument of the unchanged efnet_client, so each value is checked before any client starts: normalized absolute paths,
an IPv4 literal for the cloud host (tls.hpp:39-42), lowercase 64-hex pins (common.hpp:31-35) and the option ranges of
client.cpp:107-112. Nothing is guessed: an error names the key and the rule it breaks.
"""
import ipaddress
import json
import os
import posixpath
import re

SCHEMA = 'edgefhe.demo.gateway-config.v1'
DEFAULT_PATH = '/etc/edgefhe-demo/gateway.json'
EDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_DIR = os.path.dirname(EDGE_DIR)
BACKENDS = ('pl', 'spec', 'cpu')
MAX_BYTES = 65536

REQUIRED = ('client', 'plugin', 'profile_hex', 'host', 'port', 'timeout_ms', 'workers', 'ca', 'cert', 'key',
            'peer_name', 'peer_sha256', 'workdir')
# Literal argv strings: the permit contract binds them (invoke_permit.hpp:9-10), and the gateway derives the private
# CSV and score paths from workdir (invoke_permit.hpp:17-21).
BOUND_PATHS = ('client', 'plugin', 'ca', 'cert', 'key', 'workdir')
OTHER_PATHS = ('model_json', 'form_json', 'ui_dir', 'overlay_record', 'app_dir', 'public_log', 'disclosure_dir',
               'proc_interrupts', 'dma_node')
OPTIONAL = ('schema',) + OTHER_PATHS + ('listen', 'default_backend', 'backends', 'retain_private', 'progress_ms')

_HEX64 = re.compile(r'[0-9a-f]{64}')
_BOUND = re.compile(r'/[A-Za-z0-9._+/-]+')
_DNS = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*')

MESSAGES = {
    'CONFIG_READ': '本閘道無法讀取設定檔 %(path)s（%(why)s）。',
    'CONFIG_JSON': '設定檔 %(path)s 不是有效的 JSON 物件（%(why)s）。',
    'CONFIG_DUPLICATE_KEY': '設定檔含有重複的鍵「%(key)s」。',
    'CONFIG_SCHEMA': '設定檔的 schema 必須為「%(schema)s」。',
    'CONFIG_UNKNOWN_KEY': '設定檔含有本閘道不認得的鍵「%(key)s」（說明用的鍵須以底線開頭）。',
    'CONFIG_MISSING': '設定檔缺少必要的鍵「%(key)s」。',
    'CONFIG_TYPE': '設定值「%(key)s」的型別不正確，應為%(want)s。',
    'CONFIG_PATH': '設定值「%(key)s」必須是正規化的絕對路徑（%(rule)s）。',
    'CONFIG_HOST': '設定值「host」必須是雲端電腦的 IPv4 位址字面值（例如 192.0.2.10），不得為 0.0.0.0、廣播或群播位址。',
    'CONFIG_RANGE': '設定值「%(key)s」超出允許範圍（%(rule)s）。',
    'CONFIG_PIN': '設定值「%(key)s」必須是 64 個小寫十六進位字元，且不得全為 0。',
    'CONFIG_PEER_NAME': '設定值「peer_name」必須是小寫的 DNS 名稱（例如 edgefhe-cloud）或 IPv4 位址字面值。',
    'CONFIG_BACKENDS': '設定值「backends」必須是 pl、spec、cpu 中不重複的非空子集，且須包含 default_backend。',
    'CONFIG_LISTEN': '設定值「listen」必須是「IPv4 位址:埠號」的形式（例如 127.0.0.1:8080）。',
}


class ConfigError(Exception):
    """A configuration value breaks a rule; code is one of MESSAGES."""

    def __init__(self, code, **ctx):
        self.code = code
        self.ctx = ctx
        super().__init__(code)

    def message(self):
        return MESSAGES.get(self.code, self.code) % self.ctx

    def __str__(self):
        return '%s %s' % (self.code, self.message())


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ConfigError('CONFIG_DUPLICATE_KEY', key=key)
        seen[key] = value
    return seen


def _is_int(value):
    return type(value) is int


def check_path(key, value, bound):
    """A normalized absolute POSIX path; bound paths also use a conservative character set (ASCII argv strings)."""
    if type(value) is not str:
        raise ConfigError('CONFIG_TYPE', key=key, want='字串')
    rule = '以 / 開頭、不含 //、.. 或結尾的 /'
    if bound:
        rule += '，且只含英數字與 . _ + - /'
    if (not value.startswith('/') or value == '/' or '//' in value or posixpath.normpath(value) != value
            or any(c in value for c in '\0\r\n') or len(value) >= 4000):
        raise ConfigError('CONFIG_PATH', key=key, rule=rule)
    if bound and not _BOUND.fullmatch(value):
        raise ConfigError('CONFIG_PATH', key=key, rule=rule)
    return value


def check_pin(key, value):
    if type(value) is not str or not _HEX64.fullmatch(value) or value == '0' * 64:
        raise ConfigError('CONFIG_PIN', key=key)
    return value


def check_ipv4(value, code):
    if type(value) is not str:
        raise ConfigError(code)
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise ConfigError(code)
    if str(address) != value or address.is_unspecified or address.is_multicast or value == '255.255.255.255':
        raise ConfigError(code)
    return value


def parse_listen(value):
    """'127.0.0.1:8080' -> ('127.0.0.1', 8080)."""
    if type(value) is not str or value.count(':') != 1:
        raise ConfigError('CONFIG_LISTEN')
    host, port = value.split(':')
    if not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535:
        raise ConfigError('CONFIG_LISTEN')
    try:
        check_ipv4(host, 'CONFIG_LISTEN')
    except ConfigError:
        raise ConfigError('CONFIG_LISTEN')
    return host, int(port)


def validate(raw, path=None):
    """The checked configuration: every key of REQUIRED and OPTIONAL, defaults filled in, plus 'config_path'."""
    if not isinstance(raw, dict):
        raise ConfigError('CONFIG_JSON', path=path, why='頂層不是物件')
    for key in raw:
        if key.startswith('_'):
            continue
        if key not in REQUIRED and key not in OPTIONAL:
            raise ConfigError('CONFIG_UNKNOWN_KEY', key=key)
    for key in REQUIRED:
        if key not in raw:
            raise ConfigError('CONFIG_MISSING', key=key)
    if 'schema' in raw and raw['schema'] != SCHEMA:
        raise ConfigError('CONFIG_SCHEMA', schema=SCHEMA)
    cfg = {'schema': SCHEMA}
    for key in BOUND_PATHS:
        cfg[key] = check_path(key, raw[key], True)
    cfg['profile_hex'] = check_pin('profile_hex', raw['profile_hex'])
    cfg['peer_sha256'] = check_pin('peer_sha256', raw['peer_sha256'])
    cfg['host'] = check_ipv4(raw['host'], 'CONFIG_HOST')
    for key, low, high in (('port', 1, 65535), ('timeout_ms', 100, 120000)):
        value = raw[key]
        if not _is_int(value):
            raise ConfigError('CONFIG_TYPE', key=key, want='整數')
        if not low <= value <= high:
            raise ConfigError('CONFIG_RANGE', key=key, rule='%d 至 %d' % (low, high))
        cfg[key] = value
    if not _is_int(raw['workers']):
        raise ConfigError('CONFIG_TYPE', key='workers', want='整數')
    if raw['workers'] not in (1, 3):
        raise ConfigError('CONFIG_RANGE', key='workers', rule='1 或 3（client.cpp:110）')
    cfg['workers'] = raw['workers']
    name = raw['peer_name']
    if type(name) is not str or len(name) > 253 or not (_DNS.fullmatch(name) or _ipv4_literal(name)):
        raise ConfigError('CONFIG_PEER_NAME')
    cfg['peer_name'] = name

    client_dir = posixpath.dirname(cfg['client'])
    defaults = {
        'model_json': os.path.join(DEMO_DIR, 'model', 'cdc-diabetes-lr4-v1.json'),
        'form_json': os.path.join(DEMO_DIR, 'model', 'form_cdc.json'),
        'ui_dir': os.path.join(DEMO_DIR, 'ui'),
        'overlay_record': None,
        'app_dir': posixpath.join(posixpath.dirname(client_dir), 'app'),
        'public_log': '/var/log/edgefhe-demo/public.jsonl',
        'disclosure_dir': '/var/lib/edgefhe-demo/disclosure',
        'proc_interrupts': '/proc/interrupts',
        'dma_node': '/dev/edgefhe_v8b_dma',
    }
    for key in OTHER_PATHS:
        if key in raw:
            cfg[key] = check_path(key, raw[key], False)
        else:
            cfg[key] = defaults[key]
    if cfg['workdir'].count('/') < 2:
        raise ConfigError('CONFIG_PATH', key='workdir', rule='須為專用的子目錄，例如 /run/edgefhe-demo')

    cfg['listen'] = raw.get('listen', '127.0.0.1:8080')
    parse_listen(cfg['listen'])
    backends = raw.get('backends', list(BACKENDS))
    if (not isinstance(backends, list) or not backends or len(set(backends)) != len(backends)
            or any(b not in BACKENDS for b in backends)):
        raise ConfigError('CONFIG_BACKENDS')
    default_backend = raw.get('default_backend', backends[0] if 'pl' not in backends else 'pl')
    if default_backend not in backends:
        raise ConfigError('CONFIG_BACKENDS')
    cfg['backends'] = list(backends)
    cfg['default_backend'] = default_backend
    # Default false: no private record of a form outlives its run; the evidence session that needs a disclosure
    # (verification gates G9 and G10) turns it on explicitly, with fictional answers only.
    retain = raw.get('retain_private', False)
    if type(retain) is not bool:
        raise ConfigError('CONFIG_TYPE', key='retain_private', want='布林值')
    cfg['retain_private'] = retain
    progress_ms = raw.get('progress_ms', 25)
    if not _is_int(progress_ms):
        raise ConfigError('CONFIG_TYPE', key='progress_ms', want='整數')
    if not 0 <= progress_ms <= 1000:
        raise ConfigError('CONFIG_RANGE', key='progress_ms', rule='0 至 1000')
    cfg['progress_ms'] = progress_ms
    cfg['config_path'] = path
    return cfg


def _ipv4_literal(value):
    try:
        return str(ipaddress.IPv4Address(value)) == value
    except ValueError:
        return False


def load(path=DEFAULT_PATH):
    """Read and validate a gateway.json file."""
    try:
        with open(path, 'rb') as f:
            data = f.read(MAX_BYTES + 1)
    except OSError as e:
        raise ConfigError('CONFIG_READ', path=path, why=e.strerror or str(e))
    if len(data) > MAX_BYTES:
        raise ConfigError('CONFIG_READ', path=path, why='檔案超過 %d 位元組' % MAX_BYTES)
    try:
        raw = json.loads(data.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except ConfigError:
        raise
    except ValueError as e:
        raise ConfigError('CONFIG_JSON', path=path, why=str(e))
    return validate(raw, path)
