import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.project_db.validation import digest
from stylelora.prompt_production.types import SamplingRequest
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import PipeServer
from .support import TEMP_ROOT
from . import test_prompt_production as sampling_support
from . import test_prompt_composer as composer_support
from .test_prompt_production import entry, feature_body

CLIENT = httpx.Client
TASK, INSTANCE, KEY, DICTIONARY = (str(uuid4()) for _ in range(4))
BASE = '/api/prompt-production'
PATH = BASE+'/tasks/'+TASK
CREDENTIALS = {'authority': '127.0.0.1:8301', 'token': 'private-test-token', 'instance_id': INSTANCE, 'protocol_version': 1}


def spec():
    return {'prompt_dictionary_ids': [DICTIONARY], 'coverage_enabled': False, 'plan': {'target_count': 1, 'random_seed': 7}}


def preview():
    return {'request': SamplingRequest.model_validate(spec()).model_dump(), 'preview_digest': 'a'*64,
            'ready': True, 'count': 1, 'requested': 1, 'available': 1}


def task(status='done', **extra):
    return {'task_id': TASK, 'revision': 4, 'status': status, 'ready': status == 'done', 'reason': None,
            'progress': None, 'version': digest([TASK, 4]), **extra}


def accepted(**extra):
    return httpx.Response(202, json={**task(), 'status_url': PATH, **extra})


def seeds():
    items = [{'seq': 1, 'prompt': 'no humans, forest', 'aspect': None}]
    return {'items': items, 'count': 1, 'digest': digest(items)}


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(['prompt', *map(str, args)])
    return code, json.loads(out.getvalue()), err.getvalue()


class PromptCLITransportTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def save(self, value, name='spec.json'):
        path = self.base/name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def plan(self):
        view = preview()
        plan = {'schema_version': 1, 'instance_id': INSTANCE, 'idempotency_key': KEY,
                'body': {'task_id': TASK, 'request': view['request'], 'preview_digest': view['preview_digest']}, 'preview': view}
        return {'plan': plan, 'confirmation_digest': digest(plan)}

    def run_args(self, document=None, *extra):
        document = self.plan() if document is None else document
        return ['run', '--spec', self.save(document), '--confirm-digest', document['confirmation_digest'], *extra]

    def cli(self, args, handler, *, sleep=None):
        self.requests, self.options = [], {}
        def handle(request):
            self.requests.append(request)
            self.assertTrue(request.url.path.startswith(BASE))
            self.assertEqual(request.headers['Authorization'], 'Bearer private-test-token')
            return handler(request)
        def factory(**kwargs):
            self.options.update(kwargs)
            return CLIENT(**kwargs, transport=httpx.MockTransport(handle))
        with patch('stylelora.runtime.attached_cli.pipe_request', return_value=CREDENTIALS), \
                patch('stylelora.runtime.attached_cli.httpx.Client', side_effect=factory), \
                patch('stylelora.runtime.prompt_cli.time.sleep', side_effect=sleep):
            result = invoke(*args)
        self.assertNotIn(CREDENTIALS['token'], json.dumps(result))
        if self.options:
            self.assertFalse(self.options['trust_env'])
            self.assertFalse(self.options['follow_redirects'])
        return result

    def test_preview_freezes_server_seed_request_and_allocates_stable_task(self):
        raw = spec()
        raw['plan'].pop('random_seed')
        code, value, _ = self.cli(['preview', '--spec', self.save(raw)], lambda r: httpx.Response(200, json=preview()))
        self.assertEqual(code, 0)
        self.assertEqual(value['confirmation_digest'], digest(value['plan']))
        self.assertEqual(value['plan']['body']['request']['plan']['random_seed'], 7)
        self.assertEqual([r.url.path for r in self.requests], [BASE+'/coverage/preview'])
        self.assertNotIn('project_id', value['plan'])

    def test_run_waits_for_task_done_without_opening_a_project(self):
        polls = []
        def handle(request):
            if request.method == 'POST':
                return accepted()
            polls.append(1)
            return httpx.Response(200, json=task('running' if len(polls) == 1 else 'done'))
        code, value, progress = self.cli(self.run_args(), handle)
        self.assertEqual((code, value['status']), (0, 'done'))
        self.assertEqual([r.method for r in self.requests], ['POST', 'GET', 'GET'])
        self.assertEqual(value['cli']['task_id'], TASK)
        self.assertIn(TASK, progress)

    def test_detach_preserves_server_task_state_without_claiming_completion(self):
        code, value, _ = self.cli(self.run_args(None, '--detach'), lambda r: accepted(status='running', ready=False))
        self.assertEqual((code, value['status'], value['task_status']), (0, 'accepted', 'running'))
        self.assertEqual(len(self.requests), 1)

    def test_lost_acceptance_keeps_identity_and_explicit_replay_uses_same_body(self):
        def fail(request):
            raise httpx.ReadTimeout('private-test-token')
        code, value, _ = self.cli(self.run_args(), fail)
        self.assertEqual((code, value['outcome'], value['cli']['task_id']), (7, 'unknown', TASK))
        original = value['cli']['request']
        self.assertEqual(original['idempotency_key'], KEY)
        self.assertEqual(len(self.requests), 1)
        code, replay, _ = self.cli(self.run_args(None, '--detach'), lambda r: accepted())
        self.assertEqual(code, 0)
        self.assertEqual(replay['cli']['request'], original)

    def test_changed_plan_and_recomputed_invalid_plan_fail_before_attachment(self):
        changes = [(lambda p: p['body']['request']['plan'].update(target_count=2), False),
                   (lambda p: p.update(schema_version=True), True),
                   (lambda p: p.update(project_id=str(uuid4())), True),
                   (lambda p: p['body']['request'].update(token='private-test-token'), True),
                   (lambda p: p['preview'].update(preview_digest='b'*64), True)]
        for change, recompute in changes:
            document = self.plan()
            change(document['plan'])
            if recompute:
                document['confirmation_digest'] = digest(document['plan'])
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke(*self.run_args(document))
            self.assertEqual((code, value['code']), (2, 'prompt:invalid_plan'))
            self.assertNotIn('private-test-token', json.dumps(value))
            pipe.assert_not_called()

    def test_old_plan_instance_is_rejected_without_http(self):
        document = self.plan()
        document['plan']['instance_id'] = str(uuid4())
        document['confirmation_digest'] = digest(document['plan'])
        code, value, _ = self.cli(self.run_args(document), lambda r: self.fail('No HTTP'))
        self.assertEqual((code, value['code']), (4, 'instance:changed'))
        self.assertEqual(self.requests, [])

    def test_control_captures_revision_in_json_and_does_not_retry_conflict(self):
        def handle(request):
            if request.method == 'GET':
                return httpx.Response(200, json=task('running'))
            return httpx.Response(409, json={'error': {'code': 'prompt:revision_conflict', 'outcome': 'rejected'}})
        code, value, _ = self.cli(['pause', TASK], handle)
        self.assertEqual(code, 4)
        self.assertEqual(value['cli']['request']['body'], {'action': 'pause', 'expected_revision': 4})
        self.assertNotIn('If-Match', self.requests[-1].headers)
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])

    def test_control_replay_preserves_original_revision_and_key_without_get(self):
        code, value, _ = self.cli(['cancel', TASK, '--expected-revision', '4', '--idempotency-key', KEY, '--instance-id', INSTANCE],
                                 lambda r: httpx.Response(200, json=task('canceled')))
        self.assertEqual(code, 0)
        self.assertEqual([r.method for r in self.requests], ['POST'])
        self.assertEqual(value['cli']['request']['idempotency_key'], KEY)

    def test_resume_wait_and_detach_do_not_conflate_active_with_done(self):
        def handle(request):
            return httpx.Response(200, json=task('running' if request.method == 'POST' else 'done'))
        for extra, count in (([], 3), (['--detach'], 2)):
            code, _, _ = self.cli(['resume', TASK, *extra], handle)
            self.assertEqual(code, 0)
            self.assertEqual(len(self.requests), count)

    def test_task_readiness_and_partial_failures_have_nonzero_exit(self):
        for value, expected in ((task(ready=False), 6), (task(append_error='dictionary:conflict'), 6),
                                (task(progress={'counts': {'failed': 1}}), 6), (task('paused'), 7),
                                (task('canceled'), 7), (task('failed'), 5), (task('pending', reason='dependency:missing'), 7)):
            code, _, _ = self.cli(['status', TASK, '--wait'], lambda r: httpx.Response(200, json=value))
            self.assertEqual(code, expected)
            self.assertEqual(len(self.requests), 1)
        code, value, _ = self.cli(['preview', '--spec', self.save(spec())],
                                 lambda r: httpx.Response(200, json={**preview(), 'ready': False}))
        self.assertEqual(code, 6)
        self.assertIn('plan', value)

    def test_poll_error_and_interrupt_never_cancel_background_task(self):
        def handle(request):
            if request.method == 'POST':
                return accepted()
            raise httpx.ReadError('private-test-token')
        code, value, _ = self.cli(self.run_args(), handle)
        self.assertEqual((code, value['cli']['task_id']), (5, TASK))
        self.assertEqual([r.method for r in self.requests], ['POST', 'GET'])
        code, value, _ = self.cli(['status', TASK, '--wait'], lambda r: httpx.Response(200, json=task('running')), sleep=KeyboardInterrupt())
        self.assertEqual((code, value['cli']['task_id']), (130, TASK))
        self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_post_interrupt_keeps_preallocated_task_and_key(self):
        def handle(request):
            raise KeyboardInterrupt()
        code, value, _ = self.cli(self.run_args(), handle)
        self.assertEqual((code, value['cli']['task_id'], value['cli']['request']['idempotency_key']), (130, TASK, KEY))

    def test_invalid_or_redirected_acceptance_never_follows_url_or_retries(self):
        for response in (httpx.Response(202, json={}), accepted(task_id=str(uuid4())), accepted(status_url='http://remote.invalid'),
                         httpx.Response(307, headers={'Location': 'http://remote.invalid'}), httpx.Response(200, text='private-test-token')):
            code, value, _ = self.cli(self.run_args(), lambda r: response)
            self.assertEqual((code, value['outcome']), (7, 'unknown'))
            self.assertEqual(len(self.requests), 1)

    def test_seeds_are_content_only_and_bound_to_explicit_revision(self):
        code, value, _ = self.cli(['seeds', TASK, '--expected-revision', '4'], lambda r: httpx.Response(200, json=seeds()))
        self.assertEqual((code, value), (0, seeds()))
        self.assertNotIn('cli', value)
        self.assertEqual(json.loads(self.requests[0].content), {'expected_revision': 4})
        self.assertEqual(len(self.requests), 1)

    def test_invalid_seed_digest_shape_and_product_references_are_rejected(self):
        for change in (lambda v: v.update(digest='0'*64), lambda v: v.update(count=2),
                       lambda v: v.update(task_id=TASK), lambda v: v['items'][0].update(source_id=1),
                       lambda v: v['items'][0].update(seq=True), lambda v: v['items'][0].update(prompt='')):
            value = seeds()
            change(value)
            if value['digest'] != '0'*64:
                value['digest'] = digest(value['items'])
            code, result, _ = self.cli(['seeds', TASK, '--expected-revision', '4'], lambda r: httpx.Response(200, json=value))
            self.assertEqual((code, result['code']), (5, 'request:invalid_response'))

    def test_status_rejects_inconsistent_task_and_list_checks_page_shape(self):
        for change in ({'task_id': str(uuid4())}, {'revision': True}, {'status': 'paused', 'ready': True},
                       {'progress': {'counts': {'failed': -1}}}, {'version': None}):
            code, _, _ = self.cli(['status', TASK], lambda r: httpx.Response(200, json=task(**change)))
            self.assertEqual(code, 5)
        code, _, _ = self.cli(['list'], lambda r: httpx.Response(200, json={}))
        self.assertEqual(code, 5)

    def test_pagination_preserves_encoded_cursor_without_control_calls(self):
        for args in (['list'], ['outcomes', TASK]):
            code, _, _ = self.cli([*args, '--limit', '1', '--cursor', 'a&b=?'],
                                  lambda r: httpx.Response(200, json={'items': [], 'has_more': False, 'next_cursor': None}))
            self.assertEqual(code, 0)
            self.assertEqual(self.requests[0].url.params['cursor'], 'a&b=?')
            self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_bad_files_secret_fields_and_unsupported_launcher_never_attach(self):
        path = self.base/'raw.json'
        for raw, expected in ((b'{"token":"private-test-token"}', 2), (b'{"plan":{},"plan":{}}', 2),
                              (b'{"source_weights":NaN}', 2), (b'\xff', 2), (b'[]', 2),
                              (json.dumps({**spec(), 'launcher_selection': {'token': 'private-test-token'}}).encode(), 5)):
            path.write_bytes(raw)
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke('preview', '--spec', path)
            self.assertEqual(code, expected)
            self.assertNotIn('private-test-token', json.dumps(value))
            pipe.assert_not_called()
        with patch('stylelora.runtime.prompt_cli.FILE_LIMIT', 8), patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
            code, _, _ = invoke('preview', '--spec', self.save(spec()))
        self.assertEqual(code, 2)
        pipe.assert_not_called()

    def test_oversized_preview_is_not_returned_as_unreadable_success(self):
        path = self.save(spec())
        with patch('stylelora.runtime.prompt_cli.FILE_LIMIT', 600):
            code, value, _ = self.cli(['preview', '--spec', path], lambda r: httpx.Response(200, json=preview()))
        self.assertEqual((code, value['code']), (2, 'prompt:plan_too_large'))
        self.assertNotIn('plan', value)

    def test_invalid_arguments_fail_before_attachment(self):
        cases = [[], ['preview'], ['run', '--spec', 'a.json'], ['status', 'not-uuid'], ['seeds', TASK],
                 ['seeds', TASK, '--expected-revision', '0'], ['pause', TASK, '--idempotency-key', KEY],
                 ['list', '--limit', '201'], ['outcomes', TASK, '--cursor', ''],
                 ['preview', '--spec', 'a', '--project-id', TASK], ['list', '--standalone'],
                 ['resume', TASK, '--wait', '--detach']]
        with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
            for args in cases:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    main(['prompt', *args])
                self.assertEqual(exc.exception.code, 2)
            pipe.assert_not_called()

    def test_missing_instance_and_transport_setup_are_safe_errors(self):
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=ServiceError(503, 'instance:not_running')):
            code, value, _ = invoke('list')
        self.assertEqual((code, value['code']), (3, 'instance:unavailable'))
        with patch('stylelora.runtime.attached_cli.pipe_request', return_value=CREDENTIALS), \
                patch('stylelora.runtime.attached_cli.httpx.Client', side_effect=OSError('private-test-token')):
            code, value, _ = invoke('list')
        self.assertEqual(code, 5)
        self.assertNotIn('private-test-token', json.dumps(value))


class PromptCLIAPITests(unittest.TestCase):
    tearDown = sampling_support.PromptTests.tearDown
    request = sampling_support.PromptTests.request
    dictionary = sampling_support.PromptTests.dictionary
    sampling = sampling_support.PromptTests.sampling
    wait = sampling_support.PromptTests.wait
    mock = composer_support.ComposerTests.mock
    inputs = composer_support.ComposerTests.inputs

    def setUp(self):
        sampling_support.PromptTests.setUp(self)
        self.client.headers['Authorization'] = 'Bearer '+self.runtime.token

    def cli(self, *args, code=0):
        server = PipeServer.__new__(PipeServer)
        server.runtime = self.runtime
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=server._dispatch), \
                patch('stylelora.runtime.attached_cli.httpx.Client', return_value=contextlib.nullcontext(self.client)):
            actual, value, progress = invoke(*args)
        self.assertEqual(actual, code, value)
        for secret in (self.runtime.token, 'unit-test-secret'):
            self.assertNotIn(secret, json.dumps(value)+progress)
        return value

    def preview_cli(self, request=None, *, code=0):
        request = self.sampling(self.dictionary()) if request is None else request
        path = self.root/'sampling.json'
        path.write_text(json.dumps(request), encoding='utf-8')
        return self.cli('preview', '--spec', path, code=code)

    def run_cli(self, preview, *extra, code=0):
        path = self.root/'plan.json'
        path.write_text(json.dumps(preview), encoding='utf-8')
        return self.cli('run', '--spec', path, '--confirm-digest', preview['confirmation_digest'], *extra, code=code)

    def test_local_preview_run_list_results_and_content_seeds_without_project(self):
        preview = self.preview_cli()
        self.assertEqual(self.service.list(), [])
        done = self.run_cli(preview)
        self.assertTrue(done['ready'])
        self.assertIsNone(self.runtime.context)
        self.assertEqual(self.cli('list')['items'][0]['task_id'], done['task_id'])
        rows = self.cli('outcomes', done['task_id'], '--limit', '1')
        self.assertEqual(len(rows['items']), 1)
        self.assertTrue(rows['has_more'])
        result = self.cli('seeds', done['task_id'], '--expected-revision', done['revision'])
        self.assertEqual(result['count'], 12)
        self.assertEqual(set(result), {'items', 'count', 'digest'})
        self.assertEqual(set(result['items'][0]), {'seq', 'prompt', 'aspect'})
        self.assertEqual(result['digest'], digest(result['items']))

    def test_changed_dictionary_rejects_unsubmitted_plan_but_replay_keeps_existing_task(self):
        dictionary = self.dictionary()
        preview = self.preview_cli(self.sampling(dictionary))
        done = self.run_cli(preview)
        other = self.preview_cli(self.sampling(dictionary))
        self.request('PUT', '/dictionaries/'+dictionary['dictionary_id'], {**feature_body(), 'name': 'changed'}, version=dictionary['version'])
        self.assertEqual(self.run_cli(preview)['task_id'], done['task_id'])
        failed = self.run_cli(other, code=4)
        self.assertEqual(failed['code'], 'prompt:preview_stale')
        self.assertEqual(len(self.service.list()), 1)

    def test_lost_acceptance_is_reconciled_without_a_second_refinement(self):
        self.mock()
        preview = self.preview_cli(self.inputs())
        self.assertEqual(self.calls, [])
        original = self.client.request
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path == BASE+'/tasks':
                raise httpx.ReadTimeout('lost response')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            value = self.run_cli(preview, code=7)
        done = self.run_cli(preview)
        self.assertEqual(done['task_id'], value['cli']['task_id'])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(done['ready'])

    def test_incomplete_sampling_has_partial_exit_and_no_seeds(self):
        dictionary = self.dictionary({'kind': 'prompts', 'name': 'short', 'entries': [entry(['forest'])]})
        request = self.sampling(dictionary, coverage_enabled=False, plan={'target_count': 2, 'random_seed': 7})
        preview = self.preview_cli(request, code=6)
        done = self.run_cli(preview, code=6)
        self.assertFalse(done['ready'])
        rejected = self.cli('seeds', done['task_id'], '--expected-revision', done['revision'], code=4)
        self.assertEqual(rejected['code'], 'seeds:not_ready')

    def test_stale_seed_revision_is_rejected_without_changed_content(self):
        done = self.run_cli(self.preview_cli())
        value = self.cli('seeds', done['task_id'], '--expected-revision', done['revision']-1, code=4)
        self.assertEqual(value['code'], 'prompt:revision_conflict')
        self.assertEqual(self.service.get(done['task_id'])['revision'], done['revision'])

    def hold(self):
        from stylelora.prompt_production.tasks import sample
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def held(snapshot, **kw):
            entered.set()
            if not release.wait(8):
                raise AssertionError('test gate timed out')
            return sample(snapshot, **kw)
        return entered, release, patch('stylelora.prompt_production.tasks.sample', side_effect=held)

    def test_pause_resume_cancel_and_control_replay_keep_cas_and_no_implicit_resume(self):
        preview = self.preview_cli()
        entered, release, gate = self.hold()
        with gate:
            try:
                started = self.run_cli(preview, '--detach')
                self.assertTrue(entered.wait(2))
                identifier = started['task_id']
                paused = self.cli('pause', identifier)
                self.assertEqual(paused['status'], 'paused')
                self.run_cli(preview, code=7)
                self.cli('status', identifier, '--wait', code=7)
                stale = self.cli('cancel', identifier, '--expected-revision', started['revision'], code=4)
                self.assertEqual(stale['code'], 'prompt:revision_conflict')
                receipt = paused['cli']['request']
                replay = self.cli('pause', identifier, '--expected-revision', receipt['body']['expected_revision'],
                                  '--instance-id', self.runtime.instance_id, '--idempotency-key', receipt['idempotency_key'])
                self.assertEqual(replay['revision'], paused['revision'])
                running = self.cli('resume', identifier, '--detach')
                self.assertEqual(running['status'], 'running')
                self.assertEqual(self.cli('cancel', identifier)['status'], 'canceled')
            finally:
                release.set()
        self.runtime.supervisor.pool.shutdown(wait=True)
        self.assertIsNone(self.service.load(identifier)['result'])
        self.cli('resume', identifier, code=4)

    def test_resume_wait_completes_using_frozen_input(self):
        preview = self.preview_cli()
        entered, release, gate = self.hold()
        with gate:
            try:
                started = self.run_cli(preview, '--detach')
                self.assertTrue(entered.wait(2))
                self.cli('pause', started['task_id'])
            finally:
                release.set()
            done = self.cli('resume', started['task_id'])
        self.assertTrue(done['ready'])

    def test_live_results_cursor_becomes_stale_instead_of_silently_skipping(self):
        self.run_cli(self.preview_cli())
        self.run_cli(self.preview_cli())
        page = self.cli('list', '--limit', '1')
        self.assertTrue(page['has_more'])
        self.run_cli(self.preview_cli())
        value = self.cli('list', '--limit', '1', '--cursor', page['next_cursor'], code=4)
        self.assertEqual(value['code'], 'page:stale')

    def test_deleted_task_does_not_recreate_from_old_plan(self):
        preview = self.preview_cli()
        done = self.run_cli(preview)
        self.request('POST', '/task-cleanup', {'tasks': [{'task_id': done['task_id'], 'expected_revision': done['revision']}]})
        self.run_cli(preview, code=4)
        value = self.cli('status', done['task_id'], code=4)
        self.assertEqual(value['code'], 'prompt:task_deleted')
        self.assertEqual(self.cli('list')['items'], [])
