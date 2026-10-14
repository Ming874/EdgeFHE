"""V4 endpoint/auth policy. No fixed port; no access to fixtures or secrets on import."""
import re
from urllib.parse import urlsplit

PROTOCOL = 'edgefhe-dynamic-affine-tls-v4'
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
def endpoint(url):
    if not isinstance(url, str) or any(ord(c)<=32 for c in url) or '?' in url or '#' in url:
        raise ValueError('Endpoint must be an HTTPS origin without whitespace/query/fragment')
    u = urlsplit(url)
    try: port = u.port or 443
    except ValueError: raise ValueError('Invalid endpoint port') from None
    if (u.scheme != 'https' or u.hostname is None or u.username is not None or
            u.password is not None or u.path not in ('', '/') or u.query or u.fragment or
            not 1 <= port <= 65535 or (u.netloc.endswith(':'))):
        raise ValueError('Use an HTTPS origin with a valid port and no credentials/path/query')
    # Explicit :0 must not silently become the HTTPS default.
    if u.port == 0: raise ValueError('Port zero is invalid')
    return u.hostname, port

def validate_config(c):
    keys = {'protocol', 'server_url', 'server_id', 'token', 'server_certificate_sha256', 'mtls'}
    optional = {'client_certificate', 'client_key', 'client_ca'}
    if type(c) is not dict or not keys <= set(c) or set(c)-keys-optional: raise ValueError('Unknown/missing config field')
    endpoint(c['server_url'])
    if c['protocol'] != PROTOCOL or not isinstance(c['server_id'], str) or not 1 <= len(c['server_id']) <= 64:
        raise ValueError('Invalid server identity/protocol')
    if not isinstance(c['token'], str) or not HEX64.fullmatch(c['token']): raise ValueError('Use a fresh 256-bit token')
    if not isinstance(c['server_certificate_sha256'], str) or not HEX64.fullmatch(c['server_certificate_sha256']):
        raise ValueError('Invalid certificate pin')
    if type(c['mtls']) is not bool: raise ValueError('mtls must be boolean')
    return c
