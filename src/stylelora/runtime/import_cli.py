"""Reviewed HTTP imports and import-operation lifecycle for attached CLI clients."""
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ..api.contracts import ImportOptionsInput, ImportStart
from ..project_db.validation import digest
from .attached_cli import attach
from .contracts import uuid_string
from .errors import ServiceError
from .instance import pipe_request
from .job_cli import positive
from .prompt_cli import FILE_LIMIT, read_json
from .source_cli import check_source
from .storage_cli import ReplyError, checksum, error_exit, identifier
from . import standalone

KINDS = ('import.preview', 'import.run', 'import.resume', 'import.verify_root')
ACTIVE = ('pending', 'running')
COMMANDS = ('preview', 'run', 'status', 'summary')


def parse(argv):
    parser = argparse.ArgumentParser(prog='stylelora import', description=(
        'Preview and run imports through the existing local service; summary supports --standalone --project PATH. Input files remain read-only. '
        'The legacy import PROJECT ROOT form is still available separately.'))
    sub = parser.add_subparsers(dest='command', required=True)
    preview = sub.add_parser('preview')
    preview.add_argument('root', type=Path, nargs='?')
    preview.add_argument('--grant-id', type=identifier, help='Reuse the original grant after an unknown preview response')
    preview.add_argument('--scheme', type=positive)
    preview.add_argument('--options', type=Path, help='Strict ImportOptionsInput JSON; omitted fields inherit the scheme')
    preview.add_argument('--name', default='Local images')
    preview.add_argument('--idempotency-key', type=identifier)
    run = sub.add_parser('run')
    run.add_argument('--preview', type=Path, required=True)
    run.add_argument('--confirm-digest', type=checksum, required=True)
    waiting = run.add_mutually_exclusive_group()
    waiting.add_argument('--wait', dest='wait', action='store_true', default=True)
    waiting.add_argument('--detach', dest='wait', action='store_false')
    status = sub.add_parser('status')
    status.add_argument('operation_id', type=identifier)
    status.add_argument('--wait', action='store_true')
    summary = sub.add_parser('summary')
    summary.add_argument('source_id', type=positive)
    for command in (preview, status, summary):
        if command is summary:
            standalone.project_options(command, identifier, required=True)
        else:
            command.add_argument('--project-id', type=identifier, required=True)
        command.add_argument('--instance-id', type=identifier)
    for command in (preview, run, status, summary):
        command.add_argument('--json', action='store_true', help='One JSON result on stdout (default)')
    args = parser.parse_args(argv)
    standalone.validate(parser, args)
    if args.command == 'preview':
        if (args.root is None) == (args.grant_id is None):
            parser.error('Supply exactly one of ROOT or --grant-id')
        if (args.grant_id or args.idempotency_key) and not (args.grant_id and args.idempotency_key and args.instance_id):
            parser.error('Preview replay requires original --grant-id, --instance-id and --idempotency-key; omit ROOT')
        if not args.name.strip() or len(args.name) > 512 or '\0' in args.name:
            parser.error('Invalid source name')
    return args


def load_options(path):
    if path is None:
        return None
    try:
        return ImportOptionsInput.model_validate(read_json(path)).model_dump(exclude_unset=True)
    except (ServiceError, ValidationError, ValueError, TypeError):
        raise ServiceError(422, 'import:invalid_options') from None


def check_preview(value):
    try:
        uuid_string(value['preview_id'])
        checksum(value['digest'])
        if type(value['selected']) is not int or not 1 <= value['selected'] <= 10000:
            raise ValueError()
        if type(value['blocking_errors']) is not int or not 0 <= value['blocking_errors'] <= value['selected']:
            raise ValueError()
        if type(value['can_import']) is not bool or value['can_import'] != (value['blocking_errors'] == 0):
            raise ValueError()
        if type(value['scheme_id']) is not int or value['scheme_id'] < 1:
            raise ValueError()
        if not isinstance(value['manifest']['entries'], list) or len(value['manifest']['entries']) != value['selected']:
            raise ValueError()
        if ImportOptionsInput.model_validate(value['options']).model_dump() != value['options']:
            raise ValueError()
    except (KeyError, TypeError, ValueError, ValidationError, ServiceError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def check_report(value, source_id=None):
    try:
        if type(value['source_id']) is not int or value['source_id'] < 1 or (source_id is not None and value['source_id'] != source_id):
            raise ValueError()
        if value['status'] not in ('running', 'paused', 'done', 'canceled'):
            raise ValueError()
        fields = ('selected', 'imported', 'skipped', 'failed', 'pending', 'unresolved')
        if any(type(value[k]) is not int or value[k] < 0 for k in fields):
            raise ValueError()
        if sum(value[k] for k in fields[1:]) != value['selected']:
            raise ValueError()
        if not isinstance(value['items'], list) or len(value['items']) != value['selected']:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def check_operation(value, info, operation_id, kind=None):
    try:
        if value['operation_id'] != operation_id or value['project_id'] != info['project_id']:
            raise ValueError()
        if value['kind'] not in KINDS or (kind and value['kind'] != kind):
            raise ValueError()
        if value['status'] not in (*ACTIVE, 'done', 'failed', 'canceled', 'interrupted'):
            raise ValueError()
        if value['status'] == 'failed':
            if not isinstance(value['error'], dict) or not isinstance(value['error'].get('code'), str) or value['error'].get('outcome') not in ('rejected', 'unknown'):
                raise ValueError()
        result = value.get('result')
        if value['status'] == 'done' or (value['status'] == 'canceled' and result is not None):
            if value['kind'] == 'import.preview':
                check_preview(result)
            elif value['kind'] == 'import.verify_root':
                if type(result['verified']) is not bool:
                    raise ValueError()
                if result['verified'] and (type(result['source_id']) is not int or result['source_id'] < 1
                        or ('source_id' in info and result['source_id'] != info['source_id'])):
                    raise ValueError()
            else:
                check_report(result, info.get('source_id'))
            if 'source_id' in result:
                info['source_id'] = result['source_id']
    except (KeyError, TypeError, ValueError, ServiceError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def operation(connection, operation_id, wait, kind=None):
    path = '/api/operations/'+operation_id
    connection.info.update(operation_id=operation_id, status_url=path)
    while True:
        value = check_operation(connection.request('GET', path), connection.info, operation_id, kind)
        if not wait or value['status'] not in ACTIVE:
            return value
        time.sleep(.1)


def start_operation(connection, path, body, kind, wait, *, key=None, expected=None):
    value = connection.request('POST', path, body=body, key=key, expected=expected, status=202)
    try:
        operation_id = uuid_string(value['task']['id'])
        if value['task'] != {'kind': 'operation', 'id': operation_id, 'project_id': connection.info['project_id']}:
            raise ValueError()
        if value['status_url'] != '/api/operations/'+operation_id:
            raise ValueError()
    except (KeyError, TypeError, ValueError, ServiceError):
        raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
    connection.info.update(operation_id=operation_id, status_url=value['status_url'])
    print(standalone.progress('Accepted '+kind+' operation '+operation_id+'; stopping this client does not cancel it.'), file=sys.stderr)
    return operation(connection, operation_id, True, kind) if wait else {**value, 'status': 'accepted'}


def root_grant(connection, path, previous=None):
    grant_id = previous
    if grant_id is None:
        granted = pipe_request({'action': 'grant', 'purpose': 'import_root', 'path': str(path.absolute()),
                                'project_id': connection.info['project_id'], 'instance_id': connection.info['instance_id']})
        try:
            grant_id = uuid_string(granted['grant_id'])
            if granted['purpose'] != 'import_root' or granted['project_id'] != connection.info['project_id']:
                raise ValueError()
        except (KeyError, TypeError, ValueError, ServiceError):
            raise ServiceError(503, 'instance:invalid_grant') from None
    connection.info['grant_id'] = grant_id
    return grant_id


def plan_preview(connection, value, name):
    plan = {'schema_version': 1, 'instance_id': connection.info['instance_id'], 'project_id': connection.info['project_id'],
            'idempotency_key': str(uuid4()), 'preview': value,
            'body': {'preview_id': value['preview_id'], 'digest': value['digest'], 'name': name}}
    return {'plan': plan, 'confirmation_digest': digest(plan)}


def read_plan(args):
    try:
        document = read_json(args.preview)
        plan = document['plan']
        if set(plan) != {'schema_version', 'instance_id', 'project_id', 'idempotency_key', 'body', 'preview'}:
            raise ValueError()
        if type(plan['schema_version']) is not int or plan['schema_version'] != 1:
            raise ValueError()
        for key in ('instance_id', 'project_id', 'idempotency_key'):
            uuid_string(plan[key])
        body = ImportStart.model_validate(plan['body']).model_dump()
        preview = check_preview(plan['preview'])
        if (body != plan['body'] or body['preview_id'] != preview['preview_id'] or body['digest'] != preview['digest']
                or not body['name'].strip() or '\0' in body['name']):
            raise ValueError()
        if document['confirmation_digest'] != digest(plan) or args.confirm_digest != digest(plan):
            raise ValueError()
        return plan
    except (KeyError, TypeError, ValueError, ServiceError, ValidationError, RecursionError):
        raise ServiceError(422, 'import:invalid_plan') from None


def report_exit(value):
    if value['status'] != 'done' or value['pending'] or value['unresolved']:
        return 7
    return 6 if value['failed'] else 0


def operation_exit(value):
    if value['status'] == 'accepted':
        return 0
    if value['status'] == 'failed':
        error = value['error']
        if error['outcome'] == 'unknown':
            return 7
        if error['code'] in ('concurrency:version_conflict', 'concurrency:precondition_required', 'state:conflict',
                             'project:handle_expired', 'path:identity_changed', 'preview:stale', 'preview:expired'):
            return 4
        return 5
    if value['status'] != 'done':
        return 7
    if value['kind'] == 'import.preview':
        return 0 if value['result']['can_import'] else 6
    if value['kind'] == 'import.verify_root':
        return 0 if value['result']['verified'] else 7
    return report_exit(value['result'])


def execute(connection, args, prepared):
    base = connection.project(prepared['project_id'] if args.command == 'run' else args.project_id, resource='imports')
    if args.command == 'preview':
        grant_id = root_grant(connection, args.root, args.grant_id)
        value = start_operation(connection, base+'/preview', {'root_handle': grant_id, 'scheme_id': args.scheme, 'options': prepared},
                                'import.preview', True, key=args.idempotency_key)
        return plan_preview(connection, value['result'], args.name) if value['status'] == 'done' else value
    if args.command == 'run':
        if not prepared['preview']['can_import']:
            raise ServiceError(409, 'import:preview_blocked')
        return start_operation(connection, base, prepared['body'], 'import.run', args.wait, key=prepared['idempotency_key'])
    if args.command == 'status':
        return operation(connection, args.operation_id, args.wait)
    connection.info['source_id'] = args.source_id
    source = check_source(connection.request('GET', base.replace('/imports', '/sources')+'/'+str(args.source_id)), args.source_id)
    if source['type'] != 'import':
        raise ServiceError(422, 'source:not_import')
    return check_report(connection.request('GET', base+'/'+str(args.source_id)+'/summary'), args.source_id)


def main(argv):
    args, info = parse(argv), {'mode': 'attached'}
    try:
        prepared = load_options(args.options) if args.command == 'preview' else read_plan(args) if args.command == 'run' else None
        with standalone.connect(args, info, prepared['instance_id'] if args.command == 'run' else args.instance_id, attach) as connection:
            value = execute(connection, args, prepared)
        if 'plan' in value:
            code = 0 if value['plan']['preview']['can_import'] else 6
            if len(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2).encode('utf-8')) > FILE_LIMIT:
                info.pop('request', None)
                raise ServiceError(413, 'import:plan_too_large')
        else:
            code = report_exit(value) if args.command == 'summary' else operation_exit(value)
    except ReplyError as exc:
        value = exc.body
        code = 3 if value.get('code') == 'instance:unavailable' else error_exit(exc.status, value)
    except ServiceError as exc:
        value, code = exc.body(str(uuid4())), error_exit(exc.status, {'outcome': exc.outcome})
    except KeyboardInterrupt:
        value, code = {'code': 'client:interrupted', 'outcome': 'unknown'}, 130
        print(standalone.interruption(args, 'Stopped waiting; accepted imports continue. Inspect the operation and source before retrying.'), file=sys.stderr)
    except (OSError, httpx.HTTPError):
        unknown = bool(info.get('request'))
        value, code = {'code': 'request:unavailable', 'outcome': 'unknown' if unknown else 'rejected'}, 7 if unknown else 5
    print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
    return code
