import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db.storage import atomic_json
from stylelora.services.product_config import ProductConfig, MIB, MAX_THRESHOLD, DEFAULTS
from stylelora.services.storage_files import inventory
from stylelora.runtime.errors import ServiceError
from .api_support import APITest


class StorageConfigTests(APITest):
    def config(self):
        return self.request('GET', '/api/config')

    def change(self, body=None, *, version=None, status=200, key=None):
        return self.request('PATCH', '/api/config', body or {'storage_thresholds': {'project_bytes': MIB}},
                            version=version or self.config()['version'], status=status,
                            headers={'Idempotency-Key': key or str(uuid4())})

    def persistent(self):
        self.runtime.product_config = ProductConfig(self.runtime, self.base/'config')
        return self.runtime.product_config.path

    def scan(self, known=False, body=None):
        path = '/api/storage/scans' if known else self.p+'/storage/scans'
        return self.wait_operation(self.request('POST', path, body or {}, status=202))['result']

    def test_defaults_and_reads_do_not_create_files_or_scan(self):
        path = self.persistent()
        with patch('stylelora.services.known_storage.inventory', side_effect=AssertionError('scan')), \
             patch('stylelora.services.storage.inventory', side_effect=AssertionError('scan')):
            self.assertEqual(self.config()['storage_thresholds'], DEFAULTS)
            value = self.request('GET', '/api/storage/summary')
        self.assertIsNone(value['current_project']); self.assertIsNone(value['known_projects'])
        self.assertFalse(path.exists())

    def test_partial_patch_persists_outside_project_and_restart(self):
        path = self.persistent(); old = self.config()
        saved = self.change()
        self.assertEqual(saved['revision'], old['revision']+1)
        self.assertEqual(saved['storage_thresholds'], {**DEFAULTS, 'project_bytes': MIB})
        self.assertFalse((self.root/'product-config.json').exists())
        self.runtime.product_config = ProductConfig(self.runtime, path.parent)
        self.assertEqual(self.config(), saved)

    def test_noop_preserves_revision_version_and_file_mtime(self):
        path = self.persistent(); self.change()
        before, data = path.stat().st_mtime_ns, path.read_bytes()
        value = self.config(); after = self.change()
        self.assertEqual(value, after); self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(path.read_bytes(), data)

    def test_missing_stale_and_aba_versions_are_rejected(self):
        body = {'storage_thresholds': {'project_bytes': MIB}}
        self.request('PATCH', '/api/config', body, headers={'Idempotency-Key': str(uuid4())}, status=428)
        old = self.config()['version']; self.change()
        self.change({'storage_thresholds': DEFAULTS})
        self.change(version=old, status=409)
        self.assertEqual(self.config()['revision'], 2)

    def test_competing_versions_have_only_one_winner(self):
        version = self.config()['version']; gate = threading.Barrier(2)
        def save(amount):
            gate.wait()
            try: return self.runtime.product_config.update({'storage_thresholds': {'total_bytes': amount}}, version)
            except ServiceError as error: return error.code
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(save, (MIB, 2*MIB)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn('config:revision_conflict', results)
        self.assertEqual(self.config()['revision'], 1)

    def test_auth_request_key_duplicate_query_and_schema_boundaries(self):
        self.assertEqual(self.client.get('/api/config', headers={'Authorization': 'Bearer bad'}).status_code, 401)
        self.request('PATCH', '/api/config', {'storage_thresholds': DEFAULTS}, version=self.config()['version'], status=428)
        self.request('GET', '/api/config?path=x', status=422)
        self.request('GET', '/api/storage/summary?path=x', status=422)
        for amount in (0, -MIB, True, str(MIB), float(MIB), MIB+1, MAX_THRESHOLD+MIB, None):
            self.change({'storage_thresholds': {'project_bytes': amount}}, status=422)
        for body in ({'storage_thresholds': {}}, {'storage_thresholds': None}, {'cost_policy': 'allow'},
                     {'storage_thresholds': {'path': 'secret'}}, {'storage_thresholds': DEFAULTS, 'token': 'hidden'}):
            self.change(body, status=422)
        self.change({'storage_thresholds': {'project_bytes': MAX_THRESHOLD, 'total_bytes': MIB}})

    def test_identical_key_replays_original_response_but_different_body_conflicts(self):
        key, version = str(uuid4()), self.config()['version']
        saved = self.change(key=key, version=version)
        self.assertEqual(self.change(key=key, version=version), saved)
        self.change({'storage_thresholds': {'project_bytes': 2*MIB}}, key=key, version=version, status=409)
        self.assertEqual(self.config()['revision'], 1)

    def test_corrupt_settings_preserved_and_scans_do_not_fallback_to_defaults(self):
        path = self.persistent(); path.parent.mkdir(); path.write_bytes(b'broken secret config')
        for endpoint in ('/api/config', '/api/storage/summary'):
            value = self.request('GET', endpoint, status=503)
            self.assertNotIn('secret', str(value)); self.assertNotIn(str(path), str(value))
        self.request('POST', self.p+'/storage/scans', {}, status=503)
        self.request('POST', '/api/storage/scans', {}, status=503)
        self.change(version='old', status=503)
        self.assertEqual(path.read_bytes(), b'broken secret config')
        self.assertEqual(self.request('GET', '/api/projects/current')['project_id'], self.pid)

    def test_invalid_checksum_and_schema_are_preserved(self):
        from stylelora.project_db.validation import digest
        path = self.persistent(); path.parent.mkdir()
        for content in ({'schema_version': True, 'revision': 0, 'storage_thresholds': DEFAULTS},
                        {'schema_version': 1, 'revision': 0, 'storage_thresholds': {'project_bytes': True, 'total_bytes': MIB}}):
            atomic_json(path, {'content': content, 'digest': digest(content)})
            before = path.read_bytes(); self.request('GET', '/api/config', status=503)
            self.assertEqual(path.read_bytes(), before)
        atomic_json(path, {'content': ProductConfig.defaults(), 'digest': 'wrong'})
        self.request('GET', '/api/config', status=503)

    def test_io_failure_returns_unknown_and_preserves_prior_config(self):
        path = self.persistent(); self.change(); original = path.read_bytes()
        with patch('stylelora.services.product_config.atomic_json', side_effect=OSError('private path')):
            error = self.change({'storage_thresholds': {'project_bytes': 2*MIB}}, status=503)
        self.assertEqual(error['error']['outcome'], 'unknown')
        self.assertEqual(path.read_bytes(), original)

    def test_response_lost_after_replace_is_reconciled(self):
        self.persistent()
        def replace_then_fail(path, value):
            atomic_json(path, value); raise OSError('lost response')
        with patch('stylelora.services.product_config.atomic_json', side_effect=replace_then_fail):
            saved = self.change()
        self.assertEqual(saved, self.config()); self.assertEqual(saved['revision'], 1)

    def test_summary_is_read_only_and_current_project_is_not_reused_after_close(self):
        self.scan()
        with patch('stylelora.services.storage.inventory', side_effect=AssertionError('scan')):
            value = self.request('GET', '/api/storage/summary')['current_project']
        self.assertEqual(value['project_id'], self.pid); self.assertTrue(value['stale'])
        self.runtime._close_current()
        self.assertNotIn(self.pid, self.runtime.storage.summaries)
        self.assertIsNone(self.request('GET', '/api/storage/summary')['current_project'])
        self.runtime.open_now(self.root)
        self.assertIsNone(self.request('GET', '/api/storage/summary')['current_project'])

    def test_project_scan_uses_frozen_thresholds_at_admission(self):
        self.change(); entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        def delayed(*args, **kwargs):
            entered.set(); release.wait(5); return inventory(*args, **kwargs)
        with patch('stylelora.services.storage.inventory', side_effect=delayed):
            accepted = self.request('POST', self.p+'/storage/scans', {}, status=202)
            self.assertTrue(entered.wait(3)); self.change({'storage_thresholds': {'project_bytes': 2*MIB}})
            release.set(); result = self.wait_operation(accepted)['result']
        self.assertEqual(result['project_threshold_bytes'], MIB); self.assertEqual(result['config_revision'], 1)
        self.assertTrue(self.request('GET', self.p+'/storage/summary')['config_changed'])
        self.assertTrue(self.request('GET', '/api/storage/summary')['current_project']['config_changed'])

    def test_known_scan_freezes_both_thresholds_and_labels_selected_scope(self):
        self.change({'storage_thresholds': {'project_bytes': MIB, 'total_bytes': MIB}})
        entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        def delayed(*args, **kwargs):
            entered.set(); release.wait(5); return inventory(*args, **kwargs)
        known = self.request('GET', '/api/projects')['items'][0]['known_id']
        with patch('stylelora.services.known_storage.inventory', side_effect=delayed):
            accepted = self.request('POST', '/api/storage/scans', {'known_ids': [known]}, status=202)
            self.assertTrue(entered.wait(3)); self.change({'storage_thresholds': {'project_bytes': 2*MIB, 'total_bytes': 3*MIB}})
            release.set(); result = self.wait_operation(accepted)['result']
        self.assertEqual(result['scope'], 'selected_known')
        self.assertEqual(result['total_threshold_bytes'], MIB); self.assertEqual(result['project_threshold_bytes'], MIB)
        summary = self.request('GET', '/api/storage/summary')['known_projects']
        self.assertTrue(summary['config_changed']); self.assertFalse(summary['expired'])

    def test_threshold_exceeds_is_strict_and_partial_is_preserved(self):
        self.change({'storage_thresholds': {'project_bytes': MIB, 'total_bytes': MIB}})
        for amount, exceeded in ((MIB, False), (MIB+1, True)):
            observed = ([{'path': 'file.bin', 'bytes': amount, 'links': 1}], [{'path': 'blocked', 'code': 'storage:unsafe_or_unreadable'}], {})
            with patch('stylelora.services.storage.inventory', return_value=observed):
                self.assertEqual(self.scan()['exceeds_threshold'], exceeded)
            with patch('stylelora.services.known_storage.inventory', return_value=observed):
                result = self.scan(True)
            self.assertEqual(result['exceeds_threshold'], exceeded)
            self.assertEqual(result['over_threshold_count'], int(exceeded)); self.assertTrue(result['partial'])

    def test_expired_known_summary_retains_historical_time_without_refreshing_scan(self):
        result = self.scan(True); service = self.runtime.known_storage
        with service.lock:
            expiry, rows, summary = service.results[result['scan_id']]
            service.results[result['scan_id']] = (0, rows, summary)
        value = self.request('GET', '/api/storage/summary')['known_projects']
        self.assertTrue(value['expired']); self.assertEqual(value['as_of'], result['as_of'])
        self.request('GET', result['results_url'], status=410)

    def test_changed_recent_set_marks_summary_and_never_scans_new_project(self):
        original = self.scan(True)
        self.runtime._close_current(); self.runtime.open_now(self.base/'second.slproj', create=True, name='second')
        value = self.request('GET', '/api/storage/summary')['known_projects']
        self.assertTrue(value['history_changed']); self.assertEqual(value['scan_id'], original['scan_id'])
        self.assertEqual(value['project_count'], 1)

    def test_canceled_scan_does_not_replace_completed_notice(self):
        original = self.scan(True); entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        def delayed(*args, **kwargs):
            entered.set(); release.wait(5); return inventory(*args, **kwargs)
        with patch('stylelora.services.known_storage.inventory', side_effect=delayed):
            accepted = self.request('POST', '/api/storage/scans', {}, status=202)
            self.assertTrue(entered.wait(3)); task = self.request('GET', accepted['status_url'])
            self.request('POST', accepted['status_url']+'/cancel', {}, version=task['version'])
            release.set(); self.wait_operation(accepted, expected='canceled')
        self.assertEqual(self.request('GET', '/api/storage/summary')['known_projects']['scan_id'], original['scan_id'])

    def test_new_runtime_has_no_old_scan_or_stale_project_alert(self):
        self.scan(True); self.scan()
        from stylelora.services.known_storage import KnownStorage
        from stylelora.services.storage import StorageService
        self.runtime.known_storage = KnownStorage(self.runtime); self.runtime.storage = StorageService(self.runtime)
        value = self.request('GET', '/api/storage/summary')
        self.assertIsNone(value['known_projects']); self.assertIsNone(value['current_project'])

    def test_config_and_scan_events_are_redacted_and_noop_does_not_notify(self):
        with patch.object(self.runtime, 'emit', wraps=self.runtime.emit) as emit:
            self.change(); self.change(); self.scan(); self.scan(True)
        changes = [call for call in emit.call_args_list if call.args[0] == 'config.changed']
        scans = [call for call in emit.call_args_list if call.args[0] == 'storage.changed']
        self.assertEqual(len(changes), 1); self.assertEqual(len(scans), 2)
        self.assertEqual(changes[0].args[1], {'revision': 1})
        for call in scans:
            self.assertEqual(set(call.args[1]), {'scan_id'})

    def test_oversized_or_linked_product_config_is_never_overwritten(self):
        import _winapi
        path = self.persistent(); path.parent.mkdir()
        path.write_bytes(b'x'*16385); self.request('GET', '/api/config', status=503)
        self.assertEqual(path.stat().st_size, 16385)
        destination = self.base/'elsewhere'; destination.mkdir()
        linked = self.base/'linked-config'; _winapi.CreateJunction(str(destination), str(linked))
        try:
            self.runtime.product_config = ProductConfig(self.runtime, linked)
            self.request('GET', '/api/config', status=503)
            self.change(version='old', status=503)
            self.assertEqual(list(destination.iterdir()), [])
        finally: linked.rmdir()
