import hashlib
import threading
from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db.locking import ProjectLock
from stylelora.project_db.storage import atomic_json
from stylelora.services.recent_projects import RecentProjects
from stylelora.services.storage_files import inventory
from .api_support import APITest


class RecentTests(APITest):
    def rows(self, suffix=''):
        return self.request('GET', '/api/projects'+suffix)['items']

    def other(self, name='other'):
        self.runtime._close_current()
        return self.runtime.open_now(self.base/(name+'.slproj'), create=True, name=name)

    def scan(self, identifiers=None, expected='done'):
        accepted = self.request('POST', '/api/storage/scans', {} if identifiers is None else {'known_ids': identifiers}, status=202)
        return self.wait_operation(accepted, expected=expected)

    def scan_rows(self, scan, suffix=''):
        return self.request('GET', scan['result']['results_url']+suffix)

    def persistent(self):
        self.runtime.recent = RecentProjects(self.runtime, self.base/'product')
        self.runtime.remember_project(self.runtime.context)
        return self.runtime.recent.path

    def test_successful_open_is_recorded_with_redacted_path_and_distinct_id(self):
        rows = self.rows(); self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['status'], 'current')
        self.assertEqual(row['project_id'], self.pid)
        self.assertNotEqual(row['known_id'], self.pid)
        self.assertNotIn(str(self.base), str(rows)); self.assertNotIn('identity', row)

    def test_reopen_known_preserves_known_id_but_replaces_project_handle(self):
        row = self.rows()[0]
        self.runtime._close_current()
        accepted = self.request('POST', '/api/projects/open', {'known_id': row['known_id']}, status=202)
        view = self.wait_operation(accepted)['result']
        self.assertEqual(view['known_id'], row['known_id']); self.assertNotEqual(view['project_id'], self.pid)
        self.assertEqual(len(self.rows()), 1)

    def test_history_persists_and_get_does_not_write_or_auto_open(self):
        path = self.persistent(); row = self.rows()[0]
        self.runtime._close_current()
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.runtime.recent = RecentProjects(self.runtime, path.parent)
        self.assertEqual(self.rows()[0]['known_id'], row['known_id'])
        self.assertIsNone(self.runtime.current())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_metadata_rename_updates_history_without_reordering(self):
        first = self.rows()[0]
        project = self.runtime.current()
        self.request('PATCH', self.p, {'name': '新的名称'}, version=project['version'])
        row = self.rows()[0]
        self.assertEqual(row['name'], '新的名称'); self.assertEqual(row['last_opened_at'], first['last_opened_at'])

    def test_bad_history_is_not_overwritten_and_direct_open_remains_usable(self):
        path = self.persistent(); path.write_bytes(b'{bad')
        self.request('GET', '/api/projects', status=503)
        project = self.other()
        self.assertEqual(project['recent_warning'], 'recent:save_failed')
        self.assertEqual(path.read_bytes(), b'{bad')
        self.assertIsNotNone(self.runtime.current())

    def test_save_response_lost_after_replace_is_reconciled(self):
        path = self.persistent()
        def lost(target, value):
            atomic_json(target, value); raise OSError('lost acknowledgement')
        with patch('stylelora.services.recent_projects.atomic_json', side_effect=lost):
            project = self.other()
        self.assertIsNone(project['recent_warning']); self.assertEqual(len(self.rows()), 2)
        self.assertTrue(path.exists())

    def test_recording_io_failure_does_not_turn_successful_open_into_failure(self):
        self.persistent()
        with patch('stylelora.services.recent_projects.atomic_json', side_effect=PermissionError('private')):
            project = self.other()
        self.assertIsNone(project['known_id']); self.assertEqual(project['recent_warning'], 'recent:save_failed')
        self.assertEqual(len(self.rows()), 1)

    def test_failed_open_never_enrolls_a_path(self):
        with self.assertRaises(FileNotFoundError):
            self.runtime.open_now(self.base/'invalid.slproj')
        self.assertEqual(len(self.rows()), 1)

    def test_open_requires_exactly_one_reference_and_never_accepts_a_path(self):
        identifier = self.rows()[0]['known_id']
        for value in ({}, {'known_id': identifier, 'grant_id': str(uuid4())}, {'path': str(self.root)}, {'known_id': self.pid, 'extra': True}):
            self.request('POST', '/api/projects/open', value, status=422)
        self.request('POST', '/api/projects/open', {'known_id': self.pid}, status=404)
        self.assertEqual(self.runtime.current()['project_id'], self.pid)

    def test_moved_or_replaced_root_cannot_reuse_known_authorization(self):
        row = self.rows()[0]; self.runtime._close_current()
        renamed = self.base/'moved.slproj'
        self.assertTrue(self.root.resolve().is_relative_to(self.base.resolve()))
        self.assertTrue(renamed.resolve().is_relative_to(self.base.resolve()))
        self.root.rename(renamed)
        self.assertEqual(self.rows()[0]['reason'], 'recent:missing')
        self.request('POST', '/api/projects/open', {'known_id': row['known_id']}, status=404)
        self.root.mkdir()
        self.assertEqual(self.rows()[0]['reason'], 'recent:replaced')
        self.request('POST', '/api/projects/open', {'known_id': row['known_id']}, status=409)

    def test_external_project_lock_is_reported_and_current_project_is_preserved(self):
        first = self.rows()[0]; other = self.other()
        locked = ProjectLock(self.root)
        try:
            row = next(r for r in self.rows() if r['known_id'] == first['known_id'])
            self.assertEqual(row['reason'], 'project:locked')
            self.request('POST', '/api/projects/open', {'known_id': first['known_id']}, status=423)
            self.assertEqual(self.runtime.current()['project_id'], other['project_id'])
            scan = self.scan([first['known_id']]); value = self.scan_rows(scan)['items'][0]
            self.assertEqual(value['reason'], 'project:locked'); self.assertIsNone(value['total_bytes'])
        finally:
            locked.close()

    def test_recent_pagination_is_revision_bound_and_deduplicated(self):
        self.other('two'); self.other('three')
        page = self.request('GET', '/api/projects?limit=1')
        second = self.request('GET', '/api/projects?limit=1&cursor='+page['next_cursor'])
        self.assertNotEqual(page['items'][0]['known_id'], second['items'][0]['known_id'])
        self.runtime.open_now(self.runtime.context.handle.root)
        self.request('GET', '/api/projects?limit=1&cursor='+page['next_cursor'], status=422)
        self.assertEqual(len(self.rows()), 3)

    def test_bounded_history_evicts_records_without_touching_projects(self):
        with patch('stylelora.services.recent_projects.MAX_RECENT', 2):
            self.other('two'); self.other('three')
            self.assertEqual(len(self.rows()), 2)
        self.assertTrue((self.root/'project.db').exists())

    def test_cross_project_scan_uses_only_known_roots_and_no_second_writer(self):
        first = self.rows()[0]; self.other()
        unknown = self.base/'never-opened.slproj'; unknown.mkdir(); (unknown/'private.txt').write_text('not authorized')
        before = {p.relative_to(self.root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                  for p in self.root.rglob('*') if p.is_file()}
        with patch('stylelora.runtime.lifecycle.open_project', side_effect=AssertionError('no writer')):
            scan = self.scan(); result = self.scan_rows(scan)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(sum(r['total_bytes'] for r in result['items']), result['summary']['observed_total_bytes'])
        self.assertFalse(result['summary']['partial'])
        after = {p.relative_to(self.root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn(first['known_id'], [r['known_id'] for r in result['items']])
        self.assertNotIn(str(self.base), str(result))
        self.assertNotIn('private.txt', str(result))

    def test_subset_unknown_duplicate_empty_and_path_inputs(self):
        first = self.rows()[0]; self.other()
        result = self.scan_rows(self.scan([first['known_id']]))
        self.assertEqual([r['known_id'] for r in result['items']], [first['known_id']])
        for body, status in (({'known_ids': [str(uuid4())]}, 404), ({'known_ids': []}, 422),
                             ({'known_ids': [first['known_id']]*2}, 422), ({'paths': [str(self.root)]}, 422)):
            self.request('POST', '/api/storage/scans', body, status=status)

    def test_unavailable_project_is_not_fabricated_as_zero_bytes(self):
        first = self.rows()[0]; self.other()
        (self.root/'project.json').unlink()
        result = self.scan_rows(self.scan())
        bad = next(r for r in result['items'] if r['known_id'] == first['known_id'])
        self.assertIsNone(bad['total_bytes']); self.assertTrue(result['summary']['partial'])
        self.assertEqual(result['summary']['unavailable_count'], 1)

    def test_results_sort_pagination_and_scope_binding(self):
        self.other('two'); self.other('three')
        scan = self.scan(); url = scan['result']['results_url']
        first = self.request('GET', url+'?limit=1')
        following = self.request('GET', url+'?limit=1&cursor='+first['next_cursor'])
        self.assertNotEqual(first['items'][0]['known_id'], following['items'][0]['known_id'])
        self.request('GET', url+'?sort=known_id&cursor='+first['next_cursor'], status=422)
        other = self.scan()
        self.request('GET', other['result']['results_url']+'?cursor='+first['next_cursor'], status=422)
        self.request('GET', url+'?sort=path', status=422)
        self.runtime._close_current()
        self.assertEqual(self.request('GET', url)['total'], 3)

    def test_expiration_and_retained_scan_limit(self):
        old = self.scan()['result']['scan_id']
        for _ in range(3): self.scan()
        self.request('GET', '/api/storage/scans/'+old, status=410)
        registry = self.runtime.known_storage
        with registry.lock:
            key = next(iter(registry.results)); value = registry.results[key]
            registry.results[key] = (0, *value[1:])
        self.request('GET', '/api/storage/scans/'+key, status=410)

    def test_cancel_does_not_publish_truncated_success_or_hold_lock(self):
        first = self.rows()[0]; self.other()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def wait(*args, **kwargs):
            entered.set(); release.wait(5)
            return inventory(*args, **kwargs)
        with patch('stylelora.services.known_storage.inventory', side_effect=wait):
            accepted = self.request('POST', '/api/storage/scans', {'known_ids': [first['known_id']]}, status=202)
            self.assertTrue(entered.wait(3))
            current = self.request('GET', accepted['status_url'])
            self.request('POST', accepted['status_url']+'/cancel', {}, version=current['version'])
            self.request('POST', '/api/storage/scans', {}, status=409)
            release.set(); self.wait_operation(accepted, expected='canceled')
        self.request('GET', '/api/storage/scans/'+current['operation_id'], status=410)
        locked = ProjectLock(self.root); locked.close()

    def test_project_set_is_frozen_at_admission(self):
        first = self.rows()[0]; self.other()
        entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        def wait(*args, **kwargs):
            entered.set(); release.wait(5); return inventory(*args, **kwargs)
        with patch('stylelora.services.known_storage.inventory', side_effect=wait):
            accepted = self.request('POST', '/api/storage/scans', {'known_ids': [first['known_id']]}, status=202)
            self.assertTrue(entered.wait(3)); self.other('later'); release.set()
            result = self.wait_operation(accepted)
        self.assertEqual(self.scan_rows(result)['total'], 1)

    def test_junction_root_is_rejected_and_outside_contents_not_scanned(self):
        import _winapi
        first = self.rows()[0]; self.other()
        renamed = self.base/'held.slproj'
        self.assertTrue(renamed.resolve().is_relative_to(self.base.resolve()))
        self.assertTrue(self.root.resolve().is_relative_to(self.base.resolve()))
        self.root.rename(renamed); _winapi.CreateJunction(str(renamed), str(self.root))
        try:
            result = self.scan_rows(self.scan([first['known_id']]))['items'][0]
            self.assertEqual(result['reason'], 'recent:unsafe'); self.assertIsNone(result['total_bytes'])
        finally: self.root.rmdir()

    def test_partial_scan_reports_observed_lower_bound(self):
        with patch('stylelora.services.storage_files.MAX_ENTRIES', 1):
            result = self.scan_rows(self.scan())
        self.assertTrue(result['summary']['partial']); self.assertEqual(result['items'][0]['status'], 'partial')

    def test_enrollment_after_explicit_reauthorization_changes_replaced_id(self):
        old = self.rows()[0]['known_id']; self.runtime._close_current()
        renamed = self.base/'original.slproj'
        self.assertTrue(renamed.resolve().is_relative_to(self.base.resolve()))
        self.assertTrue(self.root.resolve().is_relative_to(self.base.resolve()))
        self.root.rename(renamed)
        view = self.runtime.open_now(self.root, create=True, name='replacement')
        self.assertNotEqual(view['known_id'], old); self.assertEqual(len(self.rows()), 1)
        self.request('POST', '/api/projects/open', {'known_id': old}, status=404)

    def test_authentication_and_idempotency_are_required(self):
        self.assertEqual(self.client.get('/api/projects', headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        self.assertEqual(self.client.post('/api/storage/scans', json={}).status_code, 428)
        key = str(uuid4())
        a = self.request('POST', '/api/storage/scans', {}, key=key, status=202)
        self.wait_operation(a)
        b = self.request('POST', '/api/storage/scans', {}, key=key, status=202)
        self.assertEqual(a['task'], b['task'])

    def test_queued_known_open_rechecks_identity_before_closing_current(self):
        old = self.rows()[0]; current = self.other()
        entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        original = self.runtime.open_now
        def delayed(*args, **kwargs):
            entered.set(); release.wait(5); return original(*args, **kwargs)
        with patch.object(self.runtime, 'open_now', side_effect=delayed):
            accepted = self.request('POST', '/api/projects/open', {'known_id': old['known_id']}, status=202)
            self.assertTrue(entered.wait(3))
            renamed = self.base/'replaced-while-queued.slproj'
            self.assertTrue(renamed.resolve().is_relative_to(self.base.resolve()))
            self.assertTrue(self.root.resolve().is_relative_to(self.base.resolve()))
            self.root.rename(renamed); self.root.mkdir()
            release.set(); result = self.wait_operation(accepted, expected='failed')
        self.assertEqual(result['error']['code'], 'recent:replaced')
        self.assertEqual(self.request('GET', '/api/projects/current')['project_id'], current['project_id'])

    def test_current_project_is_retained_until_cross_scan_releases_it(self):
        entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        context = self.runtime.context
        current = self.request('GET', '/api/projects/current')
        def delayed(*args, **kwargs):
            entered.set(); release.wait(5); return inventory(*args, **kwargs)
        with patch('stylelora.services.known_storage.inventory', side_effect=delayed):
            accepted = self.request('POST', '/api/storage/scans', {}, status=202)
            self.assertTrue(entered.wait(3)); self.assertEqual(context.users, 1)
            closed = self.request('POST', '/api/projects/'+current['project_id']+'/close', {}, version=current['version'], status=202)
            self.assertIn(self.request('GET', closed['status_url'])['status'], ('pending', 'running'))
            release.set(); scanned = self.wait_operation(accepted); self.wait_operation(closed)
        self.assertEqual(context.users, 0); self.assertIsNone(self.runtime.context)
        self.assertEqual(self.scan_rows(scanned)['total'], 1)
