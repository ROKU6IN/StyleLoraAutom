"""Project lifecycle commands; attached operations outlive the waiting CLI."""
import argparse
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import httpx

from ..project_db.validation import primitive
from .contracts import uuid_string
from .errors import ServiceError, safe_error
from .instance import pipe_request, ProductLock
from .storage_cli import identifier, ReplyError, error_exit
from .startup import add_options, validate_options, ensure_service

ACTIVE = {'pending', 'running'}
KINDS = {'project.create', 'project.open', 'project.close'}


def version(value):
    if not re.fullmatch('[A-Za-z0-9_-]{1,256}', value):
        raise argparse.ArgumentTypeError('Use the unquoted version returned by project status')
    return value


def parse(argv):
    parser = argparse.ArgumentParser(prog='stylelora project', description=(
        'Manage projects through a local service. init/open --start-service can start it if absent; '
        'only init --standalone creates and closes a project without a service.'))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='Create a new container; attached mode leaves it open')
    opening = sub.add_parser('open', help='Open or explicitly switch the service project')
    for command in (init, opening):
        add_options(command)
        command.add_argument('path', type=Path, nargs='?')
        command.add_argument('--grant-id', type=identifier, help='Reuse the original grant after an unknown reply; omit PATH')
    init.add_argument('--name', required=True)
    init.add_argument('--trigger', default='')
    init.add_argument('--standalone', action='store_true', help='Create under product/project locks and close before exit; requires no active service')
    closing = sub.add_parser('close', help='Close the captured project and wait for its work to drain')
    closing.add_argument('--project-id', type=identifier, help='Require this current project handle')
    closing.add_argument('--if-match', type=version, help='Replay with original version; requires --project-id and --instance-id')
    status = sub.add_parser('status', help='Read current project, or an accepted project operation')
    status.add_argument('--operation-id', type=identifier)
    status.add_argument('--wait', action='store_true', help='Wait for --operation-id; never resubmit or cancel')
    for command in (init, opening, closing):
        waiting = command.add_mutually_exclusive_group()
        waiting.add_argument('--wait', dest='wait', action='store_true', default=True)
        waiting.add_argument('--detach', dest='wait', action='store_false')
        command.add_argument('--idempotency-key', type=identifier)
    for command in (init, opening, closing, status):
        command.add_argument('--instance-id', type=identifier, help='Require the original service session when reconciling a request')
        command.add_argument('--json', action='store_true', help='JSON stdout (also the default); progress on stderr')
    args = parser.parse_args(argv)
    if args.command in ('init', 'open'):
        validate_options(parser, args)
        if (args.path is None) == (args.grant_id is None):
            parser.error('Supply exactly one of PATH or --grant-id')
        if args.grant_id and (not args.instance_id or not args.idempotency_key):
            parser.error('--grant-id requires the original --instance-id and --idempotency-key')
    if args.command == 'init':
        if not args.name.strip() or len(args.name) > 512 or len(args.trigger) > 1024 or '\0' in args.name+args.trigger:
            parser.error('Invalid project name or trigger')
        if args.standalone and (args.grant_id or args.instance_id or args.idempotency_key or not args.wait):
            parser.error('--standalone requires PATH and cannot detach or reuse session request identifiers')
    if args.command == 'close' and args.if_match and (not args.project_id or not args.instance_id):
        parser.error('--if-match requires --project-id and --instance-id')
    if args.command == 'close' and args.idempotency_key and not args.if_match:
        parser.error('Replaying close requires the original --project-id, --if-match and --instance-id')
    if args.command == 'status' and args.wait and not args.operation_id:
        parser.error('--wait requires --operation-id')
    return args


class Connection:
    def __init__(self, client, info):
        self.client, self.info = client, info

    def request(self, method, path, *, body=None, key=None, expected=None):
        writing = method == 'POST'
        headers = {}
        if writing:
            receipt = {'method': method, 'path': path, 'body': body, 'idempotency_key': key or str(uuid4())}
            if expected is not None:
                receipt['if_match'] = expected
                headers['If-Match'] = '"'+expected+'"'
            self.info['request'] = receipt
            headers['Idempotency-Key'] = receipt['idempotency_key']
        try:
            response = self.client.request(method, path, json=body, headers=headers)
            if not response.headers.get('content-type', '').startswith('application/json'):
                raise ValueError()
            value = response.json()
            if (not isinstance(value, dict) and not (value is None and method == 'GET' and path == '/api/projects/current' and response.status_code == 200)):
                raise ValueError()
            if not response.is_error and response.status_code != (202 if writing else 200):
                raise ValueError()
        except (httpx.HTTPError, OSError, ValueError):
            raise ServiceError(503, 'request:outcome_unknown' if writing else 'request:unavailable',
                               outcome='unknown' if writing else 'rejected') from None
        if response.is_error:
            error = value.get('error', value)
            if not isinstance(error, dict):
                raise ServiceError(503, 'request:invalid_response', outcome='unknown' if writing else 'rejected')
            raise ReplyError(response.status_code, error)
        return value

    def current(self):
        value = self.request('GET', '/api/projects/current')
        if value is not None:
            try:
                uuid_string(value['project_id'])
                version(value['version'])
            except (KeyError, TypeError, ServiceError, argparse.ArgumentTypeError):
                raise ServiceError(503, 'request:invalid_response') from None
            self.info['project_id'] = value['project_id']
        return value

    def operation(self, operation_id, wait, kind=None):
        path = '/api/operations/'+operation_id
        self.info.update(operation_id=operation_id, status_url=path)
        while True:
            value = self.request('GET', path)
            if value.get('operation_id') != operation_id or value.get('kind') not in KINDS or (kind and value['kind'] != kind):
                raise ServiceError(422, 'project:operation_required')
            if value.get('status') not in ACTIVE | {'done', 'failed', 'canceled', 'interrupted'}:
                raise ServiceError(503, 'request:invalid_response')
            if value['status'] == 'failed' and (not isinstance(value.get('error'), dict) or not isinstance(value['error'].get('code'), str)):
                raise ServiceError(503, 'request:invalid_response')
            if value['status'] == 'done':
                try:
                    outcome = value['result']
                    if value['kind'] == 'project.close':
                        if outcome['closed'] is not True:
                            raise ValueError()
                    else:
                        uuid_string(outcome['project_id'])
                        version(outcome['version'])
                except (KeyError, TypeError, ValueError, ServiceError, argparse.ArgumentTypeError):
                    raise ServiceError(503, 'request:invalid_response') from None
            if not wait or value['status'] not in ACTIVE:
                return value
            time.sleep(.1)

    def start(self, path, body, args, kind, expected=None):
        value = self.request('POST', path, body=body, key=args.idempotency_key, expected=expected)
        try:
            operation_id = uuid_string(value['task']['id'])
            if value['task']['kind'] != 'operation' or value['status_url'] != '/api/operations/'+operation_id:
                raise ValueError()
        except (KeyError, TypeError, ValueError, ServiceError):
            raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
        self.info.update(operation_id=operation_id, status_url=value['status_url'])
        print('Accepted '+kind+' operation '+operation_id+'; waiting does not own or cancel it.', file=sys.stderr)
        return self.operation(operation_id, True, kind) if args.wait else {**value, 'status': 'accepted'}


def execute(connection, args):
    if args.command == 'status':
        return connection.operation(args.operation_id, args.wait) if args.operation_id else {'project': connection.current()}
    if args.command == 'close':
        if args.if_match is not None:
            pid, expected = args.project_id, args.if_match
        else:
            current = connection.current()
            if args.project_id and (not current or current['project_id'] != args.project_id):
                raise ServiceError(409, 'project:handle_expired')
            if current is None:
                raise ServiceError(409, 'project:not_open')
            pid, expected = current['project_id'], current['version']
        connection.info['project_id'] = pid
        return connection.start('/api/projects/'+pid+'/close', {}, args, 'project.close', expected)
    grant_id = args.grant_id
    if grant_id is None:
        # A restart between hello and grant cannot authorize a path in a new session.
        granted = pipe_request({'action': 'grant', 'purpose': 'project_create' if args.command == 'init' else 'project_open',
                                'path': str(args.path.absolute()), 'instance_id': connection.info['instance_id']})
        try:
            grant_id = uuid_string(granted['grant_id'])
        except (KeyError, TypeError, ServiceError):
            raise ServiceError(503, 'instance:invalid_grant') from None
    connection.info['grant_id'] = grant_id
    body = {'grant_id': grant_id}
    if args.command == 'init':
        body.update(name=args.name, trigger_word=args.trigger)
    return connection.start('/api/projects' if args.command == 'init' else '/api/projects/open', body, args,
                            'project.create' if args.command == 'init' else 'project.open')


def result_exit(value):
    if value.get('status') == 'failed':
        error = value.get('error') or {}
        if error.get('outcome') == 'unknown':
            return 7
        if error.get('code') in {'project:locked', 'project:exists', 'project:already_open', 'project:handle_expired',
                                 'project:version_mismatch', 'concurrency:version_conflict', 'concurrency:precondition_required'}:
            return 4
        return 5
    return 7 if value.get('status') in ACTIVE | {'canceled', 'interrupted'} else 0


def standalone(args, info):
    from ..project_db.project import create_project
    info.update(mode='standalone', action='init')
    owner = ProductLock()
    try:
        with create_project(args.path, name=args.name, trigger_word=args.trigger) as project:
            info['created'] = True
            metadata, schema = primitive(project.metadata), project.schema_version
        return {'created': True, 'closed': True, 'metadata': metadata, 'schema_version': schema}
    except Exception as exc:
        error = safe_error(exc)
        if info.get('created'):
            error = ServiceError(503, 'project:close_unknown', outcome='unknown')
        raise error from None
    finally:
        owner.close()


def main(argv):
    args = parse(argv)
    info = {'mode': 'attached'}
    try:
        if args.command == 'init' and args.standalone:
            value = standalone(args, info)
        else:
            started = ensure_service(info, timeout=args.startup_timeout or 30) if getattr(args, 'start_service', False) else None
            try:
                credentials = started or pipe_request({'action': 'hello'})
                authority = credentials['authority']
                if (credentials.get('protocol_version') != 1 or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', authority)
                        or not 1 <= int(authority.split(':')[1]) <= 65535 or not isinstance(credentials['token'], str)
                        or not re.fullmatch(r'[\x21-\x7e]+', credentials['token'])):
                    raise ValueError()
                info['instance_id'] = uuid_string(credentials['instance_id'])
            except (ServiceError, OSError, KeyError, TypeError, ValueError):
                raise ReplyError(503, {'code': 'instance:unavailable', 'outcome': 'rejected'}) from None
            if args.instance_id and args.instance_id != info['instance_id']:
                raise ServiceError(409, 'instance:changed')
            with httpx.Client(base_url='http://'+authority, headers={'Authorization': 'Bearer '+credentials['token']},
                              trust_env=False, follow_redirects=False, timeout=30) as client:
                value = execute(Connection(client, info), args)
        code = result_exit(value)
    except ReplyError as exc:
        value = exc.body
        code = 3 if value.get('code') == 'instance:unavailable' else error_exit(exc.status, value)
    except ServiceError as exc:
        value, code = exc.body(str(uuid4())), error_exit(exc.status, {'outcome': exc.outcome})
        if exc.code in ('process:start_timeout', 'instance:invalid_response') and exc.outcome != 'unknown':
            code = 3
    except KeyboardInterrupt:
        value, code = {'code': 'client:interrupted', 'outcome': 'unknown'}, 130
        print('Stopped waiting; accepted service tasks continue. Inspect the recorded operation or request before retrying.', file=sys.stderr)
    except (OSError, httpx.HTTPError):
        unknown = bool(info.get('request'))
        value, code = {'code': 'request:unavailable', 'outcome': 'unknown' if unknown else 'rejected'}, 7 if unknown else 5
    print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
    return code
