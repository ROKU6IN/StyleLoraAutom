"""Explicit, bounded startup of a persistent local service; never replay writes."""
import argparse
import os
import re
import subprocess
import sys
import time

import httpx

from .contracts import strict_json, uuid_string
from .errors import ServiceError
from .instance import pipe_request


def seconds(value):
    try:
        result = int(value)
        if not 5 <= result <= 120:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('Startup wait must be an integer from 5 to 120 seconds') from None


def add_options(parser):
    parser.add_argument('--start-service', action='store_true',
                        help='Attach, or explicitly start a persistent background serve --no-browser if absent')
    parser.add_argument('--startup-timeout', type=seconds,
                        help='Startup deadline in seconds (5-120, default 30); requires --start-service')


def validate_options(parser, args):
    if args.startup_timeout is not None and not args.start_service:
        parser.error('--startup-timeout requires --start-service')
    if args.start_service and any(getattr(args, key, None) for key in
                                  ('standalone', 'instance_id', 'grant_id', 'idempotency_key')):
        parser.error('--start-service cannot be combined with standalone mode or replay identifiers')


def launch():
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
               'close_fds': True, 'shell': False}
    if os.name == 'nt':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options['startupinfo'] = startup
    else:
        options['start_new_session'] = True
    # Preserve the current interpreter/virtualenv. No credential or project path.
    return subprocess.Popen([sys.executable, '-m', 'stylelora', 'serve', '--no-browser'], **options)


def credentials(value):
    try:
        if (type(value['protocol_version']) is not int or value['protocol_version'] != 1
                or not isinstance(value['authority'], str)
                or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', value['authority'])
                or not 1 <= int(value['authority'].split(':')[1]) <= 65535
                or type(value['pid']) is not int or value['pid'] < 1
                or not isinstance(value['token'], str) or not re.fullmatch(r'[\x21-\x7e]+', value['token'])):
            raise ValueError()
        uuid_string(value['instance_id'])
    except (KeyError, TypeError, ValueError, ServiceError):
        raise ServiceError(503, 'instance:invalid_response') from None
    return value


def http_ready(value, timeout):
    """A pipe can exist before ASGI lifespan completes; verify the same session."""
    try:
        with httpx.Client(base_url='http://' + value['authority'],
                          headers={'Authorization': 'Bearer ' + value['token']},
                          trust_env=False, follow_redirects=False, timeout=min(1.0, timeout)) as client:
            response = client.get('/api/session')
        if response.status_code in (401, 403):
            raise ServiceError(403, 'instance:identity_mismatch')
        if response.status_code != 200 or not response.headers.get('content-type', '').startswith('application/json'):
            raise ValueError()
        try:
            session = strict_json(response.content)
        except ServiceError:
            raise ValueError() from None
        if not isinstance(session, dict) or session.get('instance_id') != value['instance_id'] or session.get('version') != value['instance_id']:
            raise ServiceError(409, 'instance:changed')
        if session.get('draining') is True:
            raise ServiceError(503, 'process:draining')
        if session.get('draining') is not False or type(session.get('api_schema_version')) is not int or session['api_schema_version'] != 1:
            raise ValueError()
        return True
    except (httpx.ConnectError, httpx.TimeoutException):
        return False
    except (ValueError, httpx.HTTPError):
        raise ServiceError(503, 'instance:invalid_response') from None


def ensure_service(info, *, timeout=30):
    """Retry read-only handshakes; callers own any later business RPC.

    Timeout/interrupt never kills a service another client may already use.
    Each pipe RPC retains its bounded connect/read timeouts (3s + 6s).
    """
    deadline, child, first = time.monotonic() + timeout, None, True
    state = info['service_start'] = {'status': 'checking'}
    try:
        while time.monotonic() < deadline:
            try:
                current = credentials(pipe_request({'action': 'hello'}))
            except ServiceError as error:
                if error.code != 'instance:not_running':
                    raise
                if time.monotonic() >= deadline:
                    break
                if first:
                    try:
                        child = launch()
                    except OSError:
                        state['status'] = 'failed'
                        raise ServiceError(503, 'process:start_failed') from None
                    state.update(status='starting', spawned_pid=child.pid)
                    print('Started background service process ' + str(child.pid) +
                          '; it may continue if this client stops waiting. No project request has been sent.', file=sys.stderr)
            else:
                remaining = deadline - time.monotonic()
                if remaining > 0 and http_ready(current, remaining):
                    state.update(status='ready', server_pid=current['pid'], instance_id=current['instance_id'])
                    return current
            first = False
            if child is not None and child.poll() not in (None, 0):
                state['status'] = 'failed'
                raise ServiceError(503, 'process:start_failed')
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        raise ServiceError(503, 'process:start_timeout',
                           'Service readiness was not confirmed; inspect project status before retrying',
                           outcome='unknown' if child is not None else 'rejected')
    except BaseException:
        if state['status'] != 'failed':
            state['status'] = 'unconfirmed'
        raise
