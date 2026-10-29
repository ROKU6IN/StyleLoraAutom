"""Authenticated local CLI transport with explicit success codes and write receipts."""
from contextlib import contextmanager
import re
from uuid import uuid4

import httpx

from .contracts import strict_json, uuid_string
from .errors import ServiceError
from .instance import pipe_request
from .storage_cli import ReplyError


@contextmanager
def attach(info, expected_instance=None):
    from .standalone import session_info
    local = session_info(info, expected_instance)
    if local is not None:
        yield Connection(local, info)
        return
    try:
        credentials = pipe_request({'action': 'hello'})
        authority = credentials['authority']
        if (credentials.get('protocol_version') != 1 or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', authority)
                or not 1 <= int(authority.split(':')[1]) <= 65535
                or not isinstance(credentials['token'], str) or not re.fullmatch(r'[\x21-\x7e]+', credentials['token'])):
            raise ValueError()
        info['instance_id'] = uuid_string(credentials['instance_id'])
    except (ServiceError, OSError, KeyError, TypeError, ValueError):
        raise ReplyError(503, {'code': 'instance:unavailable', 'outcome': 'rejected'}) from None
    if expected_instance and expected_instance != info['instance_id']:
        raise ServiceError(409, 'instance:changed')
    with httpx.Client(base_url='http://'+authority, headers={'Authorization': 'Bearer '+credentials['token']},
                      trust_env=False, follow_redirects=False, timeout=30) as client:
        yield Connection(client, info)


class Connection:
    def __init__(self, client, info):
        self.client, self.info = client, info

    def request(self, method, path, *, body=None, key=None, expected=None, status=200, params=None):
        writing, headers = method in ('POST', 'PUT', 'PATCH', 'DELETE'), {}
        if writing:
            receipt = {'method': method, 'path': path, 'body': body, 'idempotency_key': key or str(uuid4())}
            headers['Idempotency-Key'] = receipt['idempotency_key']
            if expected is not None:
                receipt['if_match'] = expected
                headers['If-Match'] = '"'+expected+'"'
            self.info['request'] = receipt
        try:
            response = self.client.request(method, path, json=body, headers=headers, params=params)
            if not response.headers.get('content-type', '').startswith('application/json'):
                raise ValueError()
            value = strict_json(response.content)
            nullable = method == 'GET' and path == '/api/projects/current' and response.status_code == 200
            if not isinstance(value, dict) and not (value is None and nullable):
                raise ValueError()
            if not response.is_error and response.status_code != status:
                raise ValueError()
            if response.is_error:
                error = value.get('error', value)
                if not isinstance(error, dict) or not isinstance(error.get('code'), str) or error.get('outcome') not in ('unknown', 'rejected'):
                    raise ValueError()
                raise ReplyError(response.status_code, error)
        except (httpx.HTTPError, OSError, ValueError, ServiceError):
            raise ServiceError(503, 'request:outcome_unknown' if writing else 'request:unavailable',
                               outcome='unknown' if writing else 'rejected') from None
        return value

    def project(self, expected=None, *, resource='jobs'):
        current = self.request('GET', '/api/projects/current')
        if current is None:
            raise ServiceError(409, 'project:not_open')
        try:
            pid = uuid_string(current['project_id'])
        except (KeyError, TypeError, ServiceError):
            raise ServiceError(503, 'request:invalid_response') from None
        if expected and pid != expected:
            raise ServiceError(409, 'project:handle_expired')
        self.info['project_id'] = pid
        return '/api/projects/'+pid+'/'+resource
