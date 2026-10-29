import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.exchange.errors import ExchangeError
from stylelora.project_db.validation import digest
from stylelora.runtime.instance import PipeServer
from . import api_support, test_exchange_schemes_api as scheme_support, test_generation_handoff as handoff_support
from .support import TEMP_ROOT

CLIENT = httpx.Client
PID, INSTANCE, PREVIEW, OP, EID, KEY = (str(uuid4()) for _ in range(6))
VERSION = 'a'*64
BASE = '/api/projects/'+PID+'/exports'
CREDENTIALS = {'authority': '127.0.0.1:8301', 'token': 'private-test-token', 'instance_id': INSTANCE, 'protocol_version': 1}


def invoke(*args):
    output, progress = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
        code = main(['export', *map(str, args)])
    return code, json.loads(output.getvalue()), progress.getvalue()


def preview(*, hardlink=False, collision=False):
    return {'preview_id': PREVIEW, 'digest': 'b'*64, 'plan_digest': 'c'*64, 'dry_run': True,
            'scheme': {'scheme_id': 1}, 'output_config': {'image_mode': 'hardlink' if hardlink else 'copy'},
            'hardlink_ack_digest': 'c'*64 if hardlink else None,
            'trigger_validation': {'digest': 'd'*64, 'status': 'existing_tag' if collision else 'unverifiable',
                                   'can_override': collision, 'override_digest': 'd'*64 if collision else None},
            'images': [{'image_id': 1, 'status': 'ready'}]}


def view(state='finished', **extra):
    return {'export_id': EID, 'status': state, 'version': VERSION, 'selected': 1, 'exported': int(state == 'finished'),
            'failed': 0, 'skipped': 0, 'image_mode': 'copy', 'operation_id': OP,
            'relative_dir': 'exports/1/run' if state == 'finished' else None,
            'report_url': BASE+'/'+EID+'/report' if state == 'finished' else None, **extra}


def operation(kind='export.preview', state='done', result=None, **extra):
    return {'operation_id': OP, 'project_id': PID, 'kind': kind, 'status': state,
            'result': preview() if result is None else result, 'error': None, **extra}


def accepted_preview():
    return httpx.Response(202, json={'task': {'kind': 'operation', 'id': OP, 'project_id': PID}, 'status_url': '/api/operations/'+OP})


def accepted_export(**extra):
    return httpx.Response(202, json={**view('running'), 'status_url': BASE+'/'+EID, **extra})


def report(**extra):
    return {'schema_version': 1, 'export_id': EID, 'status': 'complete', 'directory': 'exports/1/run',
            'counts': {'selected': 1, 'exported': 1, 'skipped': 0, 'failed': 0},
            'images': [{'image_id': 1, 'status': 'exported'}], 'output_config': {'image_mode': 'copy'}, **extra}


class ExportCLITransportTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def save(self, value, name='plan.json'):
        path = self.base/name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def plan(self, **kw):
        from stylelora.runtime.export_cli import submission
        value = preview(**kw)
        plan = {'schema_version': 1, 'instance_id': INSTANCE, 'project_id': PID, 'idempotency_key': KEY,
                'body': submission(value), 'preview': value}
        return {'plan': plan, 'confirmation_digest': digest(plan)}

    def run_args(self, doc=None, *extra):
        doc = doc or self.plan()
        return ['run', '--preview', self.save(doc), '--confirm-digest', doc['confirmation_digest'], *extra]

    def target(self, command, *extra):
        return [command, EID, '--project-id', PID, *extra]

    def run_cli(self, args, handler, *, sleep=None):
        self.requests = []
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
        with patch('stylelora.runtime.attached_cli.pipe_request', return_value=CREDENTIALS), \
                patch('stylelora.runtime.attached_cli.httpx.Client', side_effect=client), \
                patch('stylelora.runtime.export_cli.time.sleep', side_effect=sleep):
            result = invoke(*args)
        self.assertNotIn(CREDENTIALS['token'], json.dumps(result))
        self.assertNotIn(str(self.base), json.dumps(result))
        return result

    def test_preview_freezes_identity_without_implicitly_acknowledging_warnings(self):
        code, value, _ = self.run_cli(['preview', '--project-id', PID], lambda r: accepted_preview() if r.method == 'POST'
            else httpx.Response(200, json=operation(result=preview(hardlink=True, collision=True))))
        self.assertEqual(code, 0)
        self.assertEqual(value['confirmation_digest'], digest(value['plan']))
        self.assertIsNone(value['plan']['body']['hardlink_ack_digest'])
        self.assertIsNone(value['plan']['body']['trigger_override_digest'])
        self.assertEqual(self.requests[1].url.path, BASE+'/preview')
        self.assertEqual(value['cli']['operation_id'], OP)

    def test_preview_recovery_reads_original_operation_without_submitting(self):
        code, value, _ = self.run_cli(['preview', '--operation-id', OP, '--project-id', PID], lambda r: httpx.Response(200, json=operation()))
        self.assertEqual(code, 0)
        self.assertTrue(all(r.method == 'GET' for r in self.requests))
        self.assertEqual(value['plan']['preview']['preview_id'], PREVIEW)

    def test_specific_acknowledgements_are_required_and_cannot_cross_modes(self):
        doc = self.plan(hardlink=True, collision=True)
        for args in (self.run_args(doc), self.run_args(doc, '--accept-hardlink', 'c'*64),
                     self.run_args(doc, '--accept-trigger-warning', 'd'*64, '--accept-hardlink', 'f'*64),
                     self.run_args(None, '--accept-hardlink', 'c'*64), self.run_args(None, '--accept-trigger-warning', 'd'*64)):
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                self.assertEqual(invoke(*args)[0], 2)
            pipe.assert_not_called()
        args = self.run_args(doc, '--accept-trigger-warning', 'd'*64, '--accept-hardlink', 'c'*64, '--detach')
        code, value, _ = self.run_cli(args, lambda r: accepted_export(image_mode='hardlink'))
        self.assertEqual(code, 0)
        self.assertEqual(value['cli']['request']['body']['hardlink_ack_digest'], 'c'*64)
        self.assertEqual(value['cli']['request']['body']['trigger_override_digest'], 'd'*64)

    def test_acknowledgements_embedded_in_plan_are_not_implicit_consent(self):
        doc = self.plan(hardlink=True)
        doc['plan']['body']['hardlink_ack_digest'] = 'c'*64
        doc['confirmation_digest'] = digest(doc['plan'])
        with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
            code, value, _ = invoke(*self.run_args(doc, '--accept-hardlink', 'c'*64))
        self.assertEqual((code, value['code']), (2, 'export:invalid_plan'))
        pipe.assert_not_called()

    def test_run_waits_for_export_state_and_detach_preserves_actual_state(self):
        polls = []
        def handle(r):
            if r.method == 'POST':
                return accepted_export()
            polls.append(1)
            return httpx.Response(200, json=view('running') if len(polls) == 1 else view())
        code, value, _ = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['status'], len(polls)), (0, 'finished', 2))
        self.assertEqual(value['cli']['export_id'], EID)
        code, value, _ = self.run_cli(self.run_args(None, '--detach'), lambda r: accepted_export())
        self.assertEqual((code, value['status'], value['export_status']), (0, 'accepted', 'running'))
        self.assertEqual(len(self.requests), 2)

    def test_lost_submission_keeps_original_request_for_explicit_replay(self):
        def fail(r):
            raise httpx.ReadTimeout('private-test-token')
        code, value, _ = self.run_cli(self.run_args(), fail)
        self.assertEqual((code, value['outcome']), (7, 'unknown'))
        self.assertEqual(value['cli']['request']['idempotency_key'], KEY)
        self.assertEqual(len(self.requests), 2)
        replay = self.run_cli(self.run_args(None, '--detach'), lambda r: accepted_export())[1]
        self.assertEqual(replay['cli']['request'], value['cli']['request'])

    def test_invalid_accepted_identity_url_or_redirect_is_unknown(self):
        for response in (httpx.Response(302, headers={'Location': 'http://example.invalid'}), httpx.Response(200, json=view()),
                         accepted_export(status_url='http://example.invalid'), accepted_export(operation_id=None),
                         accepted_export(relative_dir='../private'), accepted_export(export_id='invalid')):
            code, value, _ = self.run_cli(self.run_args(), lambda r: response)
            self.assertEqual((code, value['outcome']), (7, 'unknown'))

    def test_invalid_options_files_and_legacy_boolean_ack_rejected_before_attach(self):
        path = self.base/'options.json'
        for raw in ('{"include_json":true,"include_json":false}', '{"token":"secret"}', '{"include_nl":1}',
                    '{"source_ids":[NaN]}', '{"allow_existing_trigger":true}', '[]'):
            path.write_text(raw, encoding='utf-8')
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke('preview', '--options', path, '--project-id', PID)
            self.assertEqual((code, value['code']), (2, 'export:invalid_options'))
            self.assertNotIn('secret', json.dumps(value))
            pipe.assert_not_called()

    def test_tampered_or_semantically_invalid_plan_rejected_locally(self):
        for change in (lambda p: p.update(schema_version=True), lambda p: p['body'].update(scheme_id=2),
                       lambda p: p['preview'].update(hardlink_ack_digest='c'*64), lambda p: p['body'].update(token='secret'),
                       lambda p: p['preview']['trigger_validation'].update(can_override=True), lambda p: p['preview']['images'][0].update(image_id=True)):
            doc = self.plan()
            change(doc['plan'])
            doc['confirmation_digest'] = digest(doc['plan'])
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                self.assertEqual(invoke(*self.run_args(doc))[0], 2)
            pipe.assert_not_called()
        doc = self.plan()
        doc['plan']['preview']['images'] = []
        self.assertEqual(invoke(*self.run_args(doc))[0], 2)

    def test_wrong_instance_or_project_prevents_submission(self):
        for field in ('instance_id', 'project_id'):
            doc = self.plan()
            doc['plan'][field] = str(uuid4())
            doc['confirmation_digest'] = digest(doc['plan'])
            code, _, _ = self.run_cli(self.run_args(doc), lambda r: self.fail('No submission'))
            self.assertEqual(code, 4)

    def test_preview_and_plan_size_limits_are_symmetric(self):
        with patch('stylelora.runtime.prompt_cli.FILE_LIMIT', 100):
            self.assertEqual(invoke(*self.run_args())[0], 2)
        with patch('stylelora.runtime.export_cli.FILE_LIMIT', 100):
            code, value, _ = self.run_cli(['preview', '--project-id', PID], lambda r: accepted_preview() if r.method == 'POST'
                else httpx.Response(200, json=operation()))
        self.assertEqual((code, value['code']), (2, 'export:plan_too_large'))

    def test_partial_preview_and_report_preserve_failure_exit(self):
        p = preview()
        p['images'][0]['status'] = 'failed'
        self.assertEqual(self.run_cli(['preview', '--project-id', PID], lambda r: accepted_preview() if r.method == 'POST'
            else httpx.Response(200, json=operation(result=p)))[0], 6)
        self.assertEqual(self.run_cli(self.target('status'), lambda r: httpx.Response(200, json=view(exported=0, failed=1)))[0], 6)
        self.assertEqual(self.run_cli(self.target('report'), lambda r: httpx.Response(200, json=report(
            counts={'selected': 1, 'exported': 0, 'skipped': 0, 'failed': 1})))[0], 6)

    def test_wait_observes_finished_failure_diagnostic_without_resubmitting(self):
        polls = []
        def handle(r):
            if r.method == 'POST':
                return accepted_export()
            if '/operations/' not in r.url.path:
                return httpx.Response(200, json=view('failed'))
            polls.append(1)
            return httpx.Response(200, json=operation('export.run', 'running' if len(polls) == 1 else 'failed',
                error={'code': 'concurrency:version_conflict', 'outcome': 'rejected'}))
        code, value, _ = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['operation']['error']['code'], len(polls)), (4, 'concurrency:version_conflict', 2))
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    def test_invalid_query_identity_report_path_and_preview_kind_rejected(self):
        for response in (view(export_id=str(uuid4())), view(exported=2), view(relative_dir='exports/../secret'),
                         view(report_url='http://example.invalid')):
            self.assertEqual(self.run_cli(self.target('status'), lambda r: httpx.Response(200, json=response))[0], 5)
        for response in (report(export_id=str(uuid4())), report(directory='C:/private'), report(images=[])):
            self.assertEqual(self.run_cli(self.target('report'), lambda r: httpx.Response(200, json=response))[0], 5)
        self.assertEqual(self.run_cli(['preview', '--operation-id', OP, '--project-id', PID],
            lambda r: httpx.Response(200, json=operation('import.preview')))[0], 5)

    def test_list_is_single_page_and_cleaned_status_is_not_finished(self):
        page = {'items': [view('cleaned')], 'has_more': True, 'next_cursor': 'next'}
        code, value, _ = self.run_cli(['list', '--project-id', PID, '--limit', 1, '--cursor', 'previous'], lambda r: httpx.Response(200, json=page))
        self.assertEqual((code, value['next_cursor'], len(self.requests)), (0, 'next', 2))
        self.assertEqual(self.requests[-1].url.params['cursor'], 'previous')
        self.assertEqual(self.run_cli(self.target('status', '--wait'), lambda r: httpx.Response(200, json=view('cleaned')))[0], 7)

    def test_cancel_waits_for_terminal_and_replay_keeps_original_version(self):
        seen = []
        def handle(r):
            seen.append(r.method)
            return httpx.Response(200, json=view('canceled' if len(seen) == 3 else 'running'))
        code, value, _ = self.run_cli(self.target('cancel'), handle)
        self.assertEqual((code, value['status']), (0, 'canceled'))
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        code, value, _ = self.run_cli(self.target('cancel', '--if-match', VERSION, '--instance-id', INSTANCE, '--idempotency-key', KEY, '--detach'),
            lambda r: httpx.Response(200, json=view('running', version='z'*64)))
        self.assertEqual((code, value['cancel_requested'], value['status']), (0, True, 'running'))
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])

    def test_cancel_losing_publish_race_does_not_claim_canceled(self):
        code, value, _ = self.run_cli(self.target('cancel'), lambda r: httpx.Response(200, json=view()))
        self.assertEqual((code, value['status']), (7, 'finished'))

    def test_ctrl_c_does_not_cancel_accepted_export(self):
        code, value, progress = self.run_cli(self.run_args(), lambda r: accepted_export() if r.method == 'POST'
            else httpx.Response(200, json=view('running')), sleep=KeyboardInterrupt)
        self.assertEqual((code, value['cli']['export_id']), (130, EID))
        self.assertIn('continue', progress)
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    def test_replay_flags_and_recovery_inputs_cannot_be_ambiguous(self):
        for args in (self.target('cancel', '--idempotency-key', KEY), ['preview', '--project-id', PID, '--idempotency-key', KEY],
                     ['preview', '--project-id', PID, '--operation-id', OP, '--scheme', 1]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(['export', *map(str, args)])
            self.assertEqual(error.exception.code, 2)


class ExportCLIAPITests(api_support.APITest):
    catalog = scheme_support.ExchangeSchemeTests.catalog
    create_scheme = scheme_support.ExchangeSchemeTests.create_scheme

    def cli(self, *args, code=0):
        server = PipeServer.__new__(PipeServer)
        server.runtime = self.runtime
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=server._dispatch), \
                patch('stylelora.runtime.attached_cli.httpx.Client', return_value=contextlib.nullcontext(self.client)):
            actual, value, progress = invoke(*args)
        self.assertEqual(actual, code, value)
        self.assertNotIn(self.runtime.token, json.dumps(value)+progress)
        self.assertNotIn(str(self.root), json.dumps(value))
        return value

    def save(self, value, name='plan.json'):
        path = self.base/name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def prepare_inputs(self):
        result = self.import_inputs()
        self.keep([row['image_id'] for row in result['items']])

    def preview_cli(self, *extra, code=0):
        return self.cli('preview', '--project-id', self.pid, *extra, code=code)

    def run_plan(self, doc, *extra, code=0):
        return self.cli('run', '--preview', self.save(doc), '--confirm-digest', doc['confirmation_digest'], *extra, code=code)

    def target(self, command, eid, *extra, code=0):
        return self.cli(command, eid, '--project-id', self.pid, *extra, code=code)

    def test_preview_run_report_and_replay_preserve_files_and_one_export(self):
        self.prepare_inputs()
        original = {p.name: p.read_bytes() for p in self.inputs.iterdir()}
        doc = self.preview_cli('--options', self.save({'include_nl': True}, 'options.json'))
        self.assertFalse(list((self.root/'exports').iterdir()))
        value = self.run_plan(doc)
        eid = value['export_id']
        self.assertEqual((value['status'], value['exported']), ('finished', 1))
        result = self.target('report', eid)
        folder = self.root/result['directory']
        self.assertTrue(next(folder.glob('*.txt')).read_text(encoding='utf-8').endswith('\n\n原始描述。\n'))
        self.assertEqual(next(folder.glob('*.png')).read_bytes(), original['0.png'])
        self.assertEqual(self.run_plan(doc)['export_id'], eid)
        self.assertEqual(len(self.cli('list')['items']), 1)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.inputs.iterdir()})

    def test_trigger_warning_needs_exact_explicit_digest_and_style_conflict_is_blocked(self):
        self.prepare_inputs()
        self.catalog([{'tag': 'http_style'}])
        doc = self.preview_cli()
        self.run_plan(doc, code=2)
        self.assertEqual(self.cli('list')['items'], [])
        ack = doc['plan']['preview']['trigger_validation']['override_digest']
        value = self.run_plan(doc, '--accept-trigger-warning', ack)
        self.assertEqual(self.target('report', value['export_id'])['trigger_override_digest'], ack)
        self.request('POST', self.p+'/sources', {**handoff_support.draft(), 'style_prompt': 'http style illustration'}, status=201)
        blocked = self.preview_cli(code=5)
        self.assertEqual(blocked['error']['code'], 'trigger:style_conflict')
        self.assertNotIn('plan', blocked)

    def test_hardlink_ack_is_separate_and_report_proves_shared_file(self):
        self.prepare_inputs()
        scheme = self.create_scheme(False, output_config_json={'image_mode': 'hardlink'})
        doc = self.preview_cli('--scheme', scheme['scheme_id'])
        self.run_plan(doc, code=2)
        self.assertFalse(list((self.root/'exports').iterdir()))
        ack = doc['plan']['preview']['hardlink_ack_digest']
        done = self.run_plan(doc, '--accept-hardlink', ack)
        result = self.target('report', done['export_id'])
        row = result['images'][0]
        self.assertTrue(os.path.samefile(self.root/'images'/row['filename'], self.root/result['directory']/row['output_path']))
        self.assertEqual(result['hardlink_ack_digest'], ack)
        copied = self.preview_cli()
        self.run_plan(copied, '--accept-hardlink', ack, code=2)

    def test_changed_images_fail_before_publication_and_replay_returns_same_failure(self):
        self.prepare_inputs()
        doc = self.preview_cli()
        self.request('PUT', self.p+'/annotations/1/manual', {'tags': [{'tag': 'changed'}], 'read_set': self.request('GET', self.p+'/annotations/1')['refs']},
                     version=self.request('GET', self.p+'/annotations/1')['version'])
        failed = self.run_plan(doc, code=4)
        self.assertEqual(failed['operation']['error']['code'], 'concurrency:version_conflict')
        self.assertEqual(self.run_plan(doc, code=4)['export_id'], failed['export_id'])
        self.assertFalse(list((self.root/'exports').iterdir()))

    def test_lost_acceptance_replays_without_second_directory(self):
        self.prepare_inputs()
        doc, original = self.preview_cli(), self.client.request
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path == self.p+'/exports':
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            self.run_plan(doc, code=7)
        done = self.run_plan(doc)
        self.assertEqual(len(self.cli('list')['items']), 1)
        self.assertEqual(len(list((self.root/'exports').rglob('export_report.json'))), 1)
        self.assertEqual(self.target('report', done['export_id'])['counts']['exported'], 1)

    def test_lost_preview_replay_and_read_only_recovery_keep_one_operation(self):
        self.prepare_inputs()
        original = self.client.request
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path.endswith('/exports/preview'):
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            unknown = self.preview_cli(code=7)
        info = unknown['cli']
        doc = self.preview_cli('--instance-id', info['instance_id'], '--idempotency-key', info['request']['idempotency_key'])
        recovered = self.preview_cli('--operation-id', doc['cli']['operation_id'])
        self.assertEqual(recovered['plan']['preview'], doc['plan']['preview'])
        self.assertEqual(len([t for t in self.runtime.supervisor.tasks.values() if t.kind == 'export.preview']), 1)
        self.assertEqual(self.cli('list')['items'], [])

    def test_preview_failure_diagnostics_and_runtime_partial_failures_are_preserved(self):
        self.prepare_inputs()
        doc = self.preview_cli()
        with patch('stylelora.exchange.exporter.copy_verified', side_effect=ExchangeError('image:decode')):
            result = self.run_plan(doc, code=6)
        report_value = self.target('report', result['export_id'], code=6)
        self.assertEqual(report_value['counts']['failed'], 1)
        self.assertEqual(report_value['images'][0]['status'], 'failed')

    def test_cancel_waits_for_worker_and_original_request_can_be_replayed(self):
        self.prepare_inputs()
        doc = self.preview_cli()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hold(stage):
            if stage == 'export_after_image_copy':
                entered.set()
                if not release.wait(8):
                    raise AssertionError('gate timed out')
        self.runtime.context.handle._fault_hook = hold
        try:
            accepted_value = self.run_plan(doc, '--detach')
            self.assertTrue(entered.wait(3))
            eid = accepted_value['export_id']
            current = self.target('status', eid, code=7)
            self.target('cancel', eid, '--if-match', 'wrong', '--detach', code=4)
            canceled = self.target('cancel', eid, '--detach')
            self.assertTrue(canceled['cancel_requested'])
            self.assertEqual(canceled['cli']['request']['if_match'], current['version'])
        finally:
            release.set()
            self.runtime.context.handle._fault_hook = None
        self.assertEqual(self.target('status', eid, '--wait', code=7)['status'], 'canceled')
        receipt = canceled['cli']['request']
        self.target('cancel', eid, '--instance-id', canceled['cli']['instance_id'], '--idempotency-key', receipt['idempotency_key'], '--if-match', receipt['if_match'])
        self.target('report', eid, code=4)
        self.assertFalse(list((self.root/'exports').rglob('export_report.json')))
        self.assertTrue(list((self.root/'exports').rglob('.export-state.json')))

    def test_catalog_or_preview_expiry_cannot_be_silently_refreshed(self):
        self.prepare_inputs()
        doc = self.preview_cli()
        self.catalog([{'tag': 'another'}])
        self.run_plan(doc, code=4)
        doc = self.preview_cli()
        self.runtime.previews.invalidate(self.pid)
        self.run_plan(doc, code=4)
        self.assertEqual(self.cli('list')['items'], [])

    def test_report_and_list_are_recovered_from_disk_with_current_project_urls(self):
        self.prepare_inputs()
        done = self.run_plan(self.preview_cli())
        eid = done['export_id']
        before = self.target('report', eid)
        self.runtime.dataset.exchange.exports.clear()
        recovered = self.target('status', eid)
        self.assertIsNone(recovered['operation_id'])
        after = self.target('report', eid)
        self.assertEqual({k: v for k, v in before.items() if k != 'cli'}, {k: v for k, v in after.items() if k != 'cli'})
        self.assertEqual(self.cli('list')['items'][0]['export_id'], eid)
