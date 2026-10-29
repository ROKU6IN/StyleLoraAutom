import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.project_db.project import create_project, open_project
from stylelora.project_db.locking import ProjectLock
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import PipeServer, ProductLock
from .api_support import APITest
from .support import TEMP_ROOT

CLIENT = httpx.Client
PID, OP, INSTANCE, GRANT = (str(uuid4()) for _ in range(4))
VERSION = 'a'*64
CREDENTIALS = {'authority': '127.0.0.1:8301', 'token': 'private-test-token',
               'instance_id': INSTANCE, 'protocol_version': 1}


def invoke(*args):
    output, progress = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
        code = main(['project', *map(str, args)])
    return code, json.loads(output.getvalue()), progress.getvalue()


class ProjectCLITransportTests(unittest.TestCase):
    def run_cli(self, args, handler, *, sleep=None, pipe_error=None):
        self.requests, self.pipe_calls, self.options = [], [], {}
        def handle(request):
            self.requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer private-test-token')
            return handler(request)
        def factory(**kwargs):
            self.options.update(kwargs)
            return CLIENT(**kwargs, transport=httpx.MockTransport(handle))
        def pipe(value):
            self.pipe_calls.append(value)
            if value['action'] == 'hello':
                return CREDENTIALS
            if pipe_error:
                raise pipe_error
            return {'grant_id': GRANT}
        with patch('stylelora.runtime.project_cli.pipe_request', side_effect=pipe), \
                patch('stylelora.runtime.project_cli.httpx.Client', side_effect=factory), \
                patch('stylelora.runtime.project_cli.time.sleep', side_effect=sleep):
            result = invoke(*args)
        self.assertNotIn(CREDENTIALS['token'], json.dumps(result))
        if self.options:
            self.assertFalse(self.options['trust_env'])
            self.assertFalse(self.options['follow_redirects'])
        return result

    @staticmethod
    def accepted():
        return httpx.Response(202, json={'task': {'kind': 'operation', 'id': OP}, 'status_url': '/api/operations/'+OP})

    @staticmethod
    def operation(status='done', kind='project.open', **extra):
        return httpx.Response(200, json={'operation_id': OP, 'kind': kind, 'status': status,
                                        'result': {'project_id': PID, 'version': VERSION}, **extra})

    def test_create_waits_using_only_grant_over_http_and_retains_receipt(self):
        polls = []
        def handle(request):
            if request.method == 'POST':
                return self.accepted()
            polls.append(1)
            return self.operation('running' if len(polls) == 1 else 'done', kind='project.create')
        code, value, progress = self.run_cli(['init', 'private.slproj', '--name', '样例', '--trigger', 'test_style'], handle)
        self.assertEqual((code, value['status']), (0, 'done'))
        self.assertEqual([r.method for r in self.requests], ['POST', 'GET', 'GET'])
        self.assertEqual(self.pipe_calls[1]['purpose'], 'project_create')
        self.assertEqual(self.pipe_calls[1]['instance_id'], INSTANCE)
        self.assertTrue(Path(self.pipe_calls[1]['path']).is_absolute())
        self.assertEqual(json.loads(self.requests[0].content), {'grant_id': GRANT, 'name': '样例', 'trigger_word': 'test_style'})
        self.assertNotIn('private.slproj', json.dumps(value))
        self.assertEqual(self.requests[0].headers['Idempotency-Key'], value['cli']['request']['idempotency_key'])
        self.assertEqual(value['cli']['instance_id'], INSTANCE)
        self.assertIn(OP, progress)

    def test_open_detaches_after_acceptance_without_polling(self):
        code, value, _ = self.run_cli(['open', 'private.slproj', '--detach'], lambda r: self.accepted())
        self.assertEqual((code, value['status']), (0, 'accepted'))
        self.assertEqual(self.pipe_calls[1]['purpose'], 'project_open')
        self.assertEqual([r.url.path for r in self.requests], ['/api/projects/open'])

    def test_current_status_accepts_empty_and_does_not_mutate(self):
        for current in (None, {'project_id': PID, 'version': VERSION}):
            code, value, _ = self.run_cli(['status'], lambda r: httpx.Response(200, content=json.dumps(current), headers={'content-type': 'application/json'}))
            self.assertEqual((code, value['project']), (0, current))
            self.assertEqual([r.method for r in self.requests], ['GET'])
            self.assertEqual(len(self.pipe_calls), 1)
            self.assertNotIn('Idempotency-Key', self.requests[0].headers)

    def test_close_captures_handle_and_version_before_post(self):
        def handle(request):
            return httpx.Response(200, json={'project_id': PID, 'version': VERSION}) if request.method == 'GET' else self.accepted()
        code, value, _ = self.run_cli(['close', '--detach'], handle)
        self.assertEqual(code, 0)
        self.assertEqual(value['cli']['project_id'], PID)
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        self.assertEqual(self.requests[1].headers['If-Match'], '"'+VERSION+'"')
        self.assertEqual(self.requests[1].url.path, '/api/projects/'+PID+'/close')

    def test_wrong_handle_or_no_current_project_stops_before_close(self):
        for current, args in ((None, []), (None, ['--project-id', PID]),
                              ({'project_id': str(uuid4()), 'version': VERSION}, ['--project-id', PID])):
            code, _, _ = self.run_cli(['close', *args], lambda r: httpx.Response(200, content=json.dumps(current), headers={'content-type': 'application/json'}))
            self.assertEqual(code, 4)
            self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_close_version_conflict_never_reads_new_version_or_resubmits(self):
        def handle(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'project_id': PID, 'version': VERSION})
            return httpx.Response(409, json={'error': {'code': 'concurrency:version_conflict', 'outcome': 'rejected'}})
        code, value, _ = self.run_cli(['close'], handle)
        self.assertEqual((code, value['code']), (4, 'concurrency:version_conflict'))
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])

    def test_unknown_write_retains_identity_and_explicit_replay_reuses_grant(self):
        key = str(uuid4())
        def fail(request):
            raise httpx.ReadTimeout('private-test-token', request=request)
        code, original, _ = self.run_cli(['init', 'private.slproj', '--name', '项目', '--idempotency-key', key], fail)
        self.assertEqual((code, original['outcome']), (7, 'unknown'))
        self.assertEqual(len(self.requests), 1)
        code, replay, _ = self.run_cli(['init', '--grant-id', GRANT, '--name', '项目', '--instance-id', INSTANCE,
                                       '--idempotency-key', key, '--detach'], lambda r: self.accepted())
        self.assertEqual(code, 0)
        self.assertEqual(original['cli']['request'], replay['cli']['request'])
        self.assertEqual(len(self.pipe_calls), 1)
        self.assertEqual(len(self.requests), 1)

    def test_close_replay_does_not_read_or_target_new_current_project(self):
        key = str(uuid4())
        code, value, _ = self.run_cli(['close', '--project-id', PID, '--instance-id', INSTANCE, '--if-match', VERSION,
                                      '--idempotency-key', key, '--detach'], lambda r: self.accepted())
        self.assertEqual(code, 0)
        self.assertEqual([r.method for r in self.requests], ['POST'])
        self.assertEqual(value['cli']['request']['path'], '/api/projects/'+PID+'/close')
        self.assertEqual(value['cli']['request']['idempotency_key'], key)

    def test_changed_session_stops_before_http_or_path_grant(self):
        code, value, _ = self.run_cli(['open', 'private.slproj', '--instance-id', str(uuid4())], lambda r: self.fail('no HTTP'))
        self.assertEqual((code, value['code']), (4, 'instance:changed'))
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.pipe_calls), 1)

    def test_restart_between_hello_and_grant_stops_before_http(self):
        code, value, _ = self.run_cli(['open', 'private.slproj'], lambda r: self.fail('no HTTP'),
                                     pipe_error=ServiceError(409, 'instance:changed'))
        self.assertEqual((code, value['code']), (4, 'instance:changed'))
        self.assertEqual(self.requests, [])

    def test_poll_failure_retains_operation_without_post_retry(self):
        def handle(request):
            if request.method == 'POST':
                return self.accepted()
            raise httpx.ReadError('private-test-token')
        code, value, _ = self.run_cli(['open', 'private.slproj'], handle)
        self.assertEqual((code, value['cli']['operation_id']), (5, OP))
        self.assertEqual([r.method for r in self.requests], ['POST', 'GET'])

    def test_interrupt_while_waiting_does_not_cancel_service_operation(self):
        code, value, progress = self.run_cli(['status', '--operation-id', OP, '--wait'], lambda r: self.operation('running'), sleep=KeyboardInterrupt())
        self.assertEqual((code, value['cli']['operation_id']), (130, OP))
        self.assertIn('continue', progress)
        self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_interrupt_during_submission_retains_request(self):
        def handle(request):
            raise KeyboardInterrupt()
        code, value, _ = self.run_cli(['open', 'private.slproj'], handle)
        self.assertEqual(code, 130)
        self.assertIn('idempotency_key', value['cli']['request'])
        self.assertNotIn('operation_id', value['cli'])
        self.assertEqual(len(self.requests), 1)

    def test_task_state_exit_codes_do_not_claim_incomplete_as_success(self):
        for status, error, expected in (('running', None, 7), ('pending', None, 7), ('canceled', None, 7),
                                       ('interrupted', None, 7), ('failed', {'code': 'project:locked'}, 4),
                                       ('failed', {'code': 'storage:unavailable'}, 5),
                                       ('failed', {'code': 'storage:commit_unknown', 'outcome': 'unknown'}, 7)):
            with self.subTest(status=status, error=error):
                code, _, _ = self.run_cli(['status', '--operation-id', OP], lambda r: self.operation(status, error=error))
                self.assertEqual(code, expected)

    def test_malformed_or_unrelated_operation_is_rejected(self):
        for change, expected in (({'kind': 'storage.scan'}, 2), ({'operation_id': str(uuid4())}, 2),
                                 ({'status': 'finished'}, 5), ({'result': None}, 5),
                                 ({'kind': 'project.close', 'result': {'closed': False}}, 5),
                                 ({'status': 'failed', 'error': 'oops'}, 5)):
            with self.subTest(change=change):
                code, _, _ = self.run_cli(['status', '--operation-id', OP], lambda r: self.operation(**change))
                self.assertEqual(code, expected)

    def test_redirect_non_json_and_invalid_acceptance_are_unknown(self):
        for response in (httpx.Response(307, headers={'Location': 'http://remote.invalid/'}),
                         httpx.Response(200, text='private-test-token'), httpx.Response(202, json={'task': {}}),
                         httpx.Response(202, json={'task': {'kind': 'operation', 'id': OP}, 'status_url': 'http://remote.invalid/'}),
                         httpx.Response(500, json=None)):
            code, value, _ = self.run_cli(['open', 'private.slproj'], lambda r: response)
            self.assertEqual((code, value['outcome']), (7, 'unknown'))
            self.assertEqual(len(self.requests), 1)

    def test_bad_current_response_cannot_close_project(self):
        for value in ([], {}, {'project_id': PID, 'version': None}, {'project_id': 'bad', 'version': VERSION}):
            code, _, _ = self.run_cli(['close'], lambda r: httpx.Response(200, json=value))
            self.assertEqual(code, 5)
            self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_missing_service_never_starts_or_creates_project(self):
        with patch('stylelora.runtime.project_cli.pipe_request', side_effect=ServiceError(503, 'instance:not_running')), \
                patch('stylelora.runtime.project_cli.httpx.Client') as client, \
                patch('stylelora.project_db.project.create_project') as create:
            code, value, _ = invoke('init', 'private.slproj', '--name', 'test')
        self.assertEqual((code, value['code']), (3, 'instance:unavailable'))
        client.assert_not_called()
        create.assert_not_called()

    def test_invalid_arguments_fail_before_attachment(self):
        cases = [[], ['init', 'a.slproj'], ['open'], ['open', 'a.slproj', '--grant-id', GRANT],
                 ['open', '--grant-id', GRANT], ['open', '--grant-id', GRANT, '--instance-id', INSTANCE],
                 ['close', '--if-match', VERSION], ['close', '--idempotency-key', OP],
                 ['close', '--project-id', 'not-a-uuid'], ['close', '--if-match', '"quoted"'],
                 ['status', '--wait'], ['status', '--operation-id', 'http://remote.invalid'],
                 ['init', 'a.slproj', '--name', '  '], ['init', 'a.slproj', '--name', 'a', '--standalone', '--detach'],
                 ['init', 'a.slproj', '--name', 'a', '--standalone', '--idempotency-key', OP],
                 ['open', 'a.slproj', '--wait', '--detach'], ['open', 'a.slproj', '--standalone']]
        with patch('stylelora.runtime.project_cli.pipe_request') as pipe:
            for args in cases:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(['project', *args])
                self.assertEqual(error.exception.code, 2)
            pipe.assert_not_called()


class ProjectCLIAPITests(APITest):
    def cli(self, *args, code=0):
        server = PipeServer.__new__(PipeServer)
        server.runtime = self.runtime
        with patch('stylelora.runtime.project_cli.pipe_request', side_effect=server._dispatch), \
                patch('stylelora.runtime.project_cli.httpx.Client', return_value=contextlib.nullcontext(self.client)):
            actual, value, progress = invoke(*args)
        self.assertEqual(actual, code, value)
        self.assertNotIn(self.runtime.token, json.dumps(value)+progress)
        return value

    def test_close_create_and_current_status_share_http_lifecycle(self):
        self.assertEqual(self.cli('status')['project']['project_id'], self.pid)
        self.assertTrue(self.cli('close')['result']['closed'])
        self.assertIsNone(self.cli('status')['project'])
        root = self.base/'新项目.slproj'
        created = self.cli('init', root, '--name', '新项目', '--trigger', 'new_style')['result']
        self.assertEqual(created['metadata']['name'], '新项目')
        self.assertEqual(self.cli('status')['project']['project_id'], created['project_id'])
        self.cli('close')
        with open_project(root) as project:
            self.assertEqual(project.metadata.trigger_word, 'new_style')

    def test_open_same_path_keeps_handle_and_switch_releases_old_project(self):
        same = self.cli('open', self.root)
        self.assertEqual(same['result']['project_id'], self.pid)
        other = self.base/'other.slproj'
        self.cli('close')
        self.cli('init', other, '--name', 'other')
        original = self.cli('open', self.root)['result']['project_id']
        opened = self.cli('open', other)['result']
        self.assertNotEqual(opened['project_id'], original)
        old_lock = ProjectLock(self.root)
        old_lock.close()
        self.cli('close', '--project-id', original, code=4)
        self.assertEqual(self.runtime.current()['project_id'], opened['project_id'])

    def test_attached_create_does_not_replace_open_project(self):
        other = self.base/'other.slproj'
        value = self.cli('init', other, '--name', 'other', code=4)
        self.assertEqual(value['error']['code'], 'project:already_open')
        self.assertFalse(other.exists())
        self.assertEqual(self.runtime.current()['project_id'], self.pid)

    def test_replay_create_uses_original_grant_even_after_its_expiry(self):
        self.cli('close')
        root = self.base/'replay.slproj'
        first = self.cli('init', root, '--name', 'replay')
        info = first['cli']
        # Expiry cannot block the previously accepted idempotency receipt.
        self.runtime.grants.invalidate(None)
        args = ['init', '--grant-id', info['grant_id'], '--name', 'replay', '--instance-id', info['instance_id'],
                '--idempotency-key', info['request']['idempotency_key']]
        replay = self.cli(*args)
        self.assertEqual(replay['operation_id'], first['operation_id'])
        changed = self.cli(*[('changed' if a == 'replay' else a) for a in args], code=4)
        self.assertIn('idempotency', changed['code'])

    def test_replay_closed_project_cannot_close_a_newly_opened_project(self):
        first = self.cli('close')
        info = first['cli']
        opened = self.cli('open', self.root)['result']['project_id']
        replay = self.cli('close', '--project-id', info['project_id'], '--instance-id', info['instance_id'],
                          '--if-match', info['request']['if_match'], '--idempotency-key', info['request']['idempotency_key'])
        self.assertEqual(replay['operation_id'], first['operation_id'])
        self.assertEqual(self.runtime.current()['project_id'], opened)

    def test_stale_close_version_is_rejected_without_closing(self):
        value = self.cli('close', '--project-id', self.pid, '--instance-id', self.runtime.instance_id,
                         '--if-match', '0'*64, code=4)
        self.assertEqual(value['code'], 'concurrency:version_conflict')
        self.assertEqual(self.runtime.current()['project_id'], self.pid)

    def test_close_detaches_and_waits_for_project_users_to_drain(self):
        context = self.runtime.context
        self.runtime.retain(context)
        try:
            accepted = self.cli('close', '--detach')
            deadline = time.monotonic()+5
            while not context.closing and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(context.closing)
            status = self.cli('status', '--operation-id', accepted['cli']['operation_id'], code=7)
            self.assertEqual(status['status'], 'running')
            self.assertIsNone(self.cli('status')['project'])
        finally:
            self.runtime.release(context)
        value = self.cli('status', '--operation-id', accepted['cli']['operation_id'], '--wait')
        self.assertTrue(value['result']['closed'])
        with open_project(self.root):
            pass

    def test_wrong_purpose_grant_and_invalid_path_do_not_switch_project(self):
        wrong = self.runtime.grant(self.inputs, 'import_root', self.pid)
        self.cli('open', '--grant-id', wrong['grant_id'], '--instance-id', self.runtime.instance_id,
                 '--idempotency-key', str(uuid4()), code=3)
        self.cli('open', self.base/'missing.slproj', code=2)
        self.assertEqual(self.runtime.current()['project_id'], self.pid)

    def test_unknown_operation_does_not_create_task(self):
        value = self.cli('status', '--operation-id', str(uuid4()), code=4)
        self.assertEqual(value['code'], 'session:expired')


class ProjectCLIPipeTests(unittest.TestCase):
    def test_optional_instance_binding_checked_before_grant(self):
        server = PipeServer.__new__(PipeServer)
        server.runtime = Mock(instance_id=INSTANCE)
        request = {'action': 'grant', 'path': 'example.slproj', 'purpose': 'project_open'}
        with self.assertRaises(ServiceError) as error:
            server._dispatch({**request, 'instance_id': str(uuid4())})
        self.assertEqual(error.exception.code, 'instance:changed')
        server.runtime.grant.assert_not_called()
        server._dispatch({**request, 'instance_id': INSTANCE})
        server._dispatch(request)  # Existing clients remain compatible.
        self.assertEqual(server.runtime.grant.call_count, 2)


@unittest.skipUnless(os.name == 'nt', 'Windows product ownership')
class ProjectCLIStandaloneTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'standalone.slproj'

    def test_standalone_create_closes_and_releases_both_locks_without_attachment(self):
        with patch('stylelora.runtime.project_cli.pipe_request') as pipe, patch('stylelora.runtime.project_cli.httpx.Client') as client:
            code, value, _ = invoke('init', self.root, '--name', '独立', '--trigger', 'standalone_style', '--standalone')
        self.assertEqual((code, value['closed'], value['cli']['mode']), (0, True, 'standalone'))
        pipe.assert_not_called()
        client.assert_not_called()
        with open_project(self.root) as project:
            self.assertEqual(project.metadata.trigger_word, 'standalone_style')
        owner = ProductLock()
        owner.close()

    def test_existing_container_is_never_overwritten_and_product_lock_is_released(self):
        with create_project(self.root, name='original', trigger_word=''):
            pass
        original = (self.root/'project.json').read_bytes()
        code, value, _ = invoke('init', self.root, '--name', 'replacement', '--standalone')
        self.assertEqual((code, value['code']), (4, 'project:exists'))
        self.assertEqual(original, (self.root/'project.json').read_bytes())
        owner = ProductLock()
        owner.close()

    def test_active_product_owner_blocks_standalone_without_creating_files(self):
        owner = ProductLock()
        try:
            code, value, _ = invoke('init', self.root, '--name', 'blocked', '--standalone')
            self.assertEqual((code, value['code']), (4, 'instance:already_running'))
            self.assertFalse(self.root.exists())
        finally:
            owner.close()

    def test_close_failure_reports_unknown_created_state_without_retry(self):
        context = Mock()
        context.__enter__ = Mock(return_value=Mock(metadata={}, schema_version=1))
        context.__exit__ = Mock(side_effect=OSError('private value'))
        with patch('stylelora.project_db.project.create_project', return_value=context) as create:
            code, value, _ = invoke('init', self.root, '--name', 'test', '--standalone')
        self.assertEqual((code, value['outcome'], value['cli']['created']), (7, 'unknown', True))
        self.assertNotIn('private value', json.dumps(value))
        create.assert_called_once()
        owner = ProductLock()
        owner.close()
