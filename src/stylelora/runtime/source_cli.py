"""Attached source controls, reviewed generation handoff and explicit cost consent."""
import argparse
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ..generation.types import Draft, HandoffStart
from ..project_db.validation import digest
from .attached_cli import attach
from .contracts import uuid_string
from .errors import ServiceError
from .job_cli import check_page, positive
from .project_cli import version
from .prompt_cli import FILE_LIMIT, read_json as read_bounded_json
from .storage_cli import ReplyError, checksum, error_exit, identifier, page_limit
from . import standalone

STATES = ('draft', 'running', 'paused', 'done', 'canceled')
CONTROLS = ('pause', 'resume', 'cancel', 'confirm-cost', 'set-cost-policy', 'import-root')
ITEM_COMMANDS = ('item', 'retry-item')
WRITES = (*CONTROLS, 'update', 'retry-item')


def cycle(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise argparse.ArgumentTypeError('cycle-id must be 32 lowercase hexadecimal characters')
    return value


def parse(argv):
    parser = argparse.ArgumentParser(prog='stylelora source', description=(
        'Use the source service; list/status/items/item/cost-state also support --standalone --project PATH. '
        'Generation starts only from a reviewed prompt-task handoff. '
        'Create imports with import preview/run; import controls and root verification are available here.'))
    sub = parser.add_subparsers(dest='command', required=True)
    commands = {name: sub.add_parser(name) for name in
                ('capabilities', 'create', 'update', 'list', 'status', 'items', 'preview', 'run', 'reconcile', 'cost-state', *CONTROLS, *ITEM_COMMANDS)}
    create = commands['create']
    create.add_argument('--type', choices=('nai',), required=True)
    create.add_argument('--spec', type=Path, required=True, help='Strict generation Draft JSON')
    create.add_argument('--confirm-allow', action='store_true', help='Explicitly permit cost_policy=allow in this draft')
    create.add_argument('--idempotency-key', type=identifier)
    commands['update'].add_argument('--spec', type=Path, required=True, help='Complete replacement Draft JSON')
    commands['update'].add_argument('--confirm-allow', action='store_true')
    commands['retry-item'].add_argument('--expected-attempt', type=positive, required=True,
                                       help='Authorize exactly one retry of this failed attempt; does not start a runner')
    preview = commands['preview']
    preview.add_argument('--task-id', type=identifier, required=True)
    preview.add_argument('--expected-task-revision', type=positive, required=True)
    preview.add_argument('--if-match', type=version)
    for name, command in commands.items():
        command.add_argument('--json', action='store_true', help='Single JSON stdout result (default)')
        if name != 'run':
            command.add_argument('--instance-id', type=identifier)
        if name not in ('capabilities', 'run'):
            if name in ('list', 'status', 'items', 'item', 'cost-state'):
                standalone.project_options(command, identifier, required=name != 'list')
            else:
                command.add_argument('--project-id', type=identifier, required=name != 'list')
        if name not in ('capabilities', 'create', 'list', 'run', 'reconcile', *ITEM_COMMANDS):
            command.add_argument('source_id', type=positive)
        if name in ITEM_COMMANDS:
            command.add_argument('item_id', type=positive)
        if name in ('run', 'reconcile'):
            command.add_argument('--preview', type=Path, required=True)
            command.add_argument('--confirm-digest', type=checksum, required=True)
        if name in WRITES:
            command.add_argument('--if-match', type=version, required=name == 'update',
                                 help='Version from the original source/item read')
            command.add_argument('--idempotency-key', type=identifier)
        if name in ('run', 'resume', 'confirm-cost', 'import-root'):
            waiting = command.add_mutually_exclusive_group()
            waiting.add_argument('--wait', dest='wait', action='store_true', default=True)
            waiting.add_argument('--detach', dest='wait', action='store_false')
        if name in ('list', 'items'):
            command.add_argument('--limit', type=page_limit, default=50)
            command.add_argument('--cursor')
    commands['status'].add_argument('--wait', action='store_true')
    commands['confirm-cost'].add_argument('--cycle-id', type=cycle, required=True)
    commands['set-cost-policy'].add_argument('--policy', choices=('deny', 'ask', 'allow'), required=True)
    commands['set-cost-policy'].add_argument('--confirm-allow', action='store_true')
    commands['import-root'].add_argument('root', type=Path, nargs='?')
    commands['import-root'].add_argument('--grant-id', type=identifier)
    args = parser.parse_args(argv)
    standalone.validate(parser, args)
    if getattr(args, 'idempotency_key', None):
        if not args.instance_id or (args.command in WRITES and not args.if_match):
            parser.error('Replay requires original --instance-id and, for controls, original --if-match')
    if getattr(args, 'cursor', None) is not None and not 1 <= len(args.cursor) <= 4096:
        parser.error('cursor must contain 1 to 4096 characters')
    if args.command == 'set-cost-policy' and args.confirm_allow != (args.policy == 'allow'):
        parser.error('--confirm-allow is required exactly when --policy allow is selected')
    if args.command == 'import-root':
        if (args.root is None) == (args.grant_id is None):
            parser.error('Supply exactly one of ROOT or --grant-id')
        if (args.grant_id or args.idempotency_key) and not (args.grant_id and args.idempotency_key and args.instance_id and args.if_match):
            parser.error('Root replay requires original --grant-id, --instance-id, --if-match and --idempotency-key; omit ROOT')
    return args


def read_draft(args):
    try:
        model = Draft.model_validate(read_bounded_json(args.spec))
        value = model.model_dump()
        if args.command == 'create' and 'cost_policy' not in model.model_fields_set:
            value.pop('cost_policy')
        elif args.confirm_allow != (value['cost_policy'] == 'allow'):
            raise ValueError()
        return value
    except (ServiceError, ValidationError, TypeError, ValueError):
        raise ServiceError(422, 'source:invalid_spec') from None


def check_source(value, source_id=None, *, nai=False):
    try:
        if type(value['source_id']) is not int or value['source_id'] < 1 or (source_id is not None and value['source_id'] != source_id):
            raise ValueError()
        version(value['version'])
        if value['status'] not in STATES or value['type'] not in ('nai', 'import') or type(value['locked']) is not bool:
            raise ValueError()
        if nai and value['type'] != 'nai':
            raise ServiceError(422, 'source:not_generation')
        if not isinstance(value['counts'], dict) or set(value['counts']) != {'pending', 'sent', 'done', 'failed', 'skipped'}:
            raise ValueError()
        if any(type(n) is not int or n < 0 for n in value['counts'].values()):
            raise ValueError()
        if value['cost_policy'] not in ('ask', 'deny', 'allow'):
            raise ValueError()
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def check_item(value, item_id):
    try:
        if type(value['item_id']) is not int or value['item_id'] != item_id:
            raise ValueError()
        if any(type(value[k]) is not int or value[k] < 1 for k in ('source_id', 'seq')):
            raise ValueError()
        if type(value['attempts']) is not int or value['attempts'] < 0:
            raise ValueError()
        if value['status'] not in ('pending', 'sent', 'done', 'failed', 'skipped'):
            raise ValueError()
        if value['image_id'] is not None and (type(value['image_id']) is not int or value['image_id'] < 1):
            raise ValueError()
        if value['error'] is not None and not isinstance(value['error'], str):
            raise ValueError()
        version(value['version'])
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def execute_item(connection, args):
    base = connection.project(args.project_id, resource='source-items')
    path = base+'/'+str(args.item_id)
    connection.info.update(item_id=args.item_id, status_url=path)
    current = check_item(connection.request('GET', path), args.item_id)
    connection.info['source_id'] = current['source_id']
    if args.command == 'item':
        return current
    value = connection.request('POST', path+'/retry', body={'expected_attempt': args.expected_attempt},
                               expected=args.if_match or current['version'], key=args.idempotency_key)
    try:
        check_item(value, args.item_id)
        if (value['source_id'] != current['source_id'] or value['status'] != 'pending'
                or value['attempts'] != args.expected_attempt + 1 or value['error'] is not None or value['image_id'] is not None):
            raise ServiceError(503, 'request:invalid_response')
    except ServiceError:
        raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
    return value


def check_preview(value, source_id):
    try:
        body = HandoffStart.model_validate({k: value[k] for k in HandoffStart.model_fields}).model_dump()
        version(value['source_version'])
        if type(value['source_id']) is not int or value['source_id'] != source_id or type(value['can_start']) is not bool:
            raise ValueError()
        if not isinstance(value['blockers'], list) or any(not isinstance(v, str) for v in value['blockers']):
            raise ValueError()
        if value['can_start'] != (not value['blockers']) or value['cost_policy'] not in ('deny', 'ask', 'allow'):
            raise ValueError()
        return body
    except (KeyError, TypeError, ValueError, ValidationError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None


def read_plan(args):
    try:
        document = read_bounded_json(args.preview)
        plan = document['plan']
        if set(plan) != {'schema_version', 'instance_id', 'project_id', 'source_id', 'idempotency_key', 'body', 'preview'}:
            raise ValueError()
        if type(plan['schema_version']) is not int or plan['schema_version'] != 1 or type(plan['source_id']) is not int or plan['source_id'] < 1:
            raise ValueError()
        for name in ('instance_id', 'project_id', 'idempotency_key'):
            uuid_string(plan[name])
        body = HandoffStart.model_validate(plan['body']).model_dump()
        if body != plan['body'] or body != check_preview(plan['preview'], plan['source_id']):
            raise ValueError()
        if document['confirmation_digest'] != digest(plan) or args.confirm_digest != digest(plan):
            raise ValueError()
        return plan
    except (KeyError, TypeError, ValueError, ValidationError, ServiceError, RecursionError):
        raise ServiceError(422, 'source:invalid_plan') from None


def check_cost(value, source_id):
    try:
        if type(value['source_id']) is not int or value['source_id'] != source_id or value['status'] not in STATES:
            raise ValueError()
        version(value['source_version'])
        if value['cycle_id'] is not None:
            cycle(value['cycle_id'])
        if value['cost_policy'] not in ('ask', 'deny', 'allow'):
            raise ValueError()
        if any(type(value[k]) is not bool for k in ('acknowledged', 'confirmation_required', 'can_confirm')):
            raise ValueError()
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def poll(connection, path, wait):
    while True:
        value = check_source(connection.request('GET', path), connection.info['source_id'])
        if not wait or value['status'] != 'running':
            if value['status'] == 'paused' and value['type'] == 'nai':
                value['cost_state'] = check_cost(connection.request('GET', path+'/cost-state'), value['source_id'])
            return value
        time.sleep(.1)


def checked_write(connection, path, source_id, *, method='POST', **kw):
    value = connection.request(method, path, **kw)
    try:
        return check_source(value, source_id, nai=True)
    except ServiceError:
        raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None


def execute(connection, args, prepared):
    info = connection.info
    if args.command in ITEM_COMMANDS:
        return execute_item(connection, args)
    if args.command == 'capabilities':
        value = connection.request('GET', '/api/generation/capabilities')
        if any(type(value.get(k)) is not bool for k in ('drafts', 'handoff_preview', 'generation')):
            raise ServiceError(503, 'request:invalid_response')
        return value
    base = connection.project(prepared['project_id'] if args.command == 'run' else args.project_id, resource='sources')
    if args.command == 'create':
        if 'cost_policy' not in prepared:
            policy = connection.request('GET', '/api/config')['cost_policy']
            if args.confirm_allow != (policy == 'allow'):
                raise ServiceError(422, 'source:default_cost_confirmation_required')
            prepared = {**prepared, 'cost_policy': policy}
        value = checked_write(connection, base, None, body=prepared, key=args.idempotency_key, status=201)
        info.update(source_id=value['source_id'], status_url=base+'/'+str(value['source_id']))
        return value
    if args.command == 'list':
        return check_page(connection.request('GET', base, params={k: v for k, v in
            {'limit': args.limit, 'cursor': args.cursor}.items() if v is not None}))
    source_id = prepared['source_id'] if args.command in ('run', 'reconcile') else args.source_id
    path = base+'/'+str(source_id)
    info.update(source_id=source_id, status_url=path)
    if args.command == 'update':
        return checked_write(connection, path, source_id, method='PUT', body=prepared,
                             expected=args.if_match, key=args.idempotency_key)
    if args.command == 'status':
        return poll(connection, path, args.wait)
    if args.command == 'items':
        return check_page(connection.request('GET', path+'/items', params={k: v for k, v in
            {'limit': args.limit, 'cursor': args.cursor}.items() if v is not None}))
    if args.command == 'cost-state':
        return check_cost(connection.request('GET', path+'/cost-state'), source_id)
    if args.command in ('run', 'reconcile'):
        body = prepared['body']
        if args.command == 'reconcile':
            # Explicit new project handle permits read-only projection comparison after restart.
            value = checked_write(connection, path+'/reconcile-start', source_id,
                                  body={k: body[k] for k in ('source_digest', 'seeds_digest', 'count')})
        else:
            if not prepared['preview']['can_start']:
                raise ServiceError(409, 'source:preview_blocked')
            value = checked_write(connection, path+'/start', source_id, body=body,
                expected=prepared['preview']['source_version'], key=prepared['idempotency_key'])
        if type(value.get('reconciled')) is not bool or type(value.get('dispatched')) is not bool:
            raise ServiceError(503, 'request:outcome_unknown', outcome='unknown')
        if (value['reconciled'] != value['locked'] and args.command == 'reconcile'
                or value['reconciled'] and value['dispatched']
                or args.command == 'reconcile' and value['dispatched']
                or args.command == 'run' and not value['locked']):
            raise ServiceError(503, 'request:outcome_unknown', outcome='unknown')
        if args.command == 'reconcile':
            return value
    else:
        current = check_source(connection.request('GET', path), source_id,
                               nai=args.command not in ('pause', 'resume', 'cancel', 'import-root'))
        expected = args.if_match or current['version']
        if args.command == 'import-root':
            from .import_cli import root_grant, start_operation
            if current['type'] != 'import':
                raise ServiceError(422, 'source:not_import')
            grant_id = root_grant(connection, args.root, args.grant_id)
            return start_operation(connection, path+'/import-root', {'root_handle': grant_id}, 'import.verify_root', args.wait,
                                   key=args.idempotency_key, expected=expected)
        if current['type'] == 'import':
            if args.command == 'resume':
                from .import_cli import start_operation
                return start_operation(connection, path+'/control', {'action': 'resume'}, 'import.resume', args.wait,
                                       key=args.idempotency_key, expected=expected)
            value = connection.request('POST', path+'/control', body={'action': args.command}, key=args.idempotency_key, expected=expected)
            try:
                check_source(value, source_id)
                if value['type'] != 'import':
                    raise ServiceError(503, 'request:invalid_response')
            except ServiceError:
                raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
            return value
        if args.command == 'preview':
            value = connection.request('POST', path+'/handoff-preview', expected=expected,
                body={'task_id': args.task_id, 'expected_task_revision': args.expected_task_revision})
            body = check_preview(value, source_id)
            if value['source_version'] != expected:
                raise ServiceError(503, 'request:invalid_response')
            plan = {'schema_version': 1, 'instance_id': info['instance_id'], 'project_id': info['project_id'],
                    'source_id': source_id, 'idempotency_key': str(uuid4()), 'body': body, 'preview': value}
            return {'plan': plan, 'confirmation_digest': digest(plan)}
        if args.command == 'set-cost-policy':
            return checked_write(connection, path, source_id, method='PATCH', expected=expected, key=args.idempotency_key,
                                 body={'cost_policy': args.policy, 'confirm_allow': args.confirm_allow})
        if args.command == 'confirm-cost':
            value = checked_write(connection, path+'/confirm-cost', source_id, expected=expected,
                                  key=args.idempotency_key, body={'cycle_id': args.cycle_id})
        else:
            value = checked_write(connection, path+'/control', source_id, expected=expected,
                                  key=args.idempotency_key, body={'action': args.command})
    if args.command in ('run', 'resume', 'confirm-cost'):
        print(standalone.progress('Source '+str(source_id)+' status: '+value['status']+'; stopping this client does not cancel it.'), file=sys.stderr)
        if args.wait:
            observed = poll(connection, path, True)
            return {**observed, **{k: value[k] for k in ('reconciled', 'dispatched') if k in value}}
    return value


def result_exit(value, command, wait=False):
    if 'operation_id' in value or value.get('status') == 'accepted':
        from .import_cli import operation_exit
        return operation_exit(value)
    if command == 'preview':
        return 0 if value['plan']['preview']['can_start'] else 6
    if command == 'reconcile':
        return 0 if value['reconciled'] else 7
    if command in ('capabilities', 'create', 'update', 'list', 'items', 'cost-state', 'set-cost-policy', *ITEM_COMMANDS):
        return 0
    if command in ('pause', 'cancel') and value['status'] == ('paused' if command == 'pause' else 'canceled'):
        return 0
    if command in ('run', 'resume', 'confirm-cost') and not wait and value['status'] == 'running':
        return 0
    if value['status'] == 'done':
        return 6 if value['counts']['failed'] else 0
    return 7


def main(argv):
    args = parse(argv)
    info = {'mode': 'attached'}
    try:
        prepared = read_draft(args) if args.command in ('create', 'update') else read_plan(args) if args.command in ('run', 'reconcile') else None
        instance = prepared['instance_id'] if args.command == 'run' else args.instance_id
        with standalone.connect(args, info, instance, attach) as connection:
            value = standalone.collect(args, info, lambda current: execute(connection, current, prepared))
        code = result_exit(value, args.command, getattr(args, 'wait', False))
        if args.command == 'preview' and len(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2).encode('utf-8')) > FILE_LIMIT:
            info.pop('request', None)
            raise ServiceError(413, 'source:plan_too_large')
    except ReplyError as exc:
        value = exc.body
        code = 3 if value.get('code') == 'instance:unavailable' else error_exit(exc.status, value)
    except ServiceError as exc:
        value, code = exc.body(str(uuid4())), error_exit(exc.status, {'outcome': exc.outcome})
    except KeyboardInterrupt:
        value, code = {'code': 'client:interrupted', 'outcome': 'unknown'}, 130
        print(standalone.interruption(args, 'Stopped waiting; accepted work continues. Inspect the source and request before retrying.'), file=sys.stderr)
    except (OSError, httpx.HTTPError):
        unknown = bool(info.get('request'))
        value, code = {'code': 'request:unavailable', 'outcome': 'unknown' if unknown else 'rejected'}, 7 if unknown else 5
    print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
    return code
