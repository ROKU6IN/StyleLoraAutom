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

from stylelora.api.contracts import ImportOptionsInput
from stylelora.cli import main
from stylelora.exchange.errors import ExchangeError
from stylelora.project_db.validation import digest
from stylelora.runtime.instance import PipeServer
from . import api_support
from .support import TEMP_ROOT

CLIENT = httpx.Client
PID, INSTANCE, GRANT, PREVIEW, OP, KEY = (str(uuid4()) for _ in range(6))
VERSION = 'a'*64
BASE = '/api/projects/'+PID
CREDENTIALS = {'authority': '127.0.0.1:8301', 'token': 'private-test-token', 'instance_id': INSTANCE, 'protocol_version': 1}


def invoke(*args, group='import'):
    output, progress = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
        code = main([group, *map(str, args)])
    return code, json.loads(output.getvalue()), progress.getvalue()


def preview(**extra):
    return {'preview_id': PREVIEW, 'digest': 'b'*64, 'selected': 1, 'blocking_errors': 0, 'can_import': True,
            'scheme_id': 1, 'options': ImportOptionsInput().model_dump(), 'manifest': {'entries': [{'path': 'one.png'}]}, **extra}


def report(**extra):
    return {'source_id': 1, 'status': 'done', 'selected': 1, 'imported': 1, 'skipped': 0, 'failed': 0,
            'pending': 0, 'unresolved': 0, 'items': [{'item_id': 1, 'status': 'done'}], **extra}


def source(state='paused'):
    return {'source_id': 1, 'type': 'import', 'status': state, 'locked': True, 'version': VERSION,
            'cost_policy': 'ask', 'counts': {'pending': 1, 'sent': 0, 'done': 0, 'failed': 0, 'skipped': 0}}


def operation(kind='import.run', state='done', result=None, **extra):
    return {'operation_id': OP, 'project_id': PID, 'kind': kind, 'status': state,
            'result': report() if result is None else result, 'error': None, **extra}


def accepted():
    return httpx.Response(202, json={'task': {'kind': 'operation', 'id': OP, 'project_id': PID}, 'status_url': '/api/operations/'+OP})


class ImportCLITransportTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def save(self, value, name='plan.json'):
        path = self.base/name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def plan(self):
        plan = {'schema_version': 1, 'instance_id': INSTANCE, 'project_id': PID, 'idempotency_key': KEY,
                'body': {'preview_id': PREVIEW, 'digest': 'b'*64, 'name': 'Local images'}, 'preview': preview()}
        return {'plan': plan, 'confirmation_digest': digest(plan)}

    def run_args(self, document=None, *extra):
        document = document or self.plan()
        return ['run', '--preview', self.save(document), '--confirm-digest', document['confirmation_digest'], *extra]

    def run_cli(self, args, handler, *, group='import', sleep=None, pipe=None):
        self.requests, self.pipe_calls = [], []
        def pipe_call(value):
            self.pipe_calls.append(value)
            if value['action'] == 'hello':
                return CREDENTIALS
            if pipe:
                return pipe(value)
            return {'grant_id': GRANT, 'purpose': 'import_root', 'project_id': PID}
        def handle(request):
            self.requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer '+CREDENTIALS['token'])
            if request.url.path == '/api/projects/current':
                return httpx.Response(200, json={'project_id': PID})
            return handler(request)
        def client(**kw):
            self.assertFalse(kw['trust_env'])
            self.assertFalse(kw['follow_redirects'])
            return CLIENT(**kw, transport=httpx.MockTransport(handle))
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=pipe_call), \
                patch('stylelora.runtime.import_cli.pipe_request', side_effect=pipe_call), \
                patch('stylelora.runtime.attached_cli.httpx.Client', side_effect=client), \
                patch('stylelora.runtime.import_cli.time.sleep', side_effect=sleep):
            result = invoke(*args, group=group)
        self.assertNotIn(CREDENTIALS['token'], json.dumps(result))
        self.assertNotIn(str(self.base), json.dumps(result))
        return result

    def test_preview_grants_path_only_over_pipe_and_preserves_partial_options(self):
        options = self.save({'encoding': 'auto'}, 'options.json')
        code, value, _ = self.run_cli(['preview', self.base, '--options', options, '--name', '中文来源', '--project-id', PID],
            lambda r: accepted() if r.method == 'POST' else httpx.Response(200, json=operation('import.preview', result=preview())))
        self.assertEqual(code, 0)
        self.assertEqual(value['confirmation_digest'], digest(value['plan']))
        self.assertEqual(value['plan']['body']['name'], '中文来源')
        grant = self.pipe_calls[-1]
        self.assertEqual((grant['purpose'], grant['instance_id'], grant['project_id']), ('import_root', INSTANCE, PID))
        self.assertEqual(grant['path'], str(self.base.absolute()))
        self.assertEqual(json.loads(self.requests[1].content), {'root_handle': GRANT, 'scheme_id': None, 'options': {'encoding': 'auto'}})
        self.assertNotIn(str(self.base), str(self.requests[1].content))

    def test_blocked_preview_has_partial_exit_and_cannot_run(self):
        code, value, _ = self.run_cli(['preview', self.base, '--project-id', PID], lambda r: accepted() if r.method == 'POST'
            else httpx.Response(200, json=operation('import.preview', result=preview(can_import=False, blocking_errors=1))))
        self.assertEqual(code, 6)
        code, error, _ = self.run_cli(self.run_args(value), lambda r: self.fail('No import submission'))
        self.assertEqual((code, error['code']), (4, 'import:preview_blocked'))

    def test_run_waits_for_operation_result_not_202_or_running_source(self):
        polls = []
        def handle(r):
            if r.method == 'POST':
                return accepted()
            polls.append(1)
            return httpx.Response(200, json=operation(state='running' if len(polls) == 1 else 'done'))
        code, value, progress = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['result']['imported'], len(polls)), (0, 1, 2))
        self.assertEqual(value['cli']['source_id'], 1)
        self.assertEqual(value['cli']['request']['idempotency_key'], KEY)
        self.assertIn(OP, progress)

    def test_detach_returns_operation_identity_without_completion_claim(self):
        code, value, _ = self.run_cli(self.run_args(None, '--detach'), lambda r: accepted())
        self.assertEqual((code, value['status']), (0, 'accepted'))
        self.assertEqual(value['cli']['operation_id'], OP)
        self.assertNotIn('result', value)
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])

    def test_lost_acceptance_keeps_exact_body_and_key_without_retry(self):
        def fail(r):
            raise httpx.ReadTimeout('private-test-token')
        code, value, _ = self.run_cli(self.run_args(), fail)
        self.assertEqual((code, value['outcome']), (7, 'unknown'))
        self.assertEqual(value['cli']['request']['body'], self.plan()['plan']['body'])
        self.assertEqual(len(self.requests), 2)
        replay = self.run_cli(self.run_args(None, '--detach'), lambda r: accepted())[1]
        self.assertEqual(replay['cli']['request'], value['cli']['request'])

    def test_preview_replay_keeps_original_grant_and_never_reauthorizes(self):
        args = ['preview', '--grant-id', GRANT, '--instance-id', INSTANCE, '--idempotency-key', KEY, '--project-id', PID]
        code, value, _ = self.run_cli(args, lambda r: accepted() if r.method == 'POST' else httpx.Response(200, json=operation('import.preview', result=preview())))
        self.assertEqual(code, 0)
        self.assertEqual([c['action'] for c in self.pipe_calls], ['hello'])
        self.assertEqual(value['cli']['request']['idempotency_key'], KEY)

    def test_wrong_instance_or_project_prevents_submission(self):
        for field in ('instance_id', 'project_id'):
            doc = self.plan()
            doc['plan'][field] = str(uuid4())
            doc['confirmation_digest'] = digest(doc['plan'])
            code, _, _ = self.run_cli(self.run_args(doc), lambda r: self.fail('No submission'))
            self.assertEqual(code, 4)

    def test_bad_grant_cannot_reach_http_preview(self):
        for change in ({'purpose': 'project_open'}, {'project_id': str(uuid4())}, {'grant_id': 'bad'}):
            code, value, _ = self.run_cli(['preview', self.base, '--project-id', PID], lambda r: self.fail('No preview'),
                pipe=lambda v: {'grant_id': GRANT, 'purpose': 'import_root', 'project_id': PID, **change})
            self.assertEqual((code, value['code']), (5, 'instance:invalid_grant'))

    def test_invalid_options_and_files_rejected_before_attachment(self):
        path = self.base/'bad.json'
        for raw in ('{"encoding":"utf-8","encoding":"gbk"}', '{"token":"secret"}', '{"recursive":1}', '{"max_files":NaN}', '[]'):
            path.write_text(raw, encoding='utf-8')
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke('preview', self.base, '--options', path, '--project-id', PID)
            self.assertEqual((code, value['code']), (2, 'import:invalid_options'))
            self.assertNotIn('secret', json.dumps(value))
            pipe.assert_not_called()

    def test_plan_mutation_or_invalid_schema_is_rejected_locally(self):
        for change in (lambda p: p.update(schema_version=True), lambda p: p['body'].update(token='secret'),
                       lambda p: p['body'].update(preview_id=str(uuid4())), lambda p: p['preview'].update(selected=True),
                       lambda p: p['preview']['options'].update(recursive=1), lambda p: p['preview'].update(blocking_errors=1)):
            doc = self.plan()
            change(doc['plan'])
            doc['confirmation_digest'] = digest(doc['plan'])
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke(*self.run_args(doc))
            self.assertEqual((code, value['code']), (2, 'import:invalid_plan'))
            pipe.assert_not_called()
        doc = self.plan()
        doc['plan']['body']['name'] = 'tampered'
        self.assertEqual(invoke(*self.run_args(doc))[0], 2)

    def test_oversize_plan_input_and_output_are_bounded(self):
        with patch('stylelora.runtime.prompt_cli.FILE_LIMIT', 100):
            self.assertEqual(invoke(*self.run_args())[0], 2)
        with patch('stylelora.runtime.import_cli.FILE_LIMIT', 100):
            code, value, _ = self.run_cli(['preview', self.base, '--project-id', PID], lambda r: accepted() if r.method == 'POST'
                else httpx.Response(200, json=operation('import.preview', result=preview())))
        self.assertEqual((code, value['code']), (2, 'import:plan_too_large'))

    def test_invalid_acceptance_or_redirect_reports_unknown(self):
        for response in (httpx.Response(302, headers={'Location': 'http://example.invalid'}), httpx.Response(200, json={}),
                         httpx.Response(202, json={'task': {'kind': 'operation', 'id': OP, 'project_id': str(uuid4())}, 'status_url': '/api/operations/'+OP})):
            code, value, _ = self.run_cli(self.run_args(), lambda r: response)
            self.assertEqual((code, value['outcome']), (7, 'unknown'))

    def test_operation_identity_kind_and_report_counts_are_verified(self):
        for value in (operation(project_id=str(uuid4())), operation(kind='export.run'), operation(operation_id=str(uuid4())),
                      operation(result=report(imported=5)), operation(result=report(source_id=True))):
            code, _, _ = self.run_cli(['status', OP, '--project-id', PID], lambda r: httpx.Response(200, json=value))
            self.assertEqual(code, 5)

    def test_partial_paused_canceled_and_unknown_operations_have_distinct_exits(self):
        for value, expected in ((operation(result=report(imported=0, failed=1)), 6),
                                (operation(result=report(status='paused', imported=0, pending=1)), 7),
                                (operation(state='canceled'), 7),
                                (operation(state='failed', error={'code': 'storage:commit_unknown', 'outcome': 'unknown'}), 7),
                                (operation(state='failed', error={'code': 'concurrency:version_conflict', 'outcome': 'rejected'}), 4)):
            self.assertEqual(self.run_cli(['status', OP, '--project-id', PID], lambda r: httpx.Response(200, json=value))[0], expected)

    def test_ctrl_c_does_not_cancel_background_import(self):
        code, value, progress = self.run_cli(self.run_args(), lambda r: accepted() if r.method == 'POST'
            else httpx.Response(200, json=operation(state='running')), sleep=KeyboardInterrupt)
        self.assertEqual((code, value['cli']['operation_id']), (130, OP))
        self.assertIn('continue', progress)
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    def test_source_resume_accepts_import_operation_and_preserves_cas(self):
        args = ['resume', 1, '--project-id', PID, '--detach']
        code, value, _ = self.run_cli(args, lambda r: accepted() if r.method == 'POST' else httpx.Response(200, json=source()), group='source')
        self.assertEqual((code, value['status']), (0, 'accepted'))
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        self.assertEqual(value['cli']['source_id'], 1)

    def test_source_root_replay_uses_original_grant_and_version(self):
        args = ['import-root', 1, '--project-id', PID, '--grant-id', GRANT, '--instance-id', INSTANCE,
                '--idempotency-key', KEY, '--if-match', VERSION, '--detach']
        code, value, _ = self.run_cli(args, lambda r: accepted() if r.method == 'POST' else httpx.Response(200, json={**source(), 'version': 'z'*64}), group='source')
        self.assertEqual(code, 0)
        self.assertEqual(value['cli']['request']['body'], {'root_handle': GRANT})
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        self.assertEqual([v['action'] for v in self.pipe_calls], ['hello'])

    def test_missing_replay_context_is_rejected(self):
        for group, args in (('import', ['preview', '--grant-id', GRANT, '--project-id', PID]),
                            ('source', ['import-root', 1, '--project-id', PID, '--grant-id', GRANT]),
                            ('import', ['preview', self.base, '--project-id', PID, '--idempotency-key', KEY])):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main([group, *map(str, args)])
            self.assertEqual(error.exception.code, 2)


class ImportCLIAPITests(api_support.APITest):
    def cli(self, *args, group='import', code=0):
        server = PipeServer.__new__(PipeServer)
        server.runtime = self.runtime
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=server._dispatch), \
                patch('stylelora.runtime.import_cli.pipe_request', side_effect=server._dispatch), \
                patch('stylelora.runtime.attached_cli.httpx.Client', return_value=contextlib.nullcontext(self.client)):
            actual, value, progress = invoke(*args, group=group)
        self.assertEqual(actual, code, value)
        self.assertNotIn(self.runtime.token, json.dumps(value)+progress)
        self.assertNotIn(str(self.inputs), json.dumps(value))
        return value

    def source(self, command, sid, *extra, code=0):
        return self.cli(command, sid, '--project-id', self.pid, *extra, group='source', code=code)

    def save(self, value, name='plan.json'):
        path = self.base/name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def preview_cli(self, *extra, code=0):
        return self.cli('preview', self.inputs, '--project-id', self.pid, *extra, code=code)

    def run_plan(self, doc, *extra, code=0):
        return self.cli('run', '--preview', self.save(doc), '--confirm-digest', doc['confirmation_digest'], *extra, code=code)

    def status(self, value, *, code=0):
        return self.cli('status', value['cli']['operation_id'], '--project-id', self.pid, '--wait', code=code)

    def test_preview_run_duplicate_summary_and_inputs_stay_unchanged(self):
        path = self.make_input()
        before = {p.name: p.read_bytes() for p in self.inputs.iterdir()}
        doc = self.preview_cli('--name', 'CLI 导入')
        self.assertEqual(self.request('GET', self.p+'/sources')['items'], [])
        done = self.run_plan(doc)
        self.assertEqual((done['status'], done['result']['imported']), ('done', 1))
        sid = done['result']['source_id']
        self.assertEqual(self.source('status', sid)['status'], 'done')
        self.assertEqual(self.cli('summary', sid, '--project-id', self.pid)['imported'], 1)
        self.assertEqual(self.run_plan(doc)['operation_id'], done['operation_id'])
        duplicate = self.run_plan(self.preview_cli())
        self.assertEqual((duplicate['result']['imported'], duplicate['result']['skipped']), (0, 1))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.inputs.iterdir()})
        self.assertEqual(path.read_bytes(), before[path.name])

    def test_preflight_errors_block_entire_import(self):
        self.make_input(caption='bad\nlayout')
        doc = self.preview_cli(code=6)
        self.assertEqual(doc['plan']['preview']['blocking_errors'], 1)
        self.run_plan(doc, code=4)
        self.assertEqual(self.request('GET', self.p+'/sources')['items'], [])

    def test_scheme_defaults_are_preserved_by_partial_options(self):
        self.make_input()
        options = self.save({'encoding': 'auto'}, 'options.json')
        doc = self.preview_cli('--options', options)
        self.assertEqual(doc['plan']['preview']['options']['encoding'], 'auto')
        self.assertTrue(doc['plan']['preview']['options']['recursive'])
        done = self.run_plan(doc)
        self.assertEqual(done['result']['items'][0]['encoding'], 'utf-8')

    def test_input_changes_fail_before_creating_source_and_replay_does_not_rescan(self):
        path = self.make_input()
        doc = self.preview_cli()
        path.with_suffix('.txt').write_text('changed', encoding='utf-8')
        failed = self.run_plan(doc, code=4)
        self.assertEqual(failed['error']['code'], 'concurrency:version_conflict')
        self.assertEqual(self.run_plan(doc, code=4)['operation_id'], failed['operation_id'])
        self.assertEqual(self.request('GET', self.p+'/sources')['items'], [])

    def test_unknown_preview_replayed_with_same_grant_and_operation(self):
        self.make_input()
        original = self.client.request
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path.endswith('/imports/preview'):
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            unknown = self.preview_cli(code=7)
        info = unknown['cli']
        doc = self.cli('preview', '--grant-id', info['grant_id'], '--idempotency-key', info['request']['idempotency_key'],
                       '--instance-id', info['instance_id'], '--project-id', self.pid)
        tasks = [v for v in self.runtime.supervisor.tasks.values() if v.kind == 'import.preview']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(doc['cli']['operation_id'], tasks[0].operation_id)
        self.assertEqual(self.request('GET', self.p+'/sources')['items'], [])

    def test_unknown_run_replayed_as_same_operation_without_duplicate_source(self):
        self.make_input()
        doc, original = self.preview_cli(), self.client.request
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path.endswith('/imports'):
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            self.run_plan(doc, code=7)
        done = self.run_plan(doc)
        self.assertEqual(done['result']['imported'], 1)
        self.assertEqual(len(self.request('GET', self.p+'/sources')['items']), 1)
        self.assertEqual(len([t for t in self.runtime.supervisor.tasks.values() if t.kind == 'import.run']), 1)

    def hold(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hook(stage):
            if stage == 'import_after_manifest':
                entered.set()
                if not release.wait(8):
                    raise AssertionError('test gate timed out')
        self.runtime.context.handle._fault_hook = hook
        return entered, release

    def paused(self):
        self.make_input()
        doc = self.preview_cli()
        entered, release = self.hold()
        try:
            accepted_value = self.run_plan(doc, '--detach')
            self.assertTrue(entered.wait(3))
            sid = self.request('GET', self.p+'/sources')['items'][0]['source_id']
            paused = self.source('pause', sid)
        finally:
            release.set()
            self.runtime.context.handle._fault_hook = None
        self.status(accepted_value, code=7)
        return sid, paused

    def test_import_pause_resume_and_summary_follow_background_completion(self):
        sid, paused = self.paused()
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(self.cli('summary', sid, '--project-id', self.pid, code=7)['pending'], 1)
        done = self.source('resume', sid)
        self.assertEqual((done['kind'], done['result']['imported']), ('import.resume', 1))
        receipt = done['cli']['request']
        replay = self.source('resume', sid, '--instance-id', done['cli']['instance_id'], '--if-match', receipt['if_match'],
                             '--idempotency-key', receipt['idempotency_key'])
        self.assertEqual(replay['operation_id'], done['operation_id'])

    def test_cancel_stops_import_and_resume_cannot_reopen_it(self):
        sid, _ = self.paused()
        self.assertEqual(self.source('cancel', sid)['status'], 'canceled')
        self.source('resume', sid, code=4)
        self.assertEqual(self.cli('summary', sid, '--project-id', self.pid, code=7)['imported'], 0)

    def test_expired_grant_needs_explicit_root_verification_and_fresh_version(self):
        sid, paused = self.paused()
        old = self.runtime.source_roots[(self.pid, sid)]
        self.runtime.grants._values[old].deadline = 0
        self.source('resume', sid, code=4)
        checked = self.source('import-root', sid, self.inputs)
        self.assertTrue(checked['result']['verified'])
        self.assertNotEqual(self.runtime.source_roots[(self.pid, sid)], old)
        self.source('resume', sid, '--if-match', paused['version'], code=4)
        self.assertEqual(self.source('resume', sid)['result']['imported'], 1)

    def test_changed_root_fails_without_replacing_previous_binding(self):
        sid, _ = self.paused()
        previous = self.runtime.source_roots[(self.pid, sid)]
        (self.inputs/'sample.txt').write_text('changed', encoding='utf-8')
        result = self.source('import-root', sid, self.inputs, code=5)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.runtime.source_roots[(self.pid, sid)], previous)
        self.assertEqual(self.source('status', sid, code=7)['counts']['pending'], 1)

    def test_root_unknown_replay_keeps_one_verification_operation(self):
        sid, _ = self.paused()
        original = self.client.request
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path.endswith('/import-root'):
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            unknown = self.source('import-root', sid, self.inputs, code=7)
        info = unknown['cli']
        value = self.source('import-root', sid, '--grant-id', info['grant_id'], '--instance-id', info['instance_id'],
                            '--if-match', info['request']['if_match'], '--idempotency-key', info['request']['idempotency_key'])
        self.assertTrue(value['result']['verified'])
        self.assertEqual(len([t for t in self.runtime.supervisor.tasks.values() if t.kind == 'import.verify_root']), 1)

    def test_runtime_partial_failure_is_not_mistaken_for_complete_success(self):
        self.make_input()
        doc = self.preview_cli()
        with patch('stylelora.exchange.importer.copy_verified', side_effect=ExchangeError('image:decode')):
            result = self.run_plan(doc, code=6)
        self.assertEqual((result['status'], result['result']['status'], result['result']['failed']), ('done', 'done', 1))
        self.cli('summary', result['result']['source_id'], '--project-id', self.pid, code=6)

    def test_expired_preview_rejected_without_repreview_or_source(self):
        self.make_input()
        doc = self.preview_cli()
        self.runtime.previews.invalidate(self.pid)
        self.run_plan(doc, code=4)
        self.assertEqual(len([t for t in self.runtime.supervisor.tasks.values() if t.kind == 'import.preview']), 1)
        self.assertEqual(self.request('GET', self.p+'/sources')['items'], [])
