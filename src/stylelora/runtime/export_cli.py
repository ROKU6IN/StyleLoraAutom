"""Reviewed HTTP export plans, explicit acknowledgements and durable reports."""
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ..api.contracts import ExportOptionsInput, ExportStart
from ..project_db.validation import digest
from .attached_cli import attach
from .contracts import uuid_string
from .errors import ServiceError
from .job_cli import check_page, positive
from .project_cli import version
from .prompt_cli import FILE_LIMIT, read_json
from .storage_cli import ReplyError, checksum, error_exit, identifier, page_limit
from . import standalone

ACTIVE = ('planned', 'running')
STATES = (*ACTIVE, 'finished', 'failed', 'canceled', 'interrupted', 'cleaned', 'cleanup_partial')


def parse(argv):
    parser = argparse.ArgumentParser(prog='stylelora export', description=(
        'Preview and run exports through the local service; list/status/report support --standalone --project PATH. '
        'Review trigger and hardlink acknowledgements separately. '
        'The legacy export PROJECT form remains available.'))
    sub = parser.add_subparsers(dest='command', required=True)
    preview = sub.add_parser('preview')
    preview.add_argument('--scheme', type=positive)
    preview.add_argument('--options', type=Path, help='Strict ExportOptionsInput JSON')
    preview.add_argument('--operation-id', type=identifier, help='Recover an accepted preview by GET without resubmitting')
    preview.add_argument('--idempotency-key', type=identifier)
    run = sub.add_parser('run')
    run.add_argument('--preview', type=Path, required=True)
    run.add_argument('--confirm-digest', type=checksum, required=True)
    run.add_argument('--accept-trigger-warning', type=checksum)
    run.add_argument('--accept-hardlink', type=checksum)
    listing = sub.add_parser('list')
    listing.add_argument('--limit', type=page_limit, default=50)
    listing.add_argument('--cursor')
    commands = {name: sub.add_parser(name) for name in ('status', 'report', 'cancel')}
    for command in commands.values():
        command.add_argument('export_id', type=identifier)
    commands['status'].add_argument('--wait', action='store_true')
    commands['cancel'].add_argument('--if-match', type=version)
    commands['cancel'].add_argument('--idempotency-key', type=identifier)
    for command in (run, commands['cancel']):
        waiting = command.add_mutually_exclusive_group()
        waiting.add_argument('--wait', dest='wait', action='store_true', default=True)
        waiting.add_argument('--detach', dest='wait', action='store_false')
    for command in (preview, listing, *commands.values()):
        if command in (listing, commands['status'], commands['report']):
            standalone.project_options(command, identifier, required=command is not listing)
        else:
            command.add_argument('--project-id', type=identifier, required=command is not listing)
        command.add_argument('--instance-id', type=identifier)
    for command in (preview, run, listing, *commands.values()):
        command.add_argument('--json', action='store_true', help='Single JSON stdout result (default)')
    args = parser.parse_args(argv)
    standalone.validate(parser, args)
    if args.command == 'preview':
        if args.operation_id and (args.scheme is not None or args.options is not None or args.idempotency_key):
            parser.error('--operation-id cannot be combined with preview inputs or request keys')
        if args.idempotency_key and not args.instance_id:
            parser.error('Preview replay requires original --instance-id with the request key')
    if args.command == 'cancel' and args.idempotency_key and (not args.if_match or not args.instance_id):
        parser.error('Cancel replay requires original --if-match and --instance-id with the request key')
    if getattr(args, 'cursor', None) is not None and not 1 <= len(args.cursor) <= 4096:
        parser.error('cursor must contain 1 to 4096 characters')
    return args


def load_options(path):
    try:
        value = ExportOptionsInput.model_validate(read_json(path) if path else {}).model_dump()
        if value['allow_existing_trigger']:
            raise ValueError()  # Only a current explicit digest can acknowledge the warning.
        return value
    except (ServiceError, ValidationError, TypeError, ValueError):
        raise ServiceError(422, 'export:invalid_options') from None


def relative_directory(value):
    if (not isinstance(value, str) or not value.startswith('exports/') or '\\' in value or ':' in value
            or any(ord(c) < 32 for c in value) or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError()


def counts(value, *, complete=False):
    for field in ('selected', 'exported', 'skipped', 'failed'):
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError()
    total = sum(value[k] for k in ('exported', 'skipped', 'failed'))
    if total > value['selected'] or complete and total != value['selected']:
        raise ValueError()


def check_preview(value):
    try:
        uuid_string(value['preview_id'])
        checksum(value['digest'])
        checksum(value['plan_digest'])
        if value['dry_run'] is not True or type(value['scheme']['scheme_id']) is not int or value['scheme']['scheme_id'] < 1:
            raise ValueError()
        mode = value['output_config']['image_mode']
        if mode not in ('copy', 'hardlink') or value['hardlink_ack_digest'] != (value['plan_digest'] if mode == 'hardlink' else None):
            raise ValueError()
        trigger = value['trigger_validation']
        checksum(trigger['digest'])
        if trigger['status'] not in ('valid', 'unverifiable', 'existing_tag', 'invalid') or type(trigger['can_override']) is not bool:
            raise ValueError()
        if trigger['can_override'] != (trigger['status'] == 'existing_tag'):
            raise ValueError()
        if trigger['override_digest'] != (trigger['digest'] if trigger['can_override'] else None):
            raise ValueError()
        if not isinstance(value['images'], list):
            raise ValueError()
        for row in value['images']:
            if type(row['image_id']) is not int or row['image_id'] < 1 or row['status'] not in ('ready', 'skipped', 'failed'):
                raise ValueError()
    except (KeyError, TypeError, ValueError, ServiceError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def submission(preview):
    return {'scheme_id': preview['scheme']['scheme_id'], 'preview_id': preview['preview_id'], 'digest': preview['digest'],
            'trigger_override_digest': None, 'hardlink_ack_digest': None}


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
        body = ExportStart.model_validate(plan['body']).model_dump()
        if body != plan['body'] or body != submission(check_preview(plan['preview'])):
            raise ValueError()
        if document['confirmation_digest'] != digest(plan) or args.confirm_digest != digest(plan):
            raise ValueError()
    except (KeyError, TypeError, ValueError, ValidationError, ServiceError, RecursionError):
        raise ServiceError(422, 'export:invalid_plan') from None
    trigger = plan['preview']['trigger_validation']
    if trigger['status'] == 'invalid':
        raise ServiceError(409, 'export:preview_blocked')
    if args.accept_trigger_warning != trigger['override_digest']:
        raise ServiceError(422, 'trigger:confirmation_mismatch')
    if args.accept_hardlink != plan['preview']['hardlink_ack_digest']:
        raise ServiceError(422, 'export:hardlink_confirmation_mismatch')
    return plan


def check_export(value, export_id, base):
    try:
        if uuid_string(value['export_id']) != export_id or value['status'] not in STATES:
            raise ValueError()
        version(value['version'])
        counts(value, complete=value['status'] == 'finished')
        if value['image_mode'] not in ('copy', 'hardlink'):
            raise ValueError()
        if value['operation_id'] is not None:
            uuid_string(value['operation_id'])
        if value['relative_dir'] is not None:
            relative_directory(value['relative_dir'])
        if value['report_url'] not in (None, base+'/'+export_id+'/report'):
            raise ValueError()
        if value['status'] == 'finished' and (value['relative_dir'] is None or value['report_url'] is None):
            raise ValueError()
    except (KeyError, TypeError, ValueError, ServiceError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def check_operation(value, operation_id, project_id, kind):
    try:
        if value['operation_id'] != operation_id or value['project_id'] != project_id or value['kind'] != kind:
            raise ValueError()
        if value['status'] not in ('pending', 'running', 'done', 'failed', 'canceled', 'interrupted'):
            raise ValueError()
        if value['status'] == 'failed' and (not isinstance(value['error'], dict) or not isinstance(value['error'].get('code'), str)
                or value['error'].get('outcome') not in ('unknown', 'rejected')):
            raise ValueError()
        if kind == 'export.preview' and value['status'] == 'done':
            check_preview(value['result'])
    except (KeyError, TypeError, ValueError, ServiceError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def preview_operation(connection, operation_id):
    path = '/api/operations/'+operation_id
    connection.info.update(operation_id=operation_id, status_url=path)
    while True:
        value = check_operation(connection.request('GET', path), operation_id, connection.info['project_id'], 'export.preview')
        if value['status'] not in ('pending', 'running'):
            return value
        time.sleep(.1)


def poll(connection, base, export_id, wait):
    while True:
        value = check_export(connection.request('GET', base+'/'+export_id), export_id, base)
        if not wait or value['status'] not in ACTIVE:
            if value['status'] == 'failed' and value['operation_id'] is not None:
                op = value['operation_id']
                value['operation'] = check_operation(connection.request('GET', '/api/operations/'+op), op,
                    connection.info['project_id'], 'export.run')
                if wait and value['operation']['status'] in ('pending', 'running'):
                    time.sleep(.1)
                    continue
            return value
        time.sleep(.1)


def check_report(value, export_id):
    try:
        if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['export_id'] != export_id or value['status'] != 'complete':
            raise ValueError()
        counts(value['counts'], complete=True)
        relative_directory(value['directory'])
        if not isinstance(value['images'], list) or len(value['images']) != value['counts']['selected']:
            raise ValueError()
        if value['output_config']['image_mode'] not in ('copy', 'hardlink'):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def execute(connection, args, plan):
    info = connection.info
    base = connection.project(plan['project_id'] if args.command == 'run' else args.project_id, resource='exports')
    if args.command == 'preview':
        operation_id = args.operation_id
        if operation_id is None:
            value = connection.request('POST', base+'/preview', body={'scheme_id': args.scheme, 'options': plan}, key=args.idempotency_key, status=202)
            try:
                operation_id = uuid_string(value['task']['id'])
                if value['task'] != {'kind': 'operation', 'id': operation_id, 'project_id': info['project_id']} or value['status_url'] != '/api/operations/'+operation_id:
                    raise ValueError()
            except (KeyError, TypeError, ValueError, ServiceError):
                raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
            print('Accepted export preview operation '+operation_id+'.', file=sys.stderr)
        value = preview_operation(connection, operation_id)
        if value['status'] != 'done':
            return value
        reviewed = {'schema_version': 1, 'instance_id': info['instance_id'], 'project_id': info['project_id'],
                    'idempotency_key': str(uuid4()), 'body': submission(value['result']), 'preview': value['result']}
        return {'plan': reviewed, 'confirmation_digest': digest(reviewed)}
    if args.command == 'list':
        value = check_page(connection.request('GET', base, params={k: v for k, v in {'limit': args.limit, 'cursor': args.cursor}.items() if v is not None}))
        for row in value['items']:
            check_export(row, row.get('export_id') if isinstance(row, dict) else None, base)
        return value
    if args.command == 'run':
        body = {**plan['body'], 'trigger_override_digest': args.accept_trigger_warning, 'hardlink_ack_digest': args.accept_hardlink}
        value = connection.request('POST', base, body=body, key=plan['idempotency_key'], status=202)
        try:
            export_id = uuid_string(value['export_id'])
            check_export(value, export_id, base)
            if value['status_url'] != base+'/'+export_id or value['operation_id'] is None:
                raise ValueError()
        except (KeyError, TypeError, ValueError, ServiceError):
            raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
        info.update(export_id=export_id, operation_id=value['operation_id'], status_url=base+'/'+export_id)
        print(standalone.progress('Accepted export '+export_id+'; stopping this client does not cancel it.'), file=sys.stderr)
        return poll(connection, base, export_id, True) if args.wait else {**value, 'status': 'accepted', 'export_status': value['status']}
    export_id = args.export_id
    path = base+'/'+export_id
    info.update(export_id=export_id, status_url=path)
    if args.command == 'status':
        return poll(connection, base, export_id, args.wait)
    if args.command == 'report':
        return check_report(connection.request('GET', path+'/report'), export_id)
    expected = args.if_match or poll(connection, base, export_id, False)['version']
    value = connection.request('POST', path+'/cancel', body={}, expected=expected, key=args.idempotency_key)
    try:
        check_export(value, export_id, base)
    except ServiceError:
        raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
    return poll(connection, base, export_id, True) if args.wait else {**value, 'cancel_requested': True}


def failure_exit(value):
    error = value.get('error') or {}
    if error.get('outcome') == 'unknown':
        return 7
    if error.get('code') in ('concurrency:version_conflict', 'concurrency:precondition_required', 'preview:stale', 'preview:expired', 'state:conflict'):
        return 4
    return 5


def result_exit(value, command, wait=False):
    if 'plan' in value:
        preview = value['plan']['preview']
        return 6 if preview['trigger_validation']['status'] == 'invalid' or any(row['status'] == 'failed' for row in preview['images']) else 0
    if command == 'list':
        return 0
    if command == 'report':
        return 6 if value['counts']['failed'] else 0
    state = value['status']
    if state == 'accepted' or command == 'cancel' and not wait and value.get('cancel_requested'):
        return 0
    if state == 'failed':
        return failure_exit(value.get('operation') or value)
    if command == 'cancel':
        return 0 if state == 'canceled' else 7
    if state == 'finished':
        return 6 if value['failed'] else 0
    return 7


def main(argv):
    args, info = parse(argv), {'mode': 'attached'}
    try:
        plan = load_options(args.options) if args.command == 'preview' else read_plan(args) if args.command == 'run' else None
        with standalone.connect(args, info, plan['instance_id'] if args.command == 'run' else args.instance_id, attach) as connection:
            value = standalone.collect(args, info, lambda current: execute(connection, current, plan))
        code = result_exit(value, args.command, getattr(args, 'wait', False))
        if 'plan' in value and len(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2).encode('utf-8')) > FILE_LIMIT:
            info.pop('request', None)
            raise ServiceError(413, 'export:plan_too_large')
    except ReplyError as exc:
        value = exc.body
        code = 3 if value.get('code') == 'instance:unavailable' else error_exit(exc.status, value)
    except ServiceError as exc:
        value, code = exc.body(str(uuid4())), error_exit(exc.status, {'outcome': exc.outcome})
    except KeyboardInterrupt:
        value, code = {'code': 'client:interrupted', 'outcome': 'unknown'}, 130
        print(standalone.interruption(args, 'Stopped waiting; accepted exports continue. Inspect the export and request before retrying.'), file=sys.stderr)
    except (OSError, httpx.HTTPError):
        unknown = bool(info.get('request'))
        value, code = {'code': 'request:unavailable', 'outcome': 'unknown' if unknown else 'rejected'}, 7 if unknown else 5
    print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
    return code
