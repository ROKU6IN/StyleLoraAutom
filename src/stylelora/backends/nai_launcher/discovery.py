import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from ..types import BackendError, DiscoveryInfo

PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26')
VERSION = re.compile(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]{1,48})?\Z')


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Invalid number')
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('Invalid number')
        return result
    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant, parse_float=finite_float)


def discover(appdata=None, *, explicit_token=None, environ=None, attempts=3, delay=0.05):
    environment = os.environ if environ is None else environ
    base = appdata if appdata is not None else environment.get('APPDATA')
    if not base:
        raise BackendError('discovery:not_running', phase='discovery')
    path = Path(base) / 'nai-launcher' / 'mcp-server.json'
    data = None
    for attempt in range(attempts):
        try:
            # Never follow a discovery file redirected outside its local directory.
            if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (path, *path.parents)):
                raise BackendError('discovery:invalid_file', phase='discovery')
            with path.open('rb') as stream:
                raw = stream.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError()
            data = strict_json(raw.decode('utf-8-sig'))
            break
        except (OSError, ValueError, RecursionError):
            if attempt + 1 == attempts:
                code = 'discovery:not_running' if not path.exists() else 'discovery:invalid_file'
                raise BackendError(code, phase='discovery') from None
            time.sleep(delay)
    try:
        if not isinstance(data, dict):
            raise ValueError()
        endpoint, port, pid = data['endpoint'], data['port'], data['pid']
        versions, app_version = data['protocol_versions'], data['app_version']
        if not isinstance(endpoint, str) or len(endpoint) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in endpoint):
            raise ValueError()
        parsed = urlsplit(endpoint)
        if (type(port) is not int or not 1 <= port <= 65535 or type(pid) is not int or pid < 1
                or parsed.scheme != 'http' or parsed.netloc != f'127.0.0.1:{port}'
                or parsed.port != port or parsed.query or parsed.fragment
                or not parsed.path.startswith('/') or '%' in parsed.path or '\\' in parsed.path
                or any(p in ('.', '..') for p in parsed.path.split('/'))):
            raise ValueError()
        if (not isinstance(versions, list) or not 1 <= len(versions) <= 8
                or any(not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v) for v in versions)
                or len(set(versions)) != len(versions) or not isinstance(app_version, str) or len(app_version) > 128 or not VERSION.fullmatch(app_version)):
            raise ValueError()
        token = explicit_token if explicit_token is not None else environment.get('NAI_LAUNCHER_MCP_TOKEN', data.get('token'))
        if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise BackendError('discovery:missing_token', phase='discovery')
        if token in endpoint or token in app_version:
            raise ValueError()
        return DiscoveryInfo(endpoint, port, token, pid, tuple(versions), app_version)
    except (KeyError, TypeError, ValueError):
        raise BackendError('discovery:invalid_file', phase='discovery') from None
