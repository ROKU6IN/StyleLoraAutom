"""Product-level prompt tasks, reviewed plans and content-only seed projection."""
import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ..project_db.validation import digest
from ..prompt_production.types import SamplingRequest, TaskCreate
from .attached_cli import attach
from . import standalone
from .contracts import strict_json, uuid_string
from .errors import ServiceError
from .job_cli import check_page, positive
from .storage_cli import checksum, error_exit, identifier, page_limit, ReplyError

BASE = '/api/prompt-production'
FILE_LIMIT = 16 * 1024 * 1024
ACTIVE = ('pending', 'running')
STATES = (*ACTIVE, 'paused', 'done', 'failed', 'canceled')


def parse(argv):
    parser = argparse.ArgumentParser(prog='stylelora prompt', description=(
        'Use the running product-level prompt service; no project is needed. Preview a sampling spec, '
        'then run the saved plan with its confirmation digest. Seeds require a reviewed task revision.'))
    sub = parser.add_subparsers(dest='command', required=True)
    preview = sub.add_parser('preview', help='Preview a SamplingRequest JSON without LLM calls or task creation')
    preview.add_argument('--spec', type=Path, required=True)
    run = sub.add_parser('run', help='Run a saved prompt preview plan; reuse keeps the same task identity')
    run.add_argument('--spec', type=Path, required=True, help='UTF-8 JSON output from prompt preview')
    run.add_argument('--confirm-digest', type=checksum, required=True)
    listing = sub.add_parser('list', help='Read one page of product tasks')
    commands = {}
    for name in ('status', 'pause', 'resume', 'cancel', 'outcomes', 'seeds'):
        command = sub.add_parser(name)
        command.add_argument('task_id', type=identifier)
        if name in ('pause', 'resume', 'cancel', 'seeds'):
            command.add_argument('--expected-revision', type=positive, required=name == 'seeds')
        if name in ('pause', 'resume', 'cancel'):
            command.add_argument('--idempotency-key', type=identifier)
        commands[name] = command
    for command in (listing, commands['outcomes']):
        command.add_argument('--limit', type=page_limit, default=50)
        command.add_argument('--cursor')
    for command in (run, commands['resume']):
        waiting = command.add_mutually_exclusive_group()
        waiting.add_argument('--wait', dest='wait', action='store_true', default=True)
        waiting.add_argument('--detach', dest='wait', action='store_false')
    commands['status'].add_argument('--wait', action='store_true')
    for command in (preview, listing, *commands.values()):
        command.add_argument('--instance-id', type=identifier)
    for command in (preview, run, listing, *commands.values()):
        command.add_argument('--json', action='store_true', help='One JSON result on stdout (default); progress on stderr')
    args = parser.parse_args(argv)
    if args.command in ('pause', 'resume', 'cancel') and args.idempotency_key and (not args.expected_revision or not args.instance_id):
        parser.error('Control replay requires original --expected-revision and --instance-id with the request key')
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
    except (OSError, ValueError, UnicodeError, ServiceError, RecursionError):
        raise ServiceError(422, 'prompt:invalid_file') from None


def validate_request(value):
    try:
        request = SamplingRequest.model_validate(value).model_dump()
    except (ValidationError, ValueError, TypeError):
        raise ServiceError(422, 'prompt:invalid_spec') from None
    # The open-ended future Launcher field must never carry secrets in receipts.
    if request['live_features'] or request['launcher_selection'] is not None:
        raise ServiceError(503, 'launcher:unsupported_schema')
    return request


def check_preview(value):
    try:
        checksum(value['preview_digest'])
        request = validate_request(value['request'])
        if request != value['request'] or request['plan']['random_seed'] is None or type(value['ready']) is not bool:
            raise ValueError()
        for field in ('count', 'requested', 'available'):
            if type(value[field]) is not int or value[field] < 0:
                raise ValueError()
        if value['requested'] != request['plan']['target_count'] or value['count'] > value['requested']:
            raise ValueError()
    except (KeyError, TypeError, ValueError, ServiceError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None


def read_plan(args):
    try:
        document = read_json(args.spec)
        plan = document['plan']
        if not isinstance(plan, dict) or set(plan) != {'schema_version', 'instance_id', 'idempotency_key', 'body', 'preview'}:
            raise ValueError()
        if type(plan['schema_version']) is not int or plan['schema_version'] != 1:
            raise ValueError()
        uuid_string(plan['instance_id'])
        uuid_string(plan['idempotency_key'])
        body = TaskCreate.model_validate(plan['body']).model_dump()
        check_preview(plan['preview'])
        if (body != plan['body'] or body['request'] != plan['preview']['request']
                or body['preview_digest'] != plan['preview']['preview_digest']):
            raise ValueError()
        if document['confirmation_digest'] != digest(plan) or args.confirm_digest != digest(plan):
            raise ValueError()
        return plan
    except (KeyError, TypeError, ValueError, ValidationError, ServiceError, RecursionError):
        raise ServiceError(422, 'prompt:invalid_plan') from None


def check_task(value, task_id):
    try:
        if value['task_id'] != task_id or type(value['revision']) is not int or value['revision'] < 1:
            raise ValueError()
        if value['status'] not in STATES or type(value['ready']) is not bool:
            raise ValueError()
        if value['ready'] and value['status'] != 'done':
            raise ValueError()
        checksum(value['version'])
        if value['reason'] is not None and not isinstance(value['reason'], str):
            raise ValueError()
        progress = value.get('progress')
        if progress is not None:
            if not isinstance(progress, dict) or not isinstance(progress.get('counts'), dict):
                raise ValueError()
            if any(type(n) is not int or n < 0 for n in progress['counts'].values()):
                raise ValueError()
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def poll(connection, path, wait):
    while True:
        value = check_task(connection.request('GET', path), connection.info['task_id'])
        if not wait or value['status'] not in ACTIVE or (value['status'] == 'pending' and value['reason']):
            return value
        time.sleep(.1)


def check_seeds(value):
    try:
        if set(value) != {'digest', 'items', 'count'} or type(value['count']) is not int or not 1 <= value['count'] <= 10000:
            raise ValueError()
        items = value['items']
        if not isinstance(items, list) or len(items) != value['count'] or digest(items) != value['digest']:
            raise ValueError()
        for index, row in enumerate(items, 1):
            if not isinstance(row, dict) or set(row) != {'seq', 'prompt', 'aspect'}:
                raise ValueError()
            if type(row['seq']) is not int or row['seq'] != index or not isinstance(row['prompt'], str) or not row['prompt'].strip():
                raise ValueError()
            if row['aspect'] not in (None, 'landscape', 'portrait', 'square'):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ServiceError(503, 'request:invalid_response') from None
    return value


def execute(connection, args, prepared):
    info = connection.info
    if args.command == 'preview':
        value = connection.request('POST', BASE+'/coverage/preview', body=prepared)
        check_preview(value)
        plan = {'schema_version': 1, 'instance_id': info['instance_id'], 'idempotency_key': str(uuid4()),
                'body': {'task_id': str(uuid4()), 'request': value['request'], 'preview_digest': value['preview_digest']}, 'preview': value}
        return {'plan': plan, 'confirmation_digest': digest(plan)}
    if args.command == 'list':
        return check_page(connection.request('GET', BASE+'/tasks', params={k: v for k, v in
            {'limit': args.limit, 'cursor': args.cursor}.items() if v is not None}))
    task_id = prepared['body']['task_id'] if args.command == 'run' else args.task_id
    path = BASE+'/tasks/'+task_id
    info.update(task_id=task_id, status_url=path)
    if args.command == 'run':
        value = connection.request('POST', BASE+'/tasks', body=prepared['body'], key=prepared['idempotency_key'], status=202)
        try:
            check_task(value, task_id)
            if value['status_url'] != path:
                raise ServiceError(503, 'request:invalid_response')
        except (KeyError, ServiceError):
            raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
        print(standalone.progress('Accepted prompt task '+task_id+'; stopping this client does not cancel it.'), file=sys.stderr)
        return poll(connection, path, True) if args.wait else {**value, 'status': 'accepted', 'task_status': value['status']}
    if args.command == 'status':
        return poll(connection, path, args.wait)
    if args.command == 'outcomes':
        return check_page(connection.request('GET', path+'/outcomes', params={k: v for k, v in
            {'limit': args.limit, 'cursor': args.cursor}.items() if v is not None}))
    revision = args.expected_revision
    if revision is None:
        revision = poll(connection, path, False)['revision']
    info['expected_revision'] = revision
    if args.command == 'seeds':
        return check_seeds(connection.request('POST', path+'/seeds', body={'expected_revision': revision}))
    value = connection.request('POST', path+'/control', body={'action': args.command, 'expected_revision': revision}, key=args.idempotency_key)
    try:
        check_task(value, task_id)
    except ServiceError:
        raise ServiceError(503, 'request:outcome_unknown', outcome='unknown') from None
    return poll(connection, path, True) if args.command == 'resume' and args.wait else value


def result_exit(value, command, wait=False):
    if command == 'preview':
        return 0 if value['plan']['preview']['ready'] else 6
    if command in ('list', 'outcomes', 'seeds'):
        return 0
    state = value['status']
    if command in ('pause', 'cancel') and state == ('paused' if command == 'pause' else 'canceled'):
        return 0
    if state == 'accepted' or (command == 'resume' and not wait and state in ACTIVE):
        return 0
    if state == 'failed':
        return 5
    if state != 'done':
        return 7
    counts = (value.get('progress') or {}).get('counts', {})
    return 6 if not value['ready'] or value.get('append_error') or counts.get('failed', 0) or counts.get('review_required', 0) else 0


def main(argv):
    args = parse(argv)
    info = {'mode': 'attached', 'scope': 'product'}
    try:
        prepared = validate_request(read_json(args.spec)) if args.command == 'preview' else read_plan(args) if args.command == 'run' else None
        instance = prepared['instance_id'] if args.command == 'run' else args.instance_id
        with attach(info, instance) as connection:
            value = execute(connection, args, prepared)
        code = result_exit(value, args.command, getattr(args, 'wait', False))
        if args.command == 'seeds':
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return code
        # A successful preview must be saveable and readable by run using the same bound.
        if args.command == 'preview' and len(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2).encode('utf-8')) > FILE_LIMIT:
            info.pop('request', None)
            raise ServiceError(413, 'prompt:plan_too_large')
    except ReplyError as exc:
        value = exc.body
        code = 3 if value.get('code') == 'instance:unavailable' else error_exit(exc.status, value)
    except ServiceError as exc:
        value, code = exc.body(str(uuid4())), error_exit(exc.status, {'outcome': exc.outcome})
    except KeyboardInterrupt:
        value, code = {'code': 'client:interrupted', 'outcome': 'unknown'}, 130
        from .standalone import interruption
        print(interruption(args, 'Stopped waiting; accepted product tasks continue. Inspect the recorded task or request before retrying.'), file=sys.stderr)
    except (OSError, httpx.HTTPError):
        unknown = bool(info.get('request'))
        value, code = {'code': 'request:unavailable', 'outcome': 'unknown' if unknown else 'rejected'}, 7 if unknown else 5
    print(json.dumps({**value, 'cli': info}, ensure_ascii=False, indent=2))
    return code
