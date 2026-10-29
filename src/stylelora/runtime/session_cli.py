"""Foreground owned CLI session: reviewed commands share one ephemeral Runtime.

JSON lines are argument arrays, never shell text. EOF/exit/Ctrl+C drains and
closes all owned work. Old plans are not rebound to another Runtime.
"""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from uuid import uuid4

from .contracts import strict_json
from .errors import ServiceError
from .standalone import ACTIVE_SESSION, MAX_BYTES, container
from .storage_cli import ReplyError, error_exit


def dispatch(value, client):
    from . import source_cli, job_cli, import_cli, export_cli, prompt_cli, storage_cli
    modules = {'source': source_cli, 'job': job_cli, 'import': import_cli,
               'export': export_cli, 'prompt': prompt_cli, 'storage': storage_cli}
    if (not isinstance(value, dict) or set(value) - {'argv', 'save_plan'} or 'argv' not in value
            or not isinstance(value['argv'], list) or not 2 <= len(value['argv']) <= 1024
            or any(not isinstance(a, str) or len(a) > 16384 for a in value['argv'])):
        raise ServiceError(422, 'session:invalid_command')
    argv = value['argv']
    if argv[0] not in modules:
        raise ServiceError(422, 'session:unsupported_command')
    forbidden = ('--detach', '--standalone', '--project', '--runtime-dir', '--product-dir', '--start-service')
    if any(arg.split('=', 1)[0] in forbidden for arg in argv):
        raise ServiceError(422, 'session:owned_lifecycle')
    if 'save_plan' in value and (argv[1] != 'preview' or not isinstance(value['save_plan'], str)):
        raise ServiceError(422, 'session:invalid_plan_target')
    pid = client.runtime.context.project_id if client.runtime.context else None
    if '$project_id' in argv and pid is None:
        raise ServiceError(409, 'project:not_open')
    argv = [pid if arg == '$project_id' else arg for arg in argv]
    output = io.StringIO()
    with redirect_stdout(output):
        try:
            code = modules[argv[0]].main(argv[1:])
        except SystemExit as error:
            # argparse's errors go to stderr. Help is explicit text in JSON.
            return {'exit_code': int(error.code or 0), 'result': {'help': output.getvalue()}}
    raw = output.getvalue()
    if len(raw.encode('utf-8')) > MAX_BYTES:
        raise ServiceError(413, 'session:result_limit')
    result = strict_json(raw)
    reply = {'exit_code': code, 'result': result}
    if 'save_plan' in value and isinstance(result, dict) and isinstance(result.get('plan'), dict):
        from ..llm.secrets import safe_path
        try:
            path = safe_path(Path(value['save_plan']).absolute())
            # Never overwrite a user's prior reviewed plan or follow a link.
            with path.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(raw)
            reply['plan_saved'] = True
        except FileExistsError:
            reply.update(exit_code=4, plan_saved=False, save_error='session:plan_exists')
        except (OSError, ValueError, ServiceError):
            reply.update(exit_code=5, plan_saved=False, save_error='session:plan_write_failed')
    return reply


def main(argv):
    parser = argparse.ArgumentParser(prog='stylelora session', description=(
        'Foreground owned service container. Read one JSON {argv:[...],save_plan?:PATH} per line; '
        'supports source/job/import/export/prompt/storage. EOF or {exit:true} drains and closes. '
        'No background service, no detach, no shell execution, no cross-session preview replay.'))
    parser.add_argument('--project', type=Path)
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--product-dir', type=Path)
    args = parser.parse_args(argv)
    info, code, count = {}, 0, 0
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, separators=(',', ':')), flush=True)
    try:
        with container(args, info, allow_writes=True) as connection:
            token = ACTIVE_SESSION.set(connection.client)
            try:
                emit({'event': 'ready', 'cli': info})
                while True:
                    raw = sys.stdin.readline(MAX_BYTES+1)
                    if not raw:
                        break
                    if len(raw.encode('utf-8')) > MAX_BYTES:
                        raise ServiceError(413, 'session:command_limit')
                    count += 1
                    try:
                        value = strict_json(raw)
                        if isinstance(value, dict) and set(value) == {'exit'} and value['exit'] is True:
                            break
                        result = dispatch(value, connection.client)
                    except (ServiceError, ReplyError) as error:
                        body = error.body(str(uuid4())) if isinstance(error, ServiceError) else error.body
                        result = {'exit_code': error_exit(error.status, body), 'result': body}
                    emit({'event': 'command', 'sequence': count, **result})
                    code = max(code, result['exit_code'])
                    if result['exit_code'] == 130:
                        break
            finally:
                ACTIVE_SESSION.reset(token)
    except KeyboardInterrupt:
        code = 130
    except (ServiceError, ReplyError) as error:
        body = error.body(str(uuid4())) if isinstance(error, ServiceError) else error.body
        emit({'event': 'error', 'result': body})
        code = error_exit(error.status, body)
    except (OSError, UnicodeError):
        code = 5
        emit({'event': 'error', 'result': {'code': 'session:io_failed'}})
    emit({'event': 'closed', 'exit_code': code, 'commands': count, 'cli': info})
    return code
