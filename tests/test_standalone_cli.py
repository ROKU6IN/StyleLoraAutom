import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
import stylelora

from stylelora.cli import main
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.project import open_project
from stylelora.project_db.repositories import jobs, sources
from stylelora.project_db.types import ItemSeed, JobRequest, JobSnapshot, Scope
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import ProductLock
from stylelora.runtime.lifecycle import Runtime
from stylelora.runtime import standalone
from .api_support import APITest
from .test_nai_direct import DEFAULT_CONFIG


class StandaloneCLITests(APITest):
    def setUp(self):
        super().setUp()
        self.state = self.base/'isolated-runtime'
        self.instances = []

    def factory(self, **kwargs):
        runtime = Runtime(**kwargs)
        self.instances.append(runtime)
        return runtime

    def args(self, group, action, *args):
        return [group, action, *map(str, args), '--standalone', '--project', str(self.root),
                '--runtime-dir', str(self.state)]

    def invoke(self, group, action, *args, expected=0):
        self.runtime._close_current()
        output, progress = io.StringIO(), io.StringIO()
        with patch('stylelora.runtime.lifecycle.Runtime', side_effect=self.factory), \
                patch('stylelora.runtime.attached_cli.pipe_request', side_effect=AssertionError('No attachment')), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
            code = main(self.args(group, action, *args))
        value = json.loads(output.getvalue())
        self.assertEqual(code, expected, value)
        for runtime in self.instances:
            self.assertTrue(runtime.closed)
            self.assertIsNone(runtime.context)
            self.assertNotIn(runtime.token, output.getvalue()+progress.getvalue())
        self.assertFalse((self.state/'instance.json').exists())
        self.assertTrue(value['cli']['service_closed'])
        self.assertFalse(value['cli']['session_reusable'])
        self.assertEqual(value['cli']['mode'], 'standalone')
        return value, progress.getvalue()

    def test_source_list_collects_pages_without_network_or_pipe(self):
        for i in range(3):
            self.request('POST', self.p+'/sources', {'name': f'Source {i}', 'config': DEFAULT_CONFIG,
                         'style_prompt': 'style', 'target_count': 1}, status=201)
        with patch.object(httpx.HTTPTransport, 'handle_request', side_effect=AssertionError('No socket')), \
                patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('No socket')):
            result, _ = self.invoke('source', 'list', '--limit', 1)
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(result['cli']['pagination'], {'pages': 3, 'returned': 3, 'complete': True})
        self.assertIsNone(result['next_cursor'])
        self.assertFalse(result['has_more'])
        self.assertNotIn('limit', result)
        self.assertTrue(result['cli']['service_started'])

    def test_import_summary_and_items_survive_new_sessions(self):
        imported = self.import_inputs(3)
        sid = imported['source_id']
        result, _ = self.invoke('import', 'summary', sid)
        self.assertEqual(result['imported'], 3)
        items, _ = self.invoke('source', 'items', sid, '--limit', 1)
        self.assertEqual(len(items['items']), 3)
        item, _ = self.invoke('source', 'item', items['items'][0]['item_id'])
        self.assertEqual(item['status'], 'done')
        self.assertEqual(item['attempts'], 1)

    def test_source_status_keeps_sent_unknown_and_does_not_resume(self):
        source = self.request('POST', self.p+'/sources', {'name': 'Unknown', 'config': DEFAULT_CONFIG,
                              'style_prompt': 'style', 'target_count': 2}, status=201)
        project = self.runtime.context.handle
        def seed(db):
            commands.start_source(db, source['source_id'], [ItemSeed(1, 'forest'), ItemSeed(2, 'river')])
            iid = db.execute('SELECT item_id FROM source_items ORDER BY item_id LIMIT 1').fetchone()[0]
            commands.mark_sent(db, iid)
        submit_write(project, seed).result()
        with patch('stylelora.backends.nai_direct.client.NaiDirectBackend.generate', side_effect=AssertionError('No generation')):
            result, _ = self.invoke('source', 'status', source['source_id'], expected=7)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual((result['counts']['sent'], result['counts']['pending']), (1, 1))
        self.assertEqual(result['cli']['recovery_counts']['unresolved_item_ids'], 1)
        with open_project(self.root) as reopened, read_snapshot(reopened) as db:
            rows = list(db.execute('SELECT status,attempts FROM source_items ORDER BY item_id'))
        self.assertEqual([tuple(row) for row in rows], [('sent', 1), ('pending', 0)])

    def test_storage_scan_completes_before_runtime_closes(self):
        self.import_inputs()
        result, progress = self.invoke('storage', 'scan')
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['kind'], 'storage.scan')
        self.assertGreater(result['result']['total_bytes'], 0)
        self.assertIn('Accepted storage operation', progress)
        with open_project(self.root):
            pass
        owner = ProductLock()
        owner.close()

    def test_job_queries_use_persistent_state_without_inference(self):
        iid = self.import_inputs()['items'][0]['image_id']
        project = self.runtime.context.handle
        bid = str(uuid4())
        def seed(db):
            job = jobs.create_batch(db, bid, (JobRequest('wd', 'selected', Scope(selected_ids=(iid,))),))[0]
            jobs.freeze_job(db, job.job_id, JobSnapshot('fixture-model'), image_ids=(iid,))
            return job.job_id
        jid = submit_write(project, seed).result()
        listing, _ = self.invoke('job', 'list', '--limit', 1)
        self.assertEqual(listing['items'][0]['batch_id'], bid)
        result, _ = self.invoke('job', 'status', jid, expected=7)
        self.assertEqual(result['status'], 'paused')
        rows, _ = self.invoke('job', 'outcomes', jid)
        self.assertEqual(rows['items'], [])

    def test_export_reports_are_read_from_disk_after_reopen(self):
        iid = self.import_inputs()['items'][0]['image_id']
        self.keep([iid])
        exported = self.run_export(self.export_preview())
        eid = exported['export_id']
        listing, _ = self.invoke('export', 'list', '--limit', 1)
        self.assertEqual(listing['items'][0]['export_id'], eid)
        result, _ = self.invoke('export', 'status', eid)
        self.assertEqual(result['status'], 'finished')
        report, _ = self.invoke('export', 'report', eid)
        self.assertEqual(report['counts']['exported'], 1)

    def test_product_lock_conflict_does_not_open_or_switch_any_project(self):
        self.runtime._close_current()
        owner = ProductLock()
        try:
            result, _ = self.invoke('source', 'list', expected=4)
        finally:
            owner.close()
        self.assertEqual(result['code'], 'instance:already_running')
        self.assertFalse(result['cli']['service_started'])
        self.assertEqual(self.instances, [])
        self.assertFalse(self.state.exists())

    def test_project_lock_conflict_closes_owned_runtime(self):
        self.runtime._close_current()
        env = {**os.environ, 'PYTHONPATH': str(Path(stylelora.__file__).resolve().parents[1]), 'PYTHONIOENCODING': 'utf-8'}
        script = "import sys\nfrom pathlib import Path\nfrom stylelora.project_db.project import open_project\nwith open_project(Path(sys.argv[1])):\n print('ready', flush=True)\n sys.stdin.readline()\n"
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root)], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            result, _ = self.invoke('source', 'list', expected=4)
        finally:
            child.communicate('\n', timeout=15)
        self.assertEqual(result['code'], 'project:locked')
        self.assertTrue(result['cli']['service_started'])

    def test_missing_project_is_redacted_and_lock_is_released(self):
        self.runtime._close_current()
        original = self.root
        self.root = self.base/'missing-sensitive.slproj'
        try:
            result, progress = self.invoke('source', 'list', expected=2)
        finally:
            self.root = original
        self.assertNotIn('missing-sensitive', json.dumps(result)+progress)
        owner = ProductLock()
        owner.close()

    def test_http_failure_still_closes_project_and_runtime(self):
        result, _ = self.invoke('source', 'status', 999, expected=2)
        self.assertEqual(result['code'], 'resource:not_found')
        with open_project(self.root):
            pass

    def test_interruption_drains_and_does_not_claim_detached_work_continues(self):
        with patch('stylelora.runtime.source_cli.execute', side_effect=KeyboardInterrupt):
            value, progress = self.invoke('source', 'list', expected=130)
        self.assertIn('Standalone command interrupted', progress)
        self.assertNotIn('accepted work continues', progress)
        self.assertEqual(value['code'], 'client:interrupted')
        with open_project(self.root):
            pass

    def test_storage_interruption_stops_owned_operation(self):
        original = __import__('stylelora.runtime.storage_cli', fromlist=['Connection']).Connection.operation
        def interrupt(connection, operation_id, wait):
            if wait:
                raise KeyboardInterrupt()
            return original(connection, operation_id, wait)
        with patch('stylelora.runtime.storage_cli.Connection.operation', new=interrupt):
            value, _ = self.invoke('storage', 'scan', expected=130)
        operation = self.instances[-1].supervisor.tasks[value['cli']['operation_id']]
        self.assertNotIn(operation.status, ('pending', 'running'))
        self.assertTrue(operation.finished.is_set())

    def test_local_transport_forbids_side_effects_other_than_storage_scan(self):
        self.runtime._close_current()
        args = SimpleNamespace(project=self.root, runtime_dir=self.state, product_dir=None)
        with standalone.container(args, {}) as connection:
            client = connection.client
            for method, path in (('POST', '/api/projects'), ('POST', '/api/backends/nai_direct/account-status'),
                                 ('DELETE', '/api/projects/current'), ('GET', '//external.invalid/api/secret')):
                with self.subTest(method=method, path=path), self.assertRaises(ServiceError):
                    client.request(method, path)

    def test_result_limits_fail_without_leaving_project_open(self):
        self.import_inputs(2)
        with patch.object(standalone, 'MAX_ITEMS', 1):
            value, _ = self.invoke('source', 'items', 1, '--limit', 1, expected=2)
        self.assertEqual(value['code'], 'standalone:result_limit')

    def test_real_cli_subprocess_outputs_only_json_and_releases_locks(self):
        self.import_inputs()
        self.runtime._close_current()
        env = {**os.environ, 'PYTHONPATH': str(Path(stylelora.__file__).resolve().parents[1]), 'PYTHONIOENCODING': 'utf-8'}
        result = subprocess.run([sys.executable, '-m', 'stylelora', *self.args('source', 'items', 1, '--limit', 1)],
                                capture_output=True, encoding='utf-8', env=env, timeout=25,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        value = json.loads(result.stdout)
        self.assertTrue(value['cli']['service_closed'])
        self.assertEqual(len(value['items']), 1)
        self.assertNotIn('Bearer', result.stdout+result.stderr)
        with open_project(self.root):
            pass
        owner = ProductLock()
        owner.close()

    def test_runtime_initialization_failure_releases_exclusion(self):
        self.runtime._close_current()
        info = {}
        args = SimpleNamespace(project=self.root, runtime_dir=self.state, product_dir=None)
        with patch('stylelora.runtime.lifecycle.Runtime', side_effect=OSError('private path')), self.assertRaises(ServiceError) as error:
            with standalone.container(args, info):
                self.fail('Container must not be admitted')
        self.assertEqual(error.exception.code, 'storage:unavailable')
        self.assertTrue(info['service_closed'])
        self.assertFalse(info['service_started'])
        owner = ProductLock()
        owner.close()

    def test_failed_shutdown_reports_unknown_and_keeps_exclusion(self):
        self.runtime._close_current()
        args = SimpleNamespace(project=self.root, runtime_dir=self.state, product_dir=None)
        info, locks = {}, []
        runtime = original_shutdown = None
        def lock():
            value = ProductLock()
            locks.append(value)
            return value
        try:
            with patch('stylelora.runtime.instance.ProductLock', side_effect=lock), self.assertRaises(ServiceError) as error:
                with standalone.container(args, info) as connection:
                    runtime = connection.client.runtime
                    original_shutdown = runtime.shutdown
                    def fail():
                        raise OSError('close failed')
                    runtime.shutdown = fail
            self.assertEqual(error.exception.code, 'standalone:shutdown_incomplete')
            self.assertEqual(error.exception.outcome, 'unknown')
            self.assertFalse(info['service_closed'])
            with self.assertRaises(ServiceError):
                ProductLock()
        finally:
            if original_shutdown:
                runtime.shutdown = original_shutdown
                original_shutdown()
            for owner in locks:
                owner.close()


class StandaloneValidationTests(unittest.TestCase):
    def test_invalid_options_are_rejected_before_any_runtime(self):
        cases = [
            ['source', 'list', '--standalone'], ['source', 'list', '--project', 'x.slproj'],
            ['source', 'status', '1', '--standalone', '--project-id', str(uuid4())],
            ['source', 'list', '--standalone', '--project', 'x.slproj', '--cursor', 'stale'],
            ['source', 'list', '--standalone', '--project', 'x.slproj', '--instance-id', str(uuid4())],
            ['storage', 'scan', '--standalone', '--project', 'x.slproj', '--detach'],
            ['storage', 'scan', '--standalone', '--project', 'x.slproj', '--known'],
            ['storage', 'scan', '--standalone', '--project', 'x.slproj', '--idempotency-key', str(uuid4())],
            ['source', 'list', '--runtime-dir', 'x'],
            ['source', 'list', '--product-dir', 'x'],
            ['import', 'preview', 'x', '--standalone', '--project', 'x.slproj'],
            ['export', 'preview', '--standalone', '--project', 'x.slproj'],
            ['job', 'preview', '--executor', 'llm_vision', '--standalone', '--project', 'x.slproj'],
            ['source', 'resume', '1', '--standalone', '--project', 'x.slproj'],
            ['storage', 'cleanup', '--kind', 'thumbnails', '--standalone', '--project', 'x.slproj'],
        ]
        with patch('stylelora.runtime.instance.ProductLock') as lock, patch('stylelora.runtime.lifecycle.Runtime') as runtime:
            for args in cases:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
            lock.assert_not_called()
            runtime.assert_not_called()

    def test_pagination_rejects_repeated_cursor_and_oversized_output(self):
        args = SimpleNamespace(standalone=True, cursor=None)
        def page(_):
            return {'items': [{'id': 1}], 'next_cursor': 'same', 'has_more': True}
        with self.assertRaises(ServiceError) as error:
            standalone.collect(args, {}, page)
        self.assertEqual(error.exception.code, 'request:invalid_response')
        with patch.object(standalone, 'MAX_BYTES', 1), self.assertRaises(ServiceError) as error:
            standalone.collect(args, {}, page)
        self.assertEqual(error.exception.code, 'standalone:result_limit')

    def test_pagination_rejects_incomplete_or_malformed_pages(self):
        for value in ({'items': [], 'next_cursor': 'next'}, {'items': [], 'has_more': True}, {'items': None}):
            with self.subTest(value=value), self.assertRaises(ServiceError):
                standalone.collect(SimpleNamespace(standalone=True, cursor=None), {}, lambda _: value)

