import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.project_db.validation import digest
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import PipeServer
from .support import TEMP_ROOT
from .test_api_job_batches import BatchAPIBase

CLIENT = httpx.Client
PID, INSTANCE, BATCH, KEY = (str(uuid4()) for _ in range(4))
VERSION = 'a'*64
CREDENTIALS = {'authority': '127.0.0.1:8301', 'token': 'private-test-token', 'instance_id': INSTANCE, 'protocol_version': 1}
P = '/api/projects/'+PID+'/jobs'


def invoke(*args):
    output, progress = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
        code = main(['job', *map(str, args)])
    return code, json.loads(output.getvalue()), progress.getvalue()


def estimate():
    return {'scope_digest': 'b'*64, 'estimate_only': True, 'revalidates_on_start': True, 'image_count': 1,
            'overlapping_jobs': [], 'items': [{'executor': 'llm_vision', 'total': 1, 'cache_hit': 0,
                'needs_execution': 1, 'file_missing': 0, 'waiting_for_wd': 0, 'role_empty': 0, 'rating_unresolved': 0}]}


def job(state='done', **extra):
    return {'job_id': 1, 'task': {'kind': 'job', 'id': 1, 'project_id': PID}, 'batch_id': BATCH,
            'executor': 'llm_vision', 'status': state, 'version': VERSION, 'total': 1,
            'done_count': int(state == 'done'), 'failed_count': 0, 'skipped_count': 0, **extra}


def batch(state='done', **extra):
    return {'batch_id': BATCH, 'status': state, 'version': VERSION, 'total': 1,
            'done_count': int(state == 'done'), 'failed_count': 0, 'skipped_count': 0,
            'items': [job(state)], **extra}


def accepted():
    return httpx.Response(202, json={'batch_id': BATCH, 'batch_url': P+'/batches/'+BATCH,
        'items': [job()], 'task': job()['task'], 'status_url': P+'/1'})


class JobCLITransportTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def save(self, value, name='preview.json'):
        path = self.base/name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def plan(self):
        from stylelora.runtime.job_cli import validate_body
        body = validate_body({'executors': ['llm_vision'], 'mode': 'uncovered', 'scope': {'filter': {}}, 'config': {}})
        value = {'schema_version': 1, 'project_id': PID, 'instance_id': INSTANCE, 'idempotency_key': KEY,
                 'body': {**body, 'batch_id': BATCH, 'preview_scope_digest': 'b'*64}, 'preview': estimate()}
        return {'plan': value, 'confirmation_digest': digest(value)}

    def run_cli(self, args, handler, *, sleep=None):
        self.requests, self.options = [], {}
        def handle(request):
            self.requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer private-test-token')
            if request.url.path == '/api/projects/current':
                return httpx.Response(200, json={'project_id': PID})
            return handler(request)
        def factory(**kwargs):
            self.options.update(kwargs)
            return CLIENT(**kwargs, transport=httpx.MockTransport(handle))
        with patch('stylelora.runtime.attached_cli.pipe_request', return_value=CREDENTIALS), \
                patch('stylelora.runtime.attached_cli.httpx.Client', side_effect=factory), \
                patch('stylelora.runtime.job_cli.time.sleep', side_effect=sleep):
            result = invoke(*args)
        self.assertNotIn(CREDENTIALS['token'], json.dumps(result))
        if self.options:
            self.assertFalse(self.options['trust_env'])
            self.assertFalse(self.options['follow_redirects'])
        return result

    def run_args(self, document=None, *extra):
        document = self.plan() if document is None else document
        return ['run', '--preview', self.save(document), '--confirm-digest', document['confirmation_digest'], *extra]

    def test_preview_returns_confirmable_fixed_plan_without_creating_jobs(self):
        code, value, _ = self.run_cli(['preview', '--executor', 'llm_vision', '--mode', 'all'], lambda r: httpx.Response(200, json=estimate()))
        self.assertEqual(code, 0)
        self.assertEqual(value['confirmation_digest'], digest(value['plan']))
        self.assertEqual(value['plan']['instance_id'], INSTANCE)
        self.assertEqual(value['plan']['project_id'], PID)
        self.assertEqual(value['plan']['body']['mode'], 'all')
        self.assertEqual(value['plan']['body']['preview_scope_digest'], estimate()['scope_digest'])
        self.assertEqual([r.url.path for r in self.requests], ['/api/projects/current', P+'/preview'])

    def test_run_waits_on_batch_instead_of_ending_after_first_job(self):
        polls = []
        def handle(request):
            if request.method == 'POST':
                return accepted()
            polls.append(request.url.path)
            return httpx.Response(200, json=batch('running' if len(polls) == 1 else 'done'))
        code, value, progress = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['status']), (0, 'done'))
        self.assertEqual(polls, [P+'/batches/'+BATCH]*2)
        self.assertEqual(self.requests[1].headers['Idempotency-Key'], KEY)
        self.assertEqual(value['cli']['batch_id'], BATCH)
        self.assertIn(BATCH, progress)

    def test_detach_accepts_without_polling_and_retains_exact_request(self):
        document = self.plan()
        code, value, _ = self.run_cli(self.run_args(document, '--detach'), lambda r: accepted())
        self.assertEqual((code, value['status']), (0, 'accepted'))
        self.assertEqual(value['cli']['request']['body'], document['plan']['body'])
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])

    def test_unknown_create_keeps_batch_key_and_replay_does_not_repreview(self):
        def fail(request):
            raise httpx.ReadTimeout('private-test-token')
        code, value, _ = self.run_cli(self.run_args(), fail)
        self.assertEqual((code, value['outcome'], value['cli']['batch_id']), (7, 'unknown', BATCH))
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])
        original = value['cli']['request']
        code, replay, _ = self.run_cli(self.run_args(None, '--detach'), lambda r: accepted())
        self.assertEqual(code, 0)
        self.assertEqual(original, replay['cli']['request'])

    def test_plan_modification_or_wrong_digest_stops_before_attachment(self):
        for modify in (lambda d: d['plan']['body'].update(mode='all'),
                       lambda d: d['plan']['preview'].update(image_count=2),
                       lambda d: d.update(confirmation_digest='0'*64)):
            document = self.plan()
            modify(document)
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke(*self.run_args(document))
            self.assertEqual((code, value['code']), (2, 'job:invalid_plan'))
            pipe.assert_not_called()

    def test_semantically_invalid_plan_rejected_even_with_recomputed_digest(self):
        for change in (lambda p: p['body']['config'].update(token='secret'),
                       lambda p: p.update(schema_version=True),
                       lambda p: p['body'].update(preview_scope_digest='c'*64),
                       lambda p: p.update(path='C:/private')):
            document = self.plan()
            change(document['plan'])
            document['confirmation_digest'] = digest(document['plan'])
            code, value, _ = self.run_cli(self.run_args(document), lambda r: self.fail('No HTTP'))
            self.assertEqual((code, value['code']), (2, 'job:invalid_plan'))
            self.assertEqual(self.requests, [])

    def test_old_instance_or_wrong_project_plan_cannot_submit(self):
        for field, expected_requests in (('instance_id', 0), ('project_id', 1)):
            document = self.plan()
            document['plan'][field] = str(uuid4())
            document['confirmation_digest'] = digest(document['plan'])
            code, _, _ = self.run_cli(self.run_args(document), lambda r: self.fail('No POST'))
            self.assertEqual(code, 4)
            self.assertEqual(len(self.requests), expected_requests)

    def test_control_captures_version_and_conflict_does_not_retry(self):
        def handle(request):
            if request.method == 'GET':
                return httpx.Response(200, json=job('running'))
            return httpx.Response(409, json={'error': {'code': 'concurrency:version_conflict', 'outcome': 'rejected'}})
        code, value, _ = self.run_cli(['pause', '1', '--project-id', PID], handle)
        self.assertEqual(code, 4)
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        self.assertEqual(self.requests[-1].headers['If-Match'], '"'+VERSION+'"')
        self.assertEqual([r.method for r in self.requests], ['GET', 'GET', 'POST'])

    def test_control_replay_uses_original_version_without_recapture(self):
        code, value, _ = self.run_cli(['cancel', '--batch-id', BATCH, '--project-id', PID,
                                      '--if-match', VERSION, '--instance-id', INSTANCE, '--idempotency-key', KEY],
                                     lambda r: httpx.Response(200, json=batch('canceled')))
        self.assertEqual(code, 0)
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])
        self.assertEqual(value['cli']['request']['body'], {'action': 'cancel'})
        self.assertEqual(value['cli']['request']['idempotency_key'], KEY)

    def test_resume_wait_and_detach_have_explicit_completion_semantics(self):
        def handle(request):
            return httpx.Response(200, json=job('running' if request.method == 'POST' else 'done'))
        for extra, requests in (([], 4), (['--detach'], 3)):
            code, _, _ = self.run_cli(['resume', '1', '--project-id', PID, *extra], handle)
            self.assertEqual(code, 0)
            self.assertEqual(len(self.requests), requests)

    def test_status_reports_partial_paused_blocked_and_runtime_wait(self):
        for row, expected in ((batch('done', failed_count=1), 6), (batch('paused'), 7), (batch('blocked'), 7),
                              (batch('canceled'), 7), (batch('failed'), 5), (batch('pending', reason='model:unavailable'), 7),
                              (batch('pending', items=[job('pending', runtime_error='model:unavailable')]), 7)):
            code, _, _ = self.run_cli(['status', '--batch-id', BATCH, '--project-id', PID, '--wait'], lambda r: httpx.Response(200, json=row))
            self.assertEqual(code, expected)
            self.assertEqual([r.method for r in self.requests], ['GET', 'GET'])

    def test_poll_failure_and_interrupt_keep_identity_without_cancel(self):
        def handle(request):
            if request.method == 'POST':
                return accepted()
            raise httpx.ReadError('private-test-token')
        code, value, _ = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['cli']['batch_id']), (5, BATCH))
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST', 'GET'])
        code, value, _ = self.run_cli(['status', '--batch-id', BATCH, '--project-id', PID, '--wait'],
                                     lambda r: httpx.Response(200, json=batch('running')), sleep=KeyboardInterrupt())
        self.assertEqual((code, value['cli']['batch_id']), (130, BATCH))
        self.assertEqual([r.method for r in self.requests], ['GET', 'GET'])

    def test_interruption_during_post_retains_original_batch_and_key(self):
        def handle(request):
            raise KeyboardInterrupt()
        code, value, _ = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['cli']['batch_id'], value['cli']['request']['idempotency_key']), (130, BATCH, KEY))

    def test_malformed_accepted_response_is_unknown_and_never_follows_url(self):
        for response in (httpx.Response(202, json={}), httpx.Response(307, headers={'Location': 'http://remote.invalid'}),
                         httpx.Response(200, text='private-test-token'), httpx.Response(500, json={'error': {}}),
                         httpx.Response(202, json={**json.loads(accepted().content), 'batch_url': 'http://remote.invalid'})):
            code, value, _ = self.run_cli(self.run_args(), lambda r: response)
            self.assertEqual((code, value['outcome']), (7, 'unknown'))
            self.assertEqual(len(self.requests), 2)

    def test_status_rejects_wrong_job_identity_and_invalid_counts(self):
        for change in ({'job_id': 2}, {'version': None}, {'failed_count': -1}, {'status': ['running']},
                       {'task': {'kind': 'job', 'id': 1, 'project_id': str(uuid4())}}):
            code, _, _ = self.run_cli(['status', '1', '--project-id', PID], lambda r: httpx.Response(200, json=job(**change)))
            self.assertEqual(code, 5)
        for items in (None, [], ['invalid']):
            code, _, _ = self.run_cli(['status', '--batch-id', BATCH, '--project-id', PID, '--wait'],
                                     lambda r: httpx.Response(200, json=batch('pending', items=items)))
            self.assertEqual(code, 5)

    def test_lists_and_outcomes_encode_cursor_without_scanning_or_post(self):
        for args, path in ((['list', '--limit', '1', '--cursor', 'a&b=?'], P+'/batches'),
                           (['outcomes', '1', '--project-id', PID, '--kind', 'failed', '--cursor', 'a&b=?'], P+'/1/outcomes')):
            code, _, _ = self.run_cli(args, lambda r: httpx.Response(200, json={'items': [], 'has_more': False, 'next_cursor': None}))
            self.assertEqual(code, 0)
            self.assertEqual(self.requests[-1].url.path, path)
            self.assertEqual(self.requests[-1].url.params['cursor'], 'a&b=?')
            self.assertEqual([r.method for r in self.requests], ['GET', 'GET'])

    def test_missing_page_shape_is_not_reported_as_success(self):
        code, value, _ = self.run_cli(['list'], lambda r: httpx.Response(200, json={}))
        self.assertEqual((code, value['code']), (5, 'request:invalid_response'))

    def test_transport_setup_failure_is_safe_json(self):
        with patch('stylelora.runtime.attached_cli.pipe_request', return_value=CREDENTIALS), \
                patch('stylelora.runtime.attached_cli.httpx.Client', side_effect=OSError('private-test-token')):
            code, value, _ = invoke('list')
        self.assertEqual(code, 5)
        self.assertNotIn('private-test-token', json.dumps(value))

    def test_secret_unknown_duplicate_nonfinite_and_oversize_files_rejected_locally(self):
        path = self.base/'config.json'
        for data in (b'{"token":"private-test-token"}', b'{"max_tokens":1024,"max_tokens":2048}',
                     b'{"timeout_s":NaN}', b'[]', b' '*1048577, b'\xff'):
            path.write_bytes(data)
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke('preview', '--executor', 'llm_vision', '--config', path)
            self.assertEqual(code, 2)
            self.assertNotIn('private-test-token', json.dumps(value))
            pipe.assert_not_called()

    def test_filter_and_selected_config_are_normalized_before_preview(self):
        config = self.save({'max_tokens': 512}, 'config.json')
        query = self.save({'review_statuses': ['keep'], 'q': 'red'}, 'filter.json')
        code, value, _ = self.run_cli(['preview', '--executor', 'llm_vision', '--filter', query, '--config', config],
                                     lambda r: httpx.Response(200, json=estimate()))
        self.assertEqual(code, 0)
        self.assertEqual(value['plan']['body']['scope']['filter']['q'], 'red')
        self.assertEqual(value['plan']['body']['config']['max_tokens'], 512)
        code, value, _ = self.run_cli(['preview', '--executor', 'llm_vision', '--mode', 'selected', '--ids', '1'],
                                     lambda r: httpx.Response(200, json=estimate()))
        self.assertEqual(code, 0)
        self.assertEqual(value['plan']['body']['scope']['selected_ids'], [1])

    def test_invalid_arguments_fail_before_attachment(self):
        cases = [[], ['preview'], ['preview', '--executor', 'wd'], ['preview', '--executor', 'wd,wd'],
                 ['preview', '--executor', 'llm_vision', '--mode', 'selected'],
                 ['preview', '--executor', 'llm_vision', '--ids', '1'],
                 ['preview', '--executor', 'llm_vision', '--mode', 'selected', '--ids', '1', '1'],
                 ['run', '--preview', 'missing.json'], ['run', '--preview', 'a', '--confirm-digest', 'bad'],
                 ['status', '1'], ['status', '--project-id', PID], ['pause', '1', '--batch-id', BATCH, '--project-id', PID],
                 ['resume', '1', '--project-id', PID, '--idempotency-key', KEY],
                 ['outcomes', '--project-id', PID], ['outcomes', '0', '--project-id', PID],
                 ['list', '--limit', '201'], ['list', '--cursor', ''], ['list', '--standalone']]
        with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
            for args in cases:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(['job', *args])
                self.assertEqual(error.exception.code, 2)
            pipe.assert_not_called()

    def test_missing_or_invalid_instance_cannot_start_service(self):
        for credentials in ({**CREDENTIALS, 'authority': 'remote.invalid:80'}, {**CREDENTIALS, 'token': '\n'}):
            with patch('stylelora.runtime.attached_cli.pipe_request', return_value=credentials), \
                    patch('stylelora.runtime.attached_cli.httpx.Client') as client:
                code, value, _ = invoke('list')
            self.assertEqual((code, value['code']), (3, 'instance:unavailable'))
            client.assert_not_called()
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=ServiceError(503, 'instance:not_running')):
            code, _, _ = invoke('list')
        self.assertEqual(code, 3)


class JobCLIAPITests(BatchAPIBase):
    def cli(self, *args, code=0):
        server = PipeServer.__new__(PipeServer)
        server.runtime = self.runtime
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=server._dispatch), \
                patch('stylelora.runtime.attached_cli.httpx.Client', return_value=contextlib.nullcontext(self.client)):
            actual, value, progress = invoke(*args)
        self.assertEqual(actual, code, value)
        self.assertNotIn('vision-test-secret', json.dumps(value)+progress)
        self.assertNotIn(self.runtime.token, json.dumps(value)+progress)
        return value

    def preview_cli(self, executors='wd,llm_vision', *, mode='uncovered', ids=None):
        config = self.wd_config if executors == 'wd' else {} if executors == 'llm_vision' else {'wd': self.wd_config, 'llm_vision': {}}
        path = self.base/'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        return self.cli('preview', '--executor', executors, '--mode', mode, '--config', path,
                        *(['--ids', *map(str, ids)] if ids is not None else []))

    def run_cli(self, preview, *extra, code=0):
        path = self.base/'preview.json'
        path.write_text(json.dumps(preview), encoding='utf-8')
        return self.cli('run', '--preview', path, '--confirm-digest', preview['confirmation_digest'], *extra, code=code)

    def test_combined_preview_run_waits_for_both_stages_and_outcomes(self):
        self.import_inputs(2)
        preview = self.preview_cli()
        self.assertEqual((len(self.calls), len(self.wd_calls)), (0, 0))
        self.assertEqual(self.request('GET', self.p+'/jobs')['items'], [])
        result = self.run_cli(preview)
        self.assertEqual((result['status'], result['done_count']), ('done', 4))
        self.assertEqual([r['executor'] for r in result['items']], ['wd', 'llm_vision'])
        listed = self.cli('list')
        self.assertEqual(listed['items'][0]['batch_id'], result['batch_id'])
        outcomes = self.cli('outcomes', result['items'][0]['job_id'], '--project-id', self.pid, '--kind', 'done', '--limit', '1')
        self.assertEqual(len(outcomes['items']), 1)
        self.assertTrue(outcomes['has_more'])

    def test_same_plan_reconciles_existing_batch_without_new_inference(self):
        self.import_inputs()
        preview = self.preview_cli('wd')
        first = self.run_cli(preview)
        replay = self.run_cli(preview)
        self.assertEqual(first['batch_id'], replay['batch_id'])
        self.assertEqual(len(self.wd_calls), 1)
        self.assertEqual(len(self.cli('list')['items']), 1)

    def test_lost_acceptance_replay_recovers_original_persistent_batch(self):
        self.import_inputs()
        preview = self.preview_cli('llm_vision')
        original = self.client.request
        def request(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if method == 'POST' and path.endswith('/jobs/batches'):
                raise httpx.ReadTimeout('response lost')
            return result
        with patch.object(self.client, 'request', side_effect=request):
            unknown = self.run_cli(preview, code=7)
        self.assertEqual(unknown['cli']['batch_id'], preview['plan']['body']['batch_id'])
        replay = self.run_cli(preview)
        self.assertEqual(replay['done_count'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_member_change_stales_preview_without_creating_jobs(self):
        self.import_inputs()
        preview = self.preview_cli('wd')
        self.import_inputs(2)
        result = self.run_cli(preview, code=4)
        self.assertEqual(result['code'], 'job:preview_stale')
        self.assertEqual(self.request('GET', self.p+'/jobs')['items'], [])
        self.assertEqual(self.wd_calls, [])

    def test_selected_scope_excludes_later_import_and_mode_all_gets_new_batch(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        preview = self.preview_cli('wd', mode='selected', ids=[image_id])
        self.import_inputs(2)
        first = self.run_cli(preview)
        self.assertEqual(first['total'], 1)
        all_preview = self.preview_cli('wd', mode='all')
        second = self.run_cli(all_preview)
        self.assertNotEqual(second['batch_id'], first['batch_id'])
        self.assertEqual(second['done_count'], 2)

    def test_batch_pause_tail_does_not_start_vision_until_resume(self):
        self.import_inputs()
        entered, release = self.wd_gate()
        accepted = self.run_cli(self.preview_cli(), '--detach')
        self.assertTrue(entered.wait(5))
        bid = accepted['batch_id']
        paused = self.cli('pause', '--batch-id', bid, '--project-id', self.pid)
        self.assertEqual(paused['status'], 'paused')
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        self.assertEqual(self.calls, [])
        row = self.cli('status', '--batch-id', bid, '--project-id', self.pid, '--wait', code=7)
        self.assertEqual(row['status'], 'paused')
        result = self.cli('resume', '--batch-id', bid, '--project-id', self.pid)
        self.assertEqual(result['done_count'], 2)

    def test_single_job_cancel_discards_late_result_and_cannot_resume_terminal(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        entered, release = self.wd_gate()
        accepted = self.run_cli(self.preview_cli('wd'), '--detach')
        self.assertTrue(entered.wait(5))
        jid = accepted['items'][0]['job_id']
        canceled = self.cli('cancel', jid, '--project-id', self.pid)
        self.assertEqual(canceled['status'], 'canceled')
        release.set()
        self.wd_idle(jid)
        self.assertIsNone(self.current(image_id, layer='wd'))
        self.cli('resume', jid, '--project-id', self.pid, code=4)
        self.cli('status', jid, '--project-id', self.pid, code=7)

    def test_control_replay_and_stale_version_do_not_repeat_action(self):
        self.import_inputs()
        entered, release = self.wd_gate()
        accepted = self.run_cli(self.preview_cli(), '--detach')
        self.assertTrue(entered.wait(5))
        bid = accepted['batch_id']
        canceled = self.cli('cancel', '--batch-id', bid, '--project-id', self.pid)
        receipt = canceled['cli']['request']
        replay = self.cli('cancel', '--batch-id', bid, '--project-id', self.pid,
                          '--instance-id', self.runtime.instance_id, '--if-match', receipt['if_match'],
                          '--idempotency-key', receipt['idempotency_key'])
        self.assertEqual(replay['version'], canceled['version'])
        self.cli('pause', '--batch-id', bid, '--project-id', self.pid, '--if-match', receipt['if_match'], code=4)
        release.set()

    def test_partial_failures_are_not_reported_as_all_success(self):
        self.import_inputs()
        preview = self.preview_cli('wd')
        row = self.request('GET', self.p+'/images')['items'][0]
        (self.root/'images'/row['filename']).write_bytes(b'changed')
        value = self.run_cli(preview, code=6)
        self.assertEqual((value['status'], value['failed_count']), ('done', 1))

    def test_reopen_requires_new_handle_and_old_plan_cannot_create_again(self):
        self.import_inputs()
        preview = self.preview_cli('wd')
        created = self.run_cli(preview)
        old_pid = self.pid
        self.reopen({'batch_id': created['batch_id']})
        self.run_cli(preview, code=4)
        self.cli('status', '--batch-id', created['batch_id'], '--project-id', old_pid, code=4)
        row = self.cli('status', '--batch-id', created['batch_id'], '--project-id', self.pid)
        self.assertEqual(row['batch_id'], created['batch_id'])
        self.assertEqual(len(self.wd_calls), 1)
