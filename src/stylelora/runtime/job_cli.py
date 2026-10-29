"""Preview-bound, attached annotation batch and job commands."""
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ..api.contracts import JobBatch, JobPreview
from ..project_db.validation import digest
from .attached_cli import attach
from .contracts import strict_json, uuid_string
from .errors import ServiceError
from .project_cli import version
from .storage_cli import checksum, error_exit, identifier, page_limit, ReplyError
from . import standalone

FILE_LIMIT = 1024 * 1024
ACTIVE = ('pending', 'running')
STATES = (*ACTIVE, 'paused', 'done', 'failed', 'canceled', 'blocked')


def positive(value):
    try:
        number = int(value)
        if 0 < number <= 9223372036854775807:
            return number
    except (ValueError, TypeError):
        pass
    raise argparse.ArgumentTypeError('A positive integer ID is required')


def parse(argv):
    parser = argparse.ArgumentParser(prog='stylelora job', description=(
        'list/status/outcomes support --standalone --project PATH; other actions attach to an existing service. '
        'Preview first, save the JSON, then run with its confirmation digest. '
        'No automatic server start, model install, retry or project switch.'))
    sub = parser.add_subparsers(dest='command', required=True)
    preview = sub.add_parser('preview', help='Estimate coverage without model calls or job creation')
    preview.add_argument('--executor', required=True, choices=('wd', 'llm_vision', 'wd,llm_vision', 'llm_vision,wd'))
    preview.add_argument('--mode', choices=('selected', 'uncovered', 'all'), default='uncovered')
    scope = preview.add_mutually_exclusive_group()
    scope.add_argument('--ids', nargs='+', type=positive)
    scope.add_argument('--filter', type=Path, help='Strict JSON JobFilter; omitted means the current non-rejected images')
    preview.add_argument('--config', type=Path, help='Strict executor configuration JSON; required when using WD')
    run = sub.add_parser('run', help='Submit the exact saved preview; repeated use reconciles the same batch')
    run.add_argument('--preview', required=True, type=Path, help='UTF-8 JSON output from job preview')
    run.add_argument('--confirm-digest', required=True, type=checksum, help='Confirm this plan, including scope, configuration and estimates')
    listing = sub.add_parser('list', help='Read one page of persistent batches')
    listing.add_argument('--limit', type=page_limit, default=20)
    listing.add_argument('--cursor')
    commands = {}
    for name in ('status', 'pause', 'resume', 'cancel', 'outcomes'):
        command = sub.add_parser(name)
        command.add_argument('job_id', nargs='?', type=positive)
        if name != 'outcomes':
            command.add_argument('--batch-id', type=identifier, help='Target the entire pipeline instead of a single stage')
        if name in ('pause', 'resume', 'cancel'):
            command.add_argument('--if-match', type=version, help='Use the original unquoted version for explicit replay')
            command.add_argument('--idempotency-key', type=identifier)
        commands[name] = command
    commands['status'].add_argument('--wait', action='store_true')
    commands['outcomes'].add_argument('--kind', choices=('done', 'failed', 'skipped'))
    commands['outcomes'].add_argument('--limit', type=page_limit, default=50)
    commands['outcomes'].add_argument('--cursor')
    for command in (run, commands['resume']):
        waiting = command.add_mutually_exclusive_group()
        waiting.add_argument('--wait', dest='wait', action='store_true', default=True)
        waiting.add_argument('--detach', dest='wait', action='store_false')
    for command in (preview, listing, *commands.values()):
        if command in (listing, commands['status'], commands['outcomes']):
            standalone.project_options(command, identifier, required=command is not listing)
        else:
            command.add_argument('--project-id', type=identifier, required=command not in (preview, listing))
        command.add_argument('--instance-id', type=identifier)
    for command in (preview, run, listing, *commands.values()):
        command.add_argument('--json', action='store_true', help='Single JSON stdout (default), progress on stderr')
    args = parser.parse_args(argv)
    standalone.validate(parser, args)
    if args.command == 'preview':
        if (args.mode == 'selected') != (args.ids is not None):
            parser.error('selected requires --ids; other modes use --filter or the default filter')
        if args.ids and (len(args.ids) > 5000 or len(set(args.ids)) != len(args.ids)):
            parser.error('--ids requires at most 5000 distinct IDs')
        if 'wd' in args.executor and not args.config:
            parser.error('WD requires --config with an installed manifest and thresholds')
    if args.command in commands:
        if args.command == 'outcomes':
            if args.job_id is None:
                parser.error('outcomes requires JOB_ID')
        elif (args.job_id is None) == (args.batch_id is None):
            parser.error('Supply exactly one of JOB_ID or --batch-id')
        if args.command in ('pause', 'resume', 'cancel'):
            if args.idempotency_key and (not args.if_match or not args.instance_id):
                parser.error('Replay requires original --if-match and --instance-id with the request key')
    if getattr(args, 'cursor', None) is not None and not 1 <= len(args.cursor) <= 4096:
        parser.error('cursor must contain 1 to 4096 characters')
    return args


def read_json(path):
    try:
        with path.open('rb') as stream:
            data = stream.read(FILE_LIMIT+1)
        if len(data) > FILE_LIMIT:
            raise ValueError()
        return strict_json(data.decode('utf-8-sig'))
    except (OSError, UnicodeError, ValueError, ServiceError, RecursionError):
        raise ServiceError(422, 'job:invalid_file') from None


def validate_body(value, *, batch=False):
    try:
        model = (JobBatch if batch else JobPreview).model_validate(value)
        selected, query = model.scope.selected_ids, model.scope.filter
        if (selected is None) == (query is None) or (model.mode == 'selected') != (selected is not None):
            raise ValueError()
        if selected is not None and (not selected or len(set(selected)) != len(selected)):
            raise ValueError()
        if batch and model.preview_scope_digest is None:
            raise ValueError()
        return model.model_dump()
    except (ValidationError, ValueError, TypeError):
        raise ServiceError(422, 'job:invalid_spec') from None


def preview_body(args):
    return validate_body({'executors': args.executor.split(','), 'mode': args.mode,
        'scope': {'selected_ids': args.ids} if args.ids is not None else {'filter': read_json(args.filter) if args.filter else {}},
        'config': read_json(args.config) if args.config else {}})


def check_preview(value, executors):
    try:
        checksum(value['scope_digest'])
        if value['estimate_only'] is not True or value['revalidates_on_start'] is not True:
            raise ValueError()
        if type(value['image_count']) is not int or not 0 <= value['image_count'] <= 5000:
            raise ValueError()
        if not isinstance(value['overlapping_jobs'], list) or [row['executor'] for row in value['items']] != executors:
            raise ValueError()
        for row in value['items']:
            for field in ('total', 'cache_hit', 'needs_execution', 'file_missing', 'waiting_for_wd', 'role_empty', 'rating_unresolved'):
                if type(row[field]) is not int or not 0 <= row[field] <= value['image_count']:
                    raise ValueError()
            if row['total'] != value['image_count']:
                raise ValueError()
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None


def read_plan(args):
    try:
        document = read_json(args.preview)
        plan = document['plan']
        if not isinstance(plan, dict) or set(plan) != {'schema_version', 'instance_id', 'project_id', 'idempotency_key', 'body', 'preview'}:
            raise ValueError()
        if type(plan['schema_version']) is not int or plan['schema_version'] != 1:
            raise ValueError()
        for field in ('instance_id', 'project_id', 'idempotency_key'):
            uuid_string(plan[field])
        body = validate_body(plan['body'], batch=True)
        if body != plan['body'] or body['preview_scope_digest'] != plan['preview']['scope_digest']:
            raise ValueError()
        check_preview(plan['preview'], body['executors'])
        if document['confirmation_digest'] != digest(plan) or args.confirm_digest != digest(plan):
            raise ValueError()
        return plan
    except (KeyError, TypeError, ValueError, ServiceError, RecursionError):
        raise ServiceError(422, 'job:invalid_plan') from None


def target(args, base, info):
    if args.batch_id:
        info['batch_id'] = args.batch_id
        path = base+'/batches/'+args.batch_id
    else:
        info['job_id'] = args.job_id
        path = base+'/'+str(args.job_id)
    info['status_url'] = path
    return path


def check_state(value, info):
    try:
        if 'job_id' in info:
            if value['job_id'] != info['job_id'] or type(value['job_id']) is not int:
                raise ValueError()
            task = value['task']
            if task != {'kind': 'job', 'id': info['job_id'], 'project_id': info['project_id']}:
                raise ValueError()
        else:
            if value['batch_id'] != info['batch_id'] or not isinstance(value.get('items'), list) or not 1 <= len(value['items']) <= 2:
                raise ValueError()
            for row in value['items']:
                if not isinstance(row, dict) or row.get('batch_id') != info['batch_id']:
                    raise ValueError()
                check_state(row, {'project_id': info['project_id'], 'job_id': positive(row['job_id'])})
        version(value['version'])
        if value['status'] not in STATES or type(value['status']) is not str:
            raise ValueError()
        for field in ('total', 'done_count', 'failed_count', 'skipped_count'):
            if type(value[field]) is not int or value[field] < 0:
                raise ValueError()
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def waiting(value):
    if value['status'] == 'pending' and (value.get('reason') or value.get('error') or value.get('runtime_error')
            or any(row.get('runtime_error') for row in (value.get('items') or []))):
        return False
    return value['status'] in ACTIVE


def check_page(value):
    if (not isinstance(value.get('items'), list) or type(value.get('has_more')) is not bool
            or 'next_cursor' not in value or not (value['next_cursor'] is None or isinstance(value['next_cursor'], str))):
        raise ServiceError(503, 'request:invalid_response')
    return value


def poll(connection, path, wait):
    while True:
        value = check_state(connection.request('GET', path), connection.info)
        if not wait or not waiting(value):
            return value
        time.sleep(.1)


def execute(connection, args, prepared):
    info = connection.info
    base = connection.project(prepared['project_id'] if args.command == 'run' else args.project_id)
    if args.command == 'preview':
        value = connection.request('POST', base+'/preview', body=prepared)
        check_preview(value, prepared['executors'])
        plan = {'schema_version': 1, 'instance_id': info['instance_id'], 'project_id': info['project_id'],
                'idempotency_key': str(uuid4()), 'body': {**prepared, 'batch_id': str(uuid4()), 'preview_scope_digest': value['scope_digest']},
                'preview': value}
        return {'plan': plan, 'confirmation_digest': digest(plan)}
    if args.command == 'list':
        return check_page(connection.request('GET', base+'/batches', params={k: v for k, v in {'limit': args.limit, 'cursor': args.cursor}.items() if v is not None}))
    if args.command == 'run':
        batch_id = prepared['body']['batch_id']
        path = base+'/batches/'+batch_id
        info.update(batch_id=batch_id, status_url=path)
        value = connection.request('POST', base+'/batches', body=prepared['body'], key=prepared['idempotency_key'], status=202)
        try:
            if value['batch_id'] != batch_id or value['batch_url'] != path:
                raise ValueError()
            if len(value['items']) != len(prepared['body']['executors']):
                raise ValueError()
            for row, executor in zip(value['items'], prepared['body']['executors']):
                check_state(row, {'project_id': info['project_id'], 'job_id': positive(row['job_id'])})
                if row['executor'] != executor or row['batch_id'] != batch_id:
                    raise ValueError()
            first = value['items'][0]
            if value['task'] != first['task'] or value['status_url'] != base+'/'+str(first['job_id']):
                raise ValueError()
        except (KeyError, TypeError, ValueError, ServiceError, argparse.ArgumentTypeError):
            raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
        print(standalone.progress('Accepted batch '+batch_id+'; stopping this client does not cancel it.'), file=sys.stderr)
        return poll(connection, path, True) if args.wait else {**value, 'status': 'accepted'}
    if args.command == 'outcomes':
        info['job_id'] = args.job_id
        return check_page(connection.request('GET', base+'/'+str(args.job_id)+'/outcomes',
            params={k: v for k, v in {'limit': args.limit, 'cursor': args.cursor, 'kind': args.kind}.items() if v is not None}))
    path = target(args, base, info)
    if args.command == 'status':
        return poll(connection, path, args.wait)
    expected = args.if_match or poll(connection, path, False)['version']
    value = connection.request('POST', path+'/control', body={'action': args.command}, key=args.idempotency_key, expected=expected)
    try:
        check_state(value, info)
    except ServiceError:
        raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
    if args.command == 'resume' and args.wait:
        return poll(connection, path, True)
    return value


def result_exit(value, command, wait=False):
    state = value.get('status')
    if command in ('pause', 'cancel') and state in (('paused', 'done') if command == 'pause' else ('canceled', 'done')):
        return 0
    if command == 'resume' and not wait and state in ACTIVE:
        return 0
    if state == 'failed':
        return 5
    if state in (*ACTIVE, 'paused', 'canceled', 'blocked'):
        return 7
    return 6 if value.get('failed_count', 0) else 0


def main(argv):
    args = parse(argv)
    info = {'mode': 'attached'}
    try:
        prepared = preview_body(args) if args.command == 'preview' else read_plan(args) if args.command == 'run' else None
        instance = prepared['instance_id'] if args.command == 'run' else args.instance_id
        with standalone.connect(args, info, instance, attach) as connection:
            value = standalone.collect(args, info, lambda current: execute(connection, current, prepared))
        code = result_exit(value, args.command, getattr(args, 'wait', False))
    except ReplyError as exc:
        value = exc.body
        code = 3 if value.get('code') == 'instance:unavailable' else error_exit(exc.status, value)
    except ServiceError as exc:
        value, code = exc.body(str(uuid4())), error_exit(exc.status, {'outcome': exc.outcome})
    except KeyboardInterrupt:
        value, code = {'code': 'client:interrupted', 'outcome': 'unknown'}, 130
        print(standalone.interruption(args, 'Stopped waiting; accepted service tasks continue. Inspect the recorded batch or request before retrying.'), file=sys.stderr)
    except (OSError, httpx.HTTPError):
        unknown = bool(info.get('request'))
        value, code = {'code': 'request:unavailable', 'outcome': 'unknown' if unknown else 'rejected'}, 7 if unknown else 5
    print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
    return code
