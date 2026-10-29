import contextlib
import io
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.runtime import startup
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import PipeServer

INSTANCE = str(uuid4())
CREDENTIALS = {'protocol_version': 1, 'authority': '127.0.0.1:8301', 'pid': 321,
               'instance_id': INSTANCE, 'token': 'private-fixture-token'}
CLIENT = httpx.Client


def absent():
    return ServiceError(503, 'instance:not_running')


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.info = {}
        self.child = Mock(pid=321)
        self.child.poll.return_value = None
        self.progress = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stderr(self.progress))
        self.stack.enter_context(patch.object(startup.time, 'monotonic', side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(startup.time, 'sleep', side_effect=self.advance))
        self.pipe = self.stack.enter_context(patch.object(startup, 'pipe_request', return_value=CREDENTIALS))
        self.launch = self.stack.enter_context(patch.object(startup, 'launch', return_value=self.child))
        self.ready = self.stack.enter_context(patch.object(startup, 'http_ready', return_value=True))

    def advance(self, seconds):
        self.clock += seconds

    def ensure(self):
        try:
            return startup.ensure_service(self.info, timeout=.35)
        finally:
            self.assertNotIn(CREDENTIALS['token'], json.dumps(self.info) + self.progress.getvalue())
            self.child.terminate.assert_not_called()
            self.child.kill.assert_not_called()

    def test_existing_service_is_verified_without_spawning(self):
        self.assertEqual(self.ensure(), CREDENTIALS)
        self.launch.assert_not_called()
        self.assertEqual(self.info['service_start']['status'], 'ready')
        self.assertNotIn('spawned_pid', self.info['service_start'])

    def test_absent_service_is_spawned_once_and_waits_for_http(self):
        self.pipe.side_effect = [absent(), absent(), CREDENTIALS, CREDENTIALS]
        self.ready.side_effect = [False, True]
        self.assertEqual(self.ensure(), CREDENTIALS)
        self.launch.assert_called_once_with()
        self.assertTrue(all(call.args == ({'action': 'hello'},) for call in self.pipe.call_args_list))
        self.assertEqual(self.info['service_start']['spawned_pid'], 321)

    def test_a_concurrent_winner_can_be_ready_after_losing_child_exits(self):
        self.pipe.side_effect = [absent(), absent(), {**CREDENTIALS, 'pid': 456}]
        self.child.poll.return_value = 0
        self.assertEqual(self.ensure()['pid'], 456)
        self.launch.assert_called_once()
        self.assertEqual(self.info['service_start']['server_pid'], 456)

    def test_child_failure_is_not_restarted(self):
        self.pipe.side_effect = absent()
        self.child.poll.return_value = 1
        with self.assertRaisesRegex(ServiceError, 'process: start failed'):
            self.ensure()
        self.launch.assert_called_once()
        self.assertEqual(self.info['service_start']['status'], 'failed')

    def test_spawn_error_is_sanitized(self):
        self.pipe.side_effect = absent()
        self.launch.side_effect = OSError('private fixture path')
        with self.assertRaises(ServiceError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.code, 'process:start_failed')
        self.assertNotIn('private fixture', str(caught.exception))

    def test_timeout_preserves_unconfirmed_service_and_does_not_relaunch(self):
        self.pipe.side_effect = absent()
        with self.assertRaises(ServiceError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.outcome, 'unknown')
        self.launch.assert_called_once()
        self.assertEqual(self.info['service_start']['status'], 'unconfirmed')

    def test_slow_initial_probe_does_not_spawn_after_deadline(self):
        def expired(_):
            self.advance(1)
            raise absent()
        self.pipe.side_effect = expired
        with self.assertRaises(ServiceError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.outcome, 'rejected')
        self.launch.assert_not_called()

    def test_existing_unready_service_is_never_replaced(self):
        self.ready.return_value = False
        with self.assertRaises(ServiceError):
            self.ensure()
        self.launch.assert_not_called()

    def test_interruption_leaves_service_running_and_reports_spawned_pid(self):
        self.pipe.side_effect = [absent(), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.ensure()
        self.assertEqual(self.info['service_start'], {'status': 'unconfirmed', 'spawned_pid': 321})

    def test_pipe_identity_failure_or_unknown_read_never_starts_a_service(self):
        for code in ('instance:identity_mismatch', 'instance:empty_response', 'instance:disconnected'):
            self.pipe.side_effect = ServiceError(403, code)
            with self.subTest(code=code), self.assertRaises(ServiceError):
                self.ensure()
        self.launch.assert_not_called()

    def test_invalid_hello_is_rejected_before_http_or_spawn(self):
        for changes in ({'authority': 'evil.example:80'}, {'authority': '127.0.0.1:65536'}, {'token': 'unsafe\n'},
                        {'pid': True}, {'protocol_version': True}, {'instance_id': 'wrong'}):
            self.pipe.return_value = {**CREDENTIALS, **changes}
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.ensure()
        self.launch.assert_not_called()
        self.ready.assert_not_called()


class ReadinessTests(unittest.TestCase):
    def check(self, handler):
        options, calls = {}, []
        def factory(**kwargs):
            options.update(kwargs)
            return CLIENT(**kwargs, transport=httpx.MockTransport(lambda request: (calls.append(request), handler(request))[1]))
        with patch.object(startup.httpx, 'Client', side_effect=factory):
            result = startup.http_ready(CREDENTIALS, .2)
        self.assertFalse(options['trust_env'])
        self.assertFalse(options['follow_redirects'])
        self.assertEqual(options['timeout'], .2)
        self.assertEqual([(call.method, call.url.path) for call in calls], [('GET', '/api/session')])
        return result

    def test_same_authenticated_http_session_confirms_readiness(self):
        def handler(request):
            self.assertEqual(request.headers['authorization'], 'Bearer ' + CREDENTIALS['token'])
            return httpx.Response(200, json={'instance_id': INSTANCE, 'version': INSTANCE, 'api_schema_version': 1, 'draining': False})
        self.assertTrue(self.check(handler))

    def test_http_not_listening_yet_remains_unready(self):
        def handler(request):
            raise httpx.ConnectError('not listening', request=request)
        self.assertFalse(self.check(handler))

    def test_redirect_and_authentication_failures_are_not_followed(self):
        for status in (302, 401, 403, 500):
            with self.subTest(status=status), self.assertRaises(ServiceError):
                self.check(lambda _: httpx.Response(status, headers={'Location': 'https://example.invalid/'}))

    def test_malformed_or_changed_session_and_draining_are_rejected(self):
        baseline = {'instance_id': INSTANCE, 'version': INSTANCE, 'api_schema_version': 1, 'draining': False}
        for changes in ({'instance_id': str(uuid4())}, {'version': 'bad'}, {'draining': True}, {'draining': None}, {'api_schema_version': True}):
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.check(lambda _: httpx.Response(200, json={**baseline, **changes}))
        with self.assertRaises(ServiceError) as caught:
            self.check(lambda _: httpx.Response(200, content=b'{', headers={'content-type': 'application/json'}))
        self.assertEqual(caught.exception.code, 'instance:invalid_response')


class StartupCLITests(unittest.TestCase):
    def test_launch_uses_current_python_hidden_detached_and_no_inherited_streams(self):
        with patch.object(startup.subprocess, 'Popen') as popen:
            startup.launch()
        args, options = popen.call_args
        self.assertEqual(args[0], [sys.executable, '-m', 'stylelora', 'serve', '--no-browser'])
        for stream in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(options[stream], subprocess.DEVNULL)
        self.assertFalse(options['shell'])
        self.assertTrue(options['close_fds'])
        if sys.platform == 'win32':
            self.assertTrue(options['creationflags'] & subprocess.DETACHED_PROCESS)
            self.assertTrue(options['creationflags'] & subprocess.CREATE_NEW_PROCESS_GROUP)
            self.assertEqual(options['startupinfo'].wShowWindow, subprocess.SW_HIDE)

    def test_startup_option_scope_and_replay_combinations_fail_before_io(self):
        commands = [
            ['project', 'init', 'a.slproj', '--name', 'a', '--start-service', '--standalone'],
            ['project', 'open', 'a.slproj', '--start-service', '--instance-id', INSTANCE],
            ['project', 'open', 'a.slproj', '--start-service', '--idempotency-key', INSTANCE],
            ['project', 'open', '--grant-id', INSTANCE, '--instance-id', INSTANCE, '--idempotency-key', INSTANCE, '--start-service'],
            ['project', 'status', '--start-service'], ['project', 'close', '--start-service'],
            ['project', 'open', 'a.slproj', '--startup-timeout', '10'], ['ui', '--startup-timeout', '10'],
            ['ui', '--start-service', '--startup-timeout', 'nan'], ['ui', '--start-service', '--startup-timeout', '121'],
        ]
        with patch.object(startup, 'launch') as launch, contextlib.redirect_stderr(io.StringIO()):
            for command in commands:
                with self.subTest(command=command), self.assertRaises(SystemExit) as caught:
                    main(command)
                self.assertEqual(caught.exception.code, 2)
        launch.assert_not_called()

    def test_ui_binds_bootstrap_to_verified_instance_and_only_prints_requested_link(self):
        output = io.StringIO()
        with patch('stylelora.runtime.cli.ensure_service', return_value=CREDENTIALS) as ensure, \
                patch('stylelora.runtime.cli.pipe_request', return_value={'url': 'http://127.0.0.1:8301/#bootstrap=one-use'}) as pipe, \
                patch('stylelora.runtime.cli.webbrowser.open') as browser, contextlib.redirect_stdout(output):
            self.assertEqual(main(['ui', '--start-service', '--startup-timeout', '10', '--print-link']), 0)
        ensure.assert_called_once_with({}, timeout=10)
        pipe.assert_called_once_with({'action': 'bootstrap', 'instance_id': INSTANCE})
        browser.assert_not_called()
        self.assertEqual(output.getvalue().strip(), 'http://127.0.0.1:8301/#bootstrap=one-use')

    def test_ui_default_does_not_start_and_bootstrap_is_not_retried(self):
        with patch('stylelora.runtime.cli.ensure_service') as ensure, \
                patch('stylelora.runtime.cli.pipe_request', side_effect=ServiceError(503, 'instance:empty_response', outcome='unknown')) as pipe, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['ui']), 7)
        ensure.assert_not_called()
        pipe.assert_called_once_with({'action': 'bootstrap'})

    def test_bound_bootstrap_rejects_a_restarted_service(self):
        server = object.__new__(PipeServer)
        server.runtime = SimpleNamespace(instance_id=INSTANCE, issue_bootstrap=Mock(return_value='link'))
        with self.assertRaises(ServiceError):
            server._dispatch({'action': 'bootstrap', 'instance_id': str(uuid4())})
        server.runtime.issue_bootstrap.assert_not_called()
        self.assertEqual(server._dispatch({'action': 'bootstrap', 'instance_id': INSTANCE}), {'url': 'link'})

    def test_project_start_failure_sends_no_grant_or_mutation(self):
        output = io.StringIO()
        with patch('stylelora.runtime.project_cli.ensure_service', side_effect=ServiceError(503, 'process:start_timeout', outcome='unknown')), \
                patch('stylelora.runtime.project_cli.pipe_request') as pipe, \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(['project', 'open', 'fixture.slproj', '--start-service']), 7)
        pipe.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['code'], 'process:start_timeout')

    def test_project_startup_reuses_existing_grant_and_http_flow(self):
        from . import test_project_cli as fixtures
        for command in ('open', 'init'):
            helper = fixtures.ProjectCLITransportTests()
            kind = 'project.open' if command == 'open' else 'project.create'
            def handler(request):
                return helper.accepted() if request.method == 'POST' else helper.operation(kind=kind)
            with patch('stylelora.runtime.project_cli.ensure_service', return_value=fixtures.CREDENTIALS) as ensure:
                code, body, _ = helper.run_cli([command, 'fixture.slproj', '--start-service',
                                               *(['--name', 'sample'] if command == 'init' else [])], handler)
            self.assertEqual(code, 0)
            self.assertEqual(body['result']['project_id'], fixtures.PID)
            self.assertEqual(sum(request.method == 'POST' for request in helper.requests), 1)
            self.assertEqual(helper.pipe_calls[0]['action'], 'grant')
            self.assertEqual(helper.pipe_calls[0]['instance_id'], fixtures.INSTANCE)
            self.assertEqual(ensure.call_args.kwargs, {'timeout': 30})

    def test_unknown_project_write_after_startup_is_not_replayed(self):
        from . import test_project_cli as fixtures
        helper = fixtures.ProjectCLITransportTests()
        def handler(request):
            raise httpx.ReadError('lost reply', request=request)
        with patch('stylelora.runtime.project_cli.ensure_service', return_value=fixtures.CREDENTIALS):
            code, body, _ = helper.run_cli(['open', 'fixture.slproj', '--start-service'], handler)
        self.assertEqual(code, 7)
        self.assertEqual(len(helper.requests), 1)
        self.assertEqual(body['cli']['instance_id'], fixtures.INSTANCE)
        self.assertEqual(body['cli']['request']['method'], 'POST')
