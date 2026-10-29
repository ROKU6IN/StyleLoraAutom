import contextlib
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.runtime.errors import ServiceError
from .api_support import APITest

CLIENT = httpx.Client
PID, OP, INSTANCE = (str(uuid4()) for _ in range(3))
CREDENTIALS = {'authority': '127.0.0.1:8301', 'token': 'private-test-token',
               'instance_id': INSTANCE, 'protocol_version': 1}


def invoke(*args):
    output, progress = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
        code = main(['storage', *args])
    return code, json.loads(output.getvalue()), progress.getvalue()


class StorageCLITransportTests(unittest.TestCase):
    def run_cli(self, args, handler, *, sleep=None):
        self.requests, self.options = [], {}
        def handle(request):
            self.requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer private-test-token')
            return handler(request)
        def factory(**kwargs):
            self.options.update(kwargs)
            return CLIENT(**kwargs, transport=httpx.MockTransport(handle))
        with patch('stylelora.runtime.storage_cli.pipe_request', return_value=CREDENTIALS), \
                patch('stylelora.runtime.storage_cli.httpx.Client', side_effect=factory), \
                patch('stylelora.runtime.storage_cli.time.sleep', side_effect=sleep):
            result = invoke(*args)
        self.assertNotIn(CREDENTIALS['token'], json.dumps(result))
        self.assertFalse(self.options['trust_env'])
        self.assertFalse(self.options['follow_redirects'])
        return result

    @staticmethod
    def accepted():
        return httpx.Response(202, json={'task': {'kind': 'operation', 'id': OP, 'project_id': PID},
                                         'status_url': '/api/operations/'+OP})

    @staticmethod
    def operation(status='done', **extra):
        return httpx.Response(200, json={'operation_id': OP, 'project_id': PID, 'kind': 'storage.scan',
                                        'status': status, 'result': None, **extra})

    def test_scan_default_waits_and_emits_single_json_with_receipt(self):
        polls = []
        def handler(request):
            if request.url.path == '/api/session':
                return httpx.Response(200, json={'project_id': PID})
            if request.method == 'POST':
                return self.accepted()
            polls.append(1)
            return self.operation('running') if len(polls) == 1 else self.operation(result={'total_bytes': 42})
        code, value, progress = self.run_cli(['scan', '--json'], handler)
        self.assertEqual((code, value['status'], value['result']['total_bytes']), (0, 'done', 42))
        self.assertEqual(value['cli']['project_id'], PID)
        self.assertEqual(value['cli']['instance_id'], INSTANCE)
        request = value['cli']['request']
        self.assertEqual(request['path'], '/api/projects/'+PID+'/storage/scans')
        self.assertEqual(request['body'], {})
        self.assertEqual(self.requests[1].headers['Idempotency-Key'], request['idempotency_key'])
        self.assertIn(OP, progress)
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST', 'GET', 'GET'])

    def test_detach_returns_acceptance_and_never_polls(self):
        code, value, _ = self.run_cli(['scan', '--known', '--detach'], lambda r: self.accepted())
        self.assertEqual((code, value['status']), (0, 'accepted'))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0].content), {'known_ids': None})

    def test_selected_scan_sends_only_ids_and_explicit_key(self):
        key, first, second = (str(uuid4()) for _ in range(3))
        code, value, _ = self.run_cli(['scan', '--known-id', first, '--known-id', second,
                                      '--idempotency-key', key, '--detach'], lambda r: self.accepted())
        self.assertEqual(code, 0)
        self.assertEqual(value['cli']['request']['body'], {'known_ids': [first, second]})
        self.assertEqual(self.requests[0].headers['Idempotency-Key'], key)

    def test_status_reads_without_writes_and_running_is_not_completed(self):
        code, value, _ = self.run_cli(['status', OP], lambda r: self.operation('running'))
        self.assertEqual(code, 7)
        self.assertEqual(value['cli']['operation_id'], OP)
        self.assertEqual([r.method for r in self.requests], ['GET'])
        self.assertNotIn('Idempotency-Key', self.requests[0].headers)

    def test_interruption_prints_task_identity_without_cancel(self):
        code, value, progress = self.run_cli(['status', OP, '--wait'], lambda r: self.operation('running'), sleep=KeyboardInterrupt())
        self.assertEqual((code, value['cli']['operation_id']), (130, OP))
        self.assertIn('continue', progress)
        self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_unknown_write_keeps_same_request_identity_and_never_retries(self):
        key = str(uuid4())
        def handler(request):
            raise httpx.ReadTimeout('private-test-token', request=request)
        code, value, _ = self.run_cli(['scan', '--known', '--idempotency-key', key], handler)
        self.assertEqual((code, value['outcome']), (7, 'unknown'))
        self.assertEqual(value['cli']['request']['idempotency_key'], key)
        self.assertEqual(len(self.requests), 1)

    def test_interruption_during_post_retains_request_without_new_key(self):
        def handler(request):
            raise KeyboardInterrupt()
        code, value, _ = self.run_cli(['scan', '--known'], handler)
        self.assertEqual(code, 130)
        self.assertIn('idempotency_key', value['cli']['request'])
        self.assertNotIn('operation_id', value['cli'])
        self.assertEqual(len(self.requests), 1)

    def test_poll_failure_preserves_task_and_does_not_resubmit(self):
        def handler(request):
            if request.method == 'POST':
                return self.accepted()
            raise httpx.ReadError('private-test-token')
        code, value, _ = self.run_cli(['scan', '--known'], handler)
        self.assertEqual(code, 5)
        self.assertEqual(value['cli']['operation_id'], OP)
        self.assertEqual([r.method for r in self.requests], ['POST', 'GET'])

    def test_http_error_exit_codes_preserve_server_reason(self):
        for status, expected in ((422, 2), (401, 3), (403, 3), (409, 4), (410, 4), (423, 4), (428, 4), (429, 5), (503, 5)):
            with self.subTest(status=status):
                code, value, _ = self.run_cli(['summary'], lambda r: httpx.Response(status, json={'error': {'code': 'test:error', 'outcome': 'rejected'}}))
                self.assertEqual((code, value['code']), (expected, 'test:error'))
        code, _, _ = self.run_cli(['scan', '--known'], lambda r: httpx.Response(503, json={'error': {'code': 'storage:commit_unknown', 'outcome': 'unknown'}}))
        self.assertEqual(code, 7)

    def test_terminal_partial_conflict_failure_and_unknown_exit_codes(self):
        for fields, expected in (({'result': {'partial': True}}, 6), ({'result': {'failed': 1}}, 6),
                                 ({'result': {'exceeds_threshold': True}}, 0),
                                 ({'status': 'canceled'}, 7), ({'status': 'interrupted'}, 7),
                                 ({'status': 'failed', 'error': {'code': 'cleanup:preview_stale'}}, 4),
                                 ({'status': 'failed', 'error': {'code': 'storage:leased'}}, 4),
                                 ({'status': 'failed', 'error': {'code': 'internal:error', 'outcome': 'unknown'}}, 7),
                                 ({'status': 'failed', 'error': {'code': 'cleanup:file_failed'}}, 5)):
            with self.subTest(fields=fields):
                code, _, _ = self.run_cli(['status', OP], lambda r: self.operation(**fields))
                self.assertEqual(code, expected)

    def test_results_cursor_is_encoded_and_sort_bound_without_scanning(self):
        code, value, _ = self.run_cli(['results', OP, '--limit', '1', '--cursor', 'a&x=?', '--sort', 'known_id'],
                                     lambda r: httpx.Response(200, json={'items': [], 'summary': {'partial': True}}))
        self.assertEqual(code, 6)
        self.assertEqual(dict(self.requests[0].url.params), {'limit': '1', 'cursor': 'a&x=?', 'sort': 'known_id'})
        self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_wrong_project_and_no_project_stop_before_post(self):
        for pid in (None, str(uuid4())):
            code, _, _ = self.run_cli(['scan', '--project-id', PID], lambda r: httpx.Response(200, json={'project_id': pid}))
            self.assertEqual(code, 4)
            self.assertEqual([r.method for r in self.requests], ['GET'])

    def test_confirmation_never_creates_new_preview(self):
        preview_id = str(uuid4())
        def handler(request):
            return httpx.Response(200, json={'project_id': PID}) if request.method == 'GET' else self.accepted()
        code, value, _ = self.run_cli(['cleanup', '--project-id', PID, '--preview-id', preview_id,
                                      '--confirm-digest', 'a'*64, '--detach'], handler)
        self.assertEqual(code, 0)
        self.assertEqual(value['cli']['request']['body'], {'preview_id': preview_id, 'digest': 'a'*64, 'confirm': True})
        self.assertEqual(self.requests[-1].url.path, '/api/projects/'+PID+'/storage/cleanup')
        self.assertEqual(len(self.requests), 2)

    def test_invalid_combinations_and_paths_fail_before_attachment(self):
        cases = [[], ['cleanup'], ['scan', '--project', 'C:/private'], ['cleanup', '--kind', 'database'],
                 ['cleanup', '--confirm-digest', 'a'*64], ['cleanup', '--preview-id', OP],
                 ['cleanup', '--preview-id', OP, '--confirm-digest', 'a'*64],
                 ['cleanup', '--kind', 'thumbnails', '--project-id', PID, '--preview-id', OP, '--confirm-digest', 'a'*64],
                 ['scan', '--known', '--project-id', PID], ['scan', '--known', '--known-id', PID],
                 ['scan', '--known-id', PID, '--known-id', PID], ['scan', '--wait', '--detach'],
                 ['scan', '--idempotency-key', 'token'], ['scan', '--project-id', '../escape'],
                 ['status', 'https://example.com'], ['results', OP, '--limit', '201'],
                 ['results', OP, '--limit', '0'], ['results', OP, '--cursor', ''],
                 ['results', OP, '--sort', 'name'], ['cleanup', '--known'],
                 ['cleanup', '--project-id', PID, '--preview-id', OP, '--confirm-digest', 'A'*64]]
        with patch('stylelora.runtime.storage_cli.pipe_request') as pipe:
            for args in cases:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(['storage', *args])
                self.assertEqual(error.exception.code, 2)
            pipe.assert_not_called()

    def test_missing_instance_is_json_exit_three_without_starting_server(self):
        with patch('stylelora.runtime.storage_cli.pipe_request', side_effect=ServiceError(503, 'instance:not_running')), \
                patch('stylelora.runtime.storage_cli.httpx.Client') as client:
            code, value, _ = invoke('summary')
        self.assertEqual((code, value['code']), (3, 'instance:unavailable'))
        client.assert_not_called()

    def test_redirect_or_non_json_write_is_unknown_without_following(self):
        for response in (httpx.Response(307, headers={'Location': 'http://remote.invalid/'}),
                         httpx.Response(200, text='private-test-token'), httpx.Response(202, json={'task': {}})):
            code, value, _ = self.run_cli(['scan', '--known'], lambda r: response)
            self.assertEqual((code, value['outcome']), (7, 'unknown'))
            self.assertEqual(len(self.requests), 1)

    def test_status_rejects_unrelated_or_mismatched_operations(self):
        for change in ({'kind': 'job.run'}, {'operation_id': str(uuid4())}):
            code, _, _ = self.run_cli(['status', OP], lambda r: self.operation(**change))
            self.assertEqual(code, 2)


class StorageCLIAPITests(APITest):
    def cli(self, *args, code=0):
        credentials = {**CREDENTIALS, 'token': self.runtime.token, 'instance_id': self.runtime.instance_id}
        with patch('stylelora.runtime.storage_cli.pipe_request', return_value=credentials), \
                patch('stylelora.runtime.storage_cli.httpx.Client', return_value=contextlib.nullcontext(self.client)):
            actual, value, progress = invoke(*args)
        self.assertEqual(actual, code, value)
        self.assertNotIn(self.runtime.token, json.dumps(value)+progress)
        return value

    def thumbnail(self, letter='a'):
        path = self.root/'cache/thumbnails'/(letter*64+'.webp')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
        return path

    def preview(self, kind='thumbnails'):
        return self.cli('cleanup', '--kind', kind)

    def confirm(self, preview, **kwargs):
        value = preview['result']
        return self.cli('cleanup', '--project-id', preview['project_id'], '--preview-id', value['preview_id'],
                        '--confirm-digest', value['digest'], **kwargs)

    def test_summary_is_read_only_and_scan_matches_http_report(self):
        with patch('stylelora.services.storage.inventory', side_effect=AssertionError('no scan')):
            value = self.cli('summary')
            self.assertIsNone(value['current_project'])
            self.assertIsNone(self.cli('summary', '--project-id', self.pid)['scan'])
        self.thumbnail()
        scan = self.cli('scan')
        self.assertEqual(scan['result']['categories']['cache'], 7)
        self.assertEqual(self.cli('summary', '--project-id', self.pid)['scan'], scan['result'])

    def test_cleanup_preview_then_confirmation_and_receipt_replay(self):
        target = self.thumbnail()
        preview = self.preview()
        self.assertTrue(target.exists())
        self.assertEqual(preview['result']['count'], 1)
        result = self.confirm(preview)
        self.assertFalse(target.exists())
        self.assertEqual(result['result']['deleted'], 1)
        self.assertEqual(self.confirm(preview)['operation_id'], result['operation_id'])
        self.assertEqual(json.loads((self.root/result['result']['audit']).read_text())['status'], 'complete')

    def test_changed_candidate_fails_whole_cleanup_without_deletion(self):
        target = self.thumbnail()
        preview = self.preview()
        target.write_bytes(b'changed')
        value = self.confirm(preview, code=4)
        self.assertEqual(value['error']['code'], 'cleanup:preview_stale')
        self.assertTrue(target.exists())

    def test_wrong_digest_and_expired_preview_are_not_recreated(self):
        target = self.thumbnail()
        preview = self.preview()
        original = preview['result']['digest']
        preview['result']['digest'] = '0'*64
        self.confirm(preview, code=4)
        preview['result']['digest'] = original
        with self.runtime.previews.lock:
            key = preview['result']['preview_id']
            stored = self.runtime.previews.values[key]
            self.runtime.previews.values[key] = (0, *stored[1:])
        self.confirm(preview, code=4)
        self.assertTrue(target.exists())
        self.assertEqual(len(self.runtime.previews.values), 1)

    def test_project_switch_rejects_old_confirmation_without_affecting_new_project(self):
        target = self.thumbnail()
        preview = self.preview()
        self.runtime._close_current()
        current = self.runtime.open_now(self.root)
        self.assertNotEqual(current['project_id'], preview['project_id'])
        value = self.confirm(preview, code=4)
        self.assertEqual(value['code'], 'project:handle_expired')
        self.assertTrue(target.exists())
        self.assertNotIn('request', value['cli'])

    def test_same_request_key_replays_scan_and_different_body_conflicts(self):
        key = str(uuid4())
        first = self.cli('scan', '--known', '--idempotency-key', key)
        second = self.cli('scan', '--known', '--idempotency-key', key)
        self.assertEqual(first['operation_id'], second['operation_id'])
        known_id = self.request('GET', '/api/projects')['items'][0]['known_id']
        value = self.cli('scan', '--known-id', known_id, '--idempotency-key', key, code=4)
        self.assertEqual(value['code'], 'idempotency:key_conflict')

    def test_known_scan_without_current_project_and_paginated_results(self):
        first = self.pid
        self.runtime._close_current()
        self.runtime.open_now(self.base/'second.slproj', create=True, name='Second')
        self.runtime._close_current()
        value = self.cli('scan', '--known')
        self.assertEqual(value['result']['project_count'], 2)
        page = self.cli('results', value['operation_id'], '--limit', '1', '--sort', 'known_id')
        self.assertTrue(page['has_more'])
        other = self.cli('results', value['operation_id'], '--limit', '1', '--sort', 'known_id', '--cursor', page['next_cursor'])
        self.assertFalse(other['has_more'])
        self.assertNotEqual(page['items'][0]['known_id'], other['items'][0]['known_id'])
        self.assertNotEqual(page['items'][0]['known_id'], first)
        self.cli('results', value['operation_id'], '--cursor', page['next_cursor'], code=2)

    def test_selected_recent_scope_and_unavailable_project_exit_six(self):
        known_id = self.request('GET', '/api/projects')['items'][0]['known_id']
        self.runtime._close_current()
        self.root.rename(self.base/'moved.slproj')
        value = self.cli('scan', '--known-id', known_id, code=6)
        self.assertEqual(value['result']['scope'], 'selected_known')
        page = self.cli('results', value['operation_id'], code=6)
        self.assertIsNone(page['items'][0]['total_bytes'])
        self.assertEqual(page['items'][0]['status'], 'unavailable')

    def test_partial_cleanup_keeps_failed_file_and_returns_six(self):
        first, second = self.thumbnail('a'), self.thumbnail('b')
        from stylelora.services.storage_files import unlink_verified
        def remove(root, row, dirs):
            if row['path'].endswith('b'*64+'.webp'):
                raise PermissionError('private-token')
            return unlink_verified(root, row, dirs)
        with patch('stylelora.services.storage.unlink_verified', side_effect=remove):
            value = self.confirm(self.preview(), code=6)
        self.assertEqual((value['result']['deleted'], value['result']['failed']), (1, 1))
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertNotIn('private-token', json.dumps(value))

    def test_rejected_cleanup_retains_annotation_rows_and_marks_missing(self):
        self.import_inputs()
        row = self.request('GET', self.p+'/images/1')
        self.request('POST', self.p+'/images/review', {'ids': [1], 'target': 'reject', 'expected_versions': {'1': row['version']}})
        annotations = self.request('GET', self.p+'/annotations/1')
        self.confirm(self.preview('rejected_files'))
        self.assertTrue(self.request('GET', self.p+'/images/1')['file_missing'])
        self.assertEqual(self.request('GET', self.p+'/annotations/1'), {**annotations, 'file_missing': True})

    def test_export_cleanup_preserves_original_images(self):
        self.import_inputs()
        self.keep([1])
        exported = self.run_export(self.export_preview())
        value = self.confirm(self.preview('exports'))
        self.assertGreater(value['result']['deleted'], 1)
        self.assertFalse(self.request('GET', self.p+'/images/1')['file_missing'])
        self.assertEqual(self.request('GET', self.p+'/exports/'+exported['export_id'])['status'], 'cleaned')

    def test_detached_scan_remains_queryable(self):
        value = self.cli('scan', '--detach')
        self.assertEqual(value['status'], 'accepted')
        complete = self.cli('status', value['task']['id'], '--wait')
        self.assertEqual(complete['status'], 'done')
        self.assertEqual(complete['project_id'], self.pid)
