"""Short-lived service container for explicitly selected local CLI operations.

Routes, validation and service admission are shared with HTTP, but no socket,
discovery pipe or bootstrap session is published. Session previews/cursors must
not escape as reusable authorizations for a later container.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
import json
from pathlib import Path

import httpx

from .errors import ServiceError, safe_error

MAX_ITEMS = 10000
MAX_BYTES = 16 * 1024 * 1024
ACTIVE_SESSION = ContextVar('stylelora_owned_cli_session', default=None)


def project_options(parser, identifier, *, required=False):
    target = parser.add_mutually_exclusive_group(required=required)
    target.add_argument('--project-id', type=identifier, help='Require this project handle in the running service')
    target.add_argument('--project', type=Path, help='Explicit project path; requires --standalone')
    parser.add_argument('--standalone', action='store_true',
                        help='Open an owned local service container, complete this command, then close; no active service allowed')
    parser.add_argument('--runtime-dir', type=Path, help='Local runtime/model location for standalone mode')
    parser.add_argument('--product-dir', type=Path, help='Local product configuration for standalone mode')


def validate(parser, args):
    enabled = getattr(args, 'standalone', False)
    paths = any(getattr(args, key, None) is not None for key in ('project', 'runtime_dir', 'product_dir'))
    if paths and not enabled:
        parser.error('--project/--runtime-dir/--product-dir require --standalone')
    if not enabled:
        return
    if not args.project or getattr(args, 'project_id', None):
        parser.error('--standalone requires --project PATH, not a session project handle')
    if any(getattr(args, key, None) is not None for key in ('instance_id', 'idempotency_key', 'cursor')):
        parser.error('Standalone mode cannot reuse session IDs, request keys or cursors; lists read all pages automatically')
    if args.command == 'scan' and getattr(args, 'wait', True) is False:
        parser.error('Standalone mode cannot detach')
    if getattr(args, 'known', False) or getattr(args, 'known_id', None):
        parser.error('Standalone scanning requires one explicit --project; recent-project scans use the running service')


class InProcessClient:
    def __init__(self, runtime, *, allow_writes=False):
        from ..api.app import create_app
        self.runtime = runtime
        self.allow_writes = allow_writes
        self.app = create_app(runtime, manage_lifecycle=False)

    def request(self, method, path, **kwargs):
        # Single commands expose reads/scan; sessions admit their business
        # writes but retain exclusive ownership of project/process lifecycle.
        context = self.runtime.context
        scan = f'/api/projects/{context.project_id}/storage/scans' if context else None
        project_route = bool(context and path.startswith(f'/api/projects/{context.project_id}/'))
        product_route = path.startswith(('/api/prompt-production/', '/api/storage/'))
        if context and path == f'/api/projects/{context.project_id}/close':
            raise ServiceError(422, 'standalone:action_unsupported')
        if method != 'GET' and not (method == 'POST' and path == scan) and not (
                self.allow_writes and (project_route or product_route)):
            raise ServiceError(422, 'standalone:action_unsupported')
        if not path.startswith('/api/') or path.startswith('//') or '?' in path or '#' in path:
            raise ServiceError(422, 'standalone:invalid_route')
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                    base_url=self.runtime.origin, headers={'Authorization': 'Bearer '+self.runtime.token},
                    trust_env=False, follow_redirects=False, timeout=30) as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(request())


@contextmanager
def container(args, info, connection_type=None, *, allow_writes=False):
    from .attached_cli import Connection
    from .instance import ProductLock
    from .lifecycle import Runtime
    from .paths import locations
    owner, runtime = None, None
    info.update(mode='standalone', service_started=False, project_opened=False,
                service_closed=False, session_reusable=False)
    try:
        # Acquire before loading configuration or opening/recovering a project.
        # Never attach to, stop or switch another process's project.
        owner = ProductLock()
        paths = locations(args.runtime_dir, args.product_dir)
        runtime = Runtime(product_dir=paths.product, model_dir=paths.models,
                          llm_dir=paths.config, secret_dir=paths.private)
        info['service_started'] = True
        if args.project is not None:
            granted = runtime.grant(args.project, 'project_open')
            opened = runtime.open_now(runtime.grants.get(granted['grant_id'], 'project_open'))
            info.update(project_opened=True, recovery_counts={key: len(value) for key, value in opened['recovery'].items()})
        info.update(instance_id=runtime.instance_id, project_id=runtime.context.project_id if runtime.context else None)
        yield (connection_type or Connection)(InProcessClient(runtime, allow_writes=allow_writes), info)
    except Exception as error:
        # Preserve API errors handled by the existing command, redact local
        # initialization failures through the same domain error mapping.
        from .storage_cli import ReplyError
        if isinstance(error, ReplyError):
            raise
        raise safe_error(error) from None
    finally:
        closed = runtime is None
        try:
            if runtime is not None:
                runtime.shutdown()
                closed = True
            info['service_closed'] = True
        except BaseException:
            info['service_closed'] = False
            raise ServiceError(503, 'standalone:shutdown_incomplete', outcome='unknown') from None
        finally:
            # Keep exclusion if draining failed; this process must not admit a
            # second container while the first might still own workers/files.
            if owner is not None and closed:
                owner.close()


@contextmanager
def connect(args, info, expected_instance, attached):
    if getattr(args, 'standalone', False):
        with container(args, info) as connection:
            yield connection
    else:
        with attached(info, expected_instance) as connection:
            yield connection


def collect(args, info, execute):
    if not getattr(args, 'standalone', False) or not hasattr(args, 'cursor'):
        return execute(args)
    current, items, seen, pages, size = copy(args), [], set(), 0, 0
    while True:
        value = execute(current)
        if not isinstance(value, dict) or not isinstance(value.get('items'), list):
            raise ServiceError(503, 'request:invalid_response')
        pages += 1
        size += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        items.extend(value['items'])
        if len(items) > MAX_ITEMS or size > MAX_BYTES:
            raise ServiceError(413, 'standalone:result_limit')
        cursor = value.get('next_cursor')
        if cursor is None:
            if value.get('has_more'):
                raise ServiceError(503, 'request:invalid_response')
            info['pagination'] = {'pages': pages, 'returned': len(items), 'complete': True}
            result = {**value, 'items': items, 'next_cursor': None, 'has_more': False}
            result.pop('limit', None)  # --limit is the per-request size, not the aggregate size.
            return result
        if not isinstance(cursor, str) or not cursor or cursor in seen or not value['items']:
            raise ServiceError(503, 'request:invalid_response')
        seen.add(cursor)
        current.cursor = cursor


def interruption(args, attached_message):
    if ACTIVE_SESSION.get() is not None:
        return 'Interrupted; the owned CLI session will now drain its work and close. Inspect persistent state before retrying.'
    if getattr(args, 'standalone', False):
        return 'Standalone command interrupted; owned work was drained and the project closed. Inspect persistent state before retrying.'
    return attached_message


def progress(message):
    if ACTIVE_SESSION.get() is not None:
        return message.split(';', 1)[0] + '; owned work is drained and paused when this CLI session closes.'
    return message


def session_info(info, expected_instance=None):
    client = ACTIVE_SESSION.get()
    if client is None:
        return None
    runtime = client.runtime
    if expected_instance and expected_instance != runtime.instance_id:
        raise ServiceError(409, 'instance:changed')
    info.update(mode='standalone-session', instance_id=runtime.instance_id, session_reusable=False)
    return client


def session_control(value):
    client = ACTIVE_SESSION.get()
    if client is None:
        return None
    runtime = client.runtime
    if value.get('instance_id') != runtime.instance_id:
        raise ServiceError(409, 'instance:changed')
    if value.get('action') != 'grant' or value.get('purpose') != 'import_root':
        raise ServiceError(422, 'standalone:action_unsupported')
    if runtime.context is None or value.get('project_id') != runtime.context.project_id:
        raise ServiceError(409, 'project:handle_expired')
    return runtime.grant(Path(value['path']), 'import_root', runtime.context.project_id)
