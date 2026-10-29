"""Storage commands using an attached service or an owned standalone scan."""
import argparse
import json
import re
import sys
import time
from uuid import uuid4

import httpx

from .contracts import uuid_string
from .errors import ServiceError
from .instance import pipe_request

KINDS = ('thumbnails', 'exports', 'rejected_files')
OPERATIONS = {'storage.scan', 'storage.known_scan', 'storage.preview', 'storage.cleanup'}
ACTIVE = {'pending', 'running'}


def identifier(value):
    try:
        return uuid_string(value)
    except ServiceError as exc:
        raise argparse.ArgumentTypeError('A canonical UUID is required') from exc


def checksum(value):
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise argparse.ArgumentTypeError('A lowercase SHA-256 digest is required')
    return value


def page_limit(value):
    try:
        number = int(value)
        if 1 <= number <= 200:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('limit must be between 1 and 200')


def parse(argv):
    parser = argparse.ArgumentParser(prog='stylelora storage', description=(
        'Attach to a running local service. JSON goes to stdout; progress goes to stderr. '
        'scan --standalone --project PATH opens and closes an owned local container; other actions attach.'))
    commands = parser.add_subparsers(dest='command', required=True)
    scan = commands.add_parser('scan', help='Scan the current project, or recent projects')
    scope = scan.add_mutually_exclusive_group()
    scope.add_argument('--known', action='store_true', help='Scan all recently authorized projects')
    scope.add_argument('--known-id', action='append', type=identifier, help='Repeat to select recent project IDs')
    from . import standalone
    standalone.project_options(scan, identifier)
    cleanup = commands.add_parser('cleanup', help='Preview a kind, or explicitly confirm an existing preview')
    cleanup.add_argument('--kind', choices=KINDS, help='Preview only; does not delete files')
    cleanup.add_argument('--preview-id', type=identifier)
    cleanup.add_argument('--confirm-digest', type=checksum, help='Confirm the exact displayed preview; no new preview is created')
    cleanup.add_argument('--project-id', type=identifier, help='Required for confirmation; use the preview operation project_id')
    summary = commands.add_parser('summary', help='Read historical summaries without scanning')
    summary.add_argument('--project-id', type=identifier, help='Read only this current project summary')
    status = commands.add_parser('status', help='Read an accepted storage operation, optionally wait')
    status.add_argument('operation_id', type=identifier)
    status.add_argument('--wait', action='store_true')
    results = commands.add_parser('results', help='Read one page of a completed recent-project scan')
    results.add_argument('scan_id', type=identifier)
    results.add_argument('--limit', type=page_limit, default=50)
    results.add_argument('--cursor')
    results.add_argument('--sort', choices=('-total_bytes,known_id', 'known_id'), default='-total_bytes,known_id')
    for command in (scan, cleanup):
        wait = command.add_mutually_exclusive_group()
        wait.add_argument('--wait', dest='wait', action='store_true', default=True, help='Wait for completion (default)')
        wait.add_argument('--detach', dest='wait', action='store_false', help='Return the accepted task, not a completion claim')
        command.add_argument('--idempotency-key', type=identifier, help='Reuse only for the same request in the same service session')
    for command in (scan, cleanup, summary, status, results):
        command.add_argument('--json', action='store_true', help='JSON output (also the default)')
    args = parser.parse_args(argv)
    standalone.validate(parser, args)
    if args.command == 'scan':
        if args.project_id and (args.known or args.known_id):
            parser.error('--project-id cannot be combined with recent-project scanning')
        if args.known_id and (len(args.known_id) > 200 or len(set(args.known_id)) != len(args.known_id)):
            parser.error('--known-id must contain 1 to 200 distinct IDs')
    if args.command == 'cleanup':
        confirming = args.preview_id is not None or args.confirm_digest is not None
        if confirming and (not args.preview_id or not args.confirm_digest or not args.project_id or args.kind):
            parser.error('Confirmation requires --project-id, --preview-id and --confirm-digest; omit --kind')
        if not confirming and not args.kind:
            parser.error('Preview requires --kind; confirmation requires an existing preview and digest')
    if args.command == 'results' and args.cursor is not None and (not args.cursor or len(args.cursor) > 4096):
        parser.error('cursor must contain 1 to 4096 characters')
    return args


def error_exit(status, body):
    if body.get('outcome') == 'unknown':
        return 7
    if status in (401, 403):
        return 3
    if status in (409, 410, 423, 428):
        return 4
    return 5 if status >= 500 or status in (408, 429) else 2


def result_exit(value):
    if value.get('status') == 'failed':
        error = value.get('error') or {}
        code = error.get('code', '')
        if error.get('outcome') == 'unknown':
            return 7
        if code in ('cleanup:preview_stale', 'cleanup:receipt_exists', 'storage:leased'):
            return 4
        return 5
    if value.get('status') in ACTIVE | {'paused', 'canceled', 'interrupted'}:
        return 7
    outcome = value.get('result') or value.get('summary') or value
    if outcome.get('partial') or outcome.get('failed', 0) or outcome.get('status') == 'partial':
        return 6
    return 0


class ReplyError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body


class Connection:
    def __init__(self, client, info):
        self.client, self.info = client, info

    def request(self, method, path, *, body=None, key=None, params=None):
        writing = method == 'POST'
        if writing:
            self.info['request'] = {'method': method, 'path': path, 'body': body, 'idempotency_key': key or str(uuid4())}
        try:
            response = self.client.request(method, path, json=body, params=params,
                                           headers={'Idempotency-Key': self.info['request']['idempotency_key']} if writing else {})
            if not response.headers.get('content-type', '').startswith('application/json'):
                raise ValueError()
            value = response.json()
            if not isinstance(value, dict) or 300 <= response.status_code < 400:
                raise ValueError()
        except (httpx.HTTPError, OSError, ValueError):
            # Retain the request identity, but never retry a POST or reattach to a new session.
            raise ServiceError(503, 'request:outcome_unknown' if writing else 'request:unavailable',
                               outcome='unknown' if writing else 'rejected') from None
        if response.is_error:
            error = value.get('error', value)
            if not isinstance(error, dict):
                raise ServiceError(503, 'request:invalid_response', outcome='unknown' if writing else 'rejected')
            raise ReplyError(response.status_code, error)
        return value

    def project(self, expected=None):
        value = self.request('GET', '/api/session')
        pid = value.get('project_id')
        if pid is None:
            raise ServiceError(409, 'project:not_open')
        uuid_string(pid)
        if expected is not None and expected != pid:
            raise ServiceError(409, 'project:handle_expired')
        self.info['project_id'] = pid
        return '/api/projects/'+pid+'/storage'

    def operation(self, operation_id, wait):
        url = '/api/operations/'+operation_id
        self.info.update(operation_id=operation_id, status_url=url)
        while True:
            value = self.request('GET', url)
            if value.get('operation_id') != operation_id or value.get('kind') not in OPERATIONS:
                raise ServiceError(422, 'storage:operation_required')
            if value.get('status') not in ACTIVE | {'done', 'failed', 'canceled', 'interrupted'}:
                raise ServiceError(503, 'request:invalid_response')
            if not wait or value['status'] not in ACTIVE:
                return value
            time.sleep(.1)

    def start(self, path, body, args):
        value = self.request('POST', path, body=body, key=args.idempotency_key)
        try:
            operation_id = uuid_string(value['task']['id'])
            if value['task']['kind'] != 'operation' or value['status_url'] != '/api/operations/'+operation_id:
                raise ValueError()
        except (KeyError, TypeError, ValueError, ServiceError):
            raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
        self.info.update(operation_id=operation_id, status_url=value['status_url'])
        print('Accepted storage operation '+operation_id+'; status: '+value['status_url'], file=sys.stderr)
        return self.operation(operation_id, True) if args.wait else {**value, 'status': 'accepted'}


def execute(connection, args):
    if args.command == 'summary':
        path = connection.project(args.project_id)+'/summary' if args.project_id else '/api/storage/summary'
        return connection.request('GET', path)
    if args.command == 'status':
        return connection.operation(args.operation_id, args.wait)
    if args.command == 'results':
        params = {'limit': args.limit, 'sort': args.sort}
        if args.cursor:
            params['cursor'] = args.cursor
        return connection.request('GET', '/api/storage/scans/'+args.scan_id, params=params)
    if args.command == 'scan' and (args.known or args.known_id):
        return connection.start('/api/storage/scans', {'known_ids': args.known_id}, args)
    path = connection.project(args.project_id)
    if args.command == 'scan':
        return connection.start(path+'/scans', {}, args)
    if args.kind:
        return connection.start(path+'/cleanup/preview', {'kind': args.kind}, args)
    return connection.start(path+'/cleanup', {'preview_id': args.preview_id, 'digest': args.confirm_digest, 'confirm': True}, args)


def main(argv):
    args = parse(argv)
    info = {}
    try:
        from .standalone import session_info
        local = session_info(info)
        if local is not None:
            value = execute(Connection(local, info), args)
            code = result_exit(value)
            print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
            return code
        if getattr(args, 'standalone', False):
            from .standalone import container
            with container(args, info, Connection) as connection:
                value = execute(connection, args)
            code = result_exit(value)
            print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
            return code
        try:
            credentials = pipe_request({'action': 'hello'})
        except (ServiceError, OSError):
            raise ReplyError(503, {'code': 'instance:unavailable', 'outcome': 'rejected'}) from None
        if (credentials.get('protocol_version') != 1 or
                not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', credentials.get('authority', ''))):
            raise ReplyError(503, {'code': 'instance:unavailable', 'outcome': 'rejected'})
        info['instance_id'] = credentials['instance_id']
        with httpx.Client(base_url='http://'+credentials['authority'],
                          headers={'Authorization': 'Bearer '+credentials['token']},
                          trust_env=False, follow_redirects=False, timeout=30) as client:
            value = execute(Connection(client, info), args)
        code = result_exit(value)
    except ReplyError as exc:
        value = exc.body
        code = 3 if value.get('code') == 'instance:unavailable' else error_exit(exc.status, value)
    except ServiceError as exc:
        value = exc.body(str(uuid4()))
        code = error_exit(exc.status, value)
    except KeyboardInterrupt:
        value, code = {'code': 'client:interrupted', 'outcome': 'unknown'}, 130
        from .standalone import interruption
        print(interruption(args, 'Stopped waiting; accepted server tasks continue. Inspect cli.operation_id or cli.request before retrying.'), file=sys.stderr)
    except (httpx.HTTPError, OSError):
        value, code = {'code': 'request:unavailable', 'outcome': 'unknown' if info.get('request') else 'rejected'}, 5
    print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
    return code
