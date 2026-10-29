import json
import os
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import images
from stylelora.services import storage_files
from .api_support import APITest


class StorageTests(APITest):
    def file(self, relative, content=b'fixture'):
        path = self.root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def preview(self, kind='thumbnails'):
        return self.wait_operation(self.request('POST', self.p+'/storage/cleanup/preview', {'kind': kind}, status=202))['result']

    def clean(self, preview, *, expected='done', key=None):
        accepted = self.request('POST', self.p+'/storage/cleanup', {
            'preview_id': preview['preview_id'], 'digest': preview['digest'], 'confirm': True}, status=202, key=key)
        return self.wait_operation(accepted, expected=expected)

    def reject(self, identifier=1):
        row = self.request('GET', self.p+f'/images/{identifier}')
        self.request('POST', self.p+'/images/review', {'ids': [identifier], 'target': 'reject', 'expected_versions': {str(identifier): row['version']}})

    def scan(self):
        return self.wait_operation(self.request('POST', self.p+'/storage/scans', {}, status=202))['result']

    def test_scan_classifies_without_double_count_or_missing_mutation(self):
        self.import_inputs(3); self.keep([1]); self.reject(2)
        self.file('images/.pending/900/1/receipt.json')
        self.file('images/.costs/900/1.json')
        self.file('cache/unrelated.bin')
        self.file('exports/loose.txt')
        self.file('.migration-backups/v1/project.db')
        self.file('.cleanup/history.json')
        with read_snapshot(self.runtime.context.handle) as db:
            third = images.get_image(db, 3)
        (self.root/'images'/third.filename).unlink()
        result = self.scan()
        self.assertEqual(result['total_bytes'], sum(result['categories'].values()))
        self.assertFalse(result['partial'])
        self.assertEqual(result['missing_records'], 0)
        self.assertEqual(result['not_observed_records'], 1)
        self.assertEqual(result['reviews']['keep']['files'], 1)
        self.assertEqual(result['reviews']['reject']['files'], 1)
        self.assertEqual(result['categories']['pending'], 7)
        self.assertEqual(result['categories']['evidence'], 14)
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertFalse(images.get_image(db, 3).file_missing)
        summary = self.request('GET', self.p+'/storage/summary')
        self.assertTrue(summary['stale']); self.assertEqual(summary['scan'], result)

    def test_initial_summary_unknown_and_reads_do_not_scan(self):
        with patch('stylelora.services.storage.inventory', side_effect=AssertionError('must not scan')):
            result = self.request('GET', self.p+'/storage/summary')
        self.assertIsNone(result['scan']); self.assertFalse(result['stale'])

    def test_thumbnail_cleanup_only_owned_cache_and_replay(self):
        target = self.file('cache/thumbnails/'+'a'*64+'.webp')
        protected = [self.file('cache/other.bin'), self.file('cache/thumbnails/unknown.webp'), self.file('images/.pending/1/1/receipt.json')]
        preview = self.preview()
        self.assertEqual(preview['count'], 1)
        self.assertTrue(target.exists())
        first = self.clean(preview)
        self.assertEqual(first['result']['deleted'], 1, first)
        self.assertFalse(target.exists())
        self.assertTrue(all(p.exists() for p in protected))
        second = self.clean(preview)
        self.assertEqual(first['operation_id'], second['operation_id'])
        self.assertEqual(len(list((self.root/'.cleanup').glob('*.json'))), 1)

    def test_reject_cleanup_preserves_rows_annotations_and_blocks_review(self):
        self.import_inputs(2); self.reject()
        preview = self.preview('rejected_files')
        self.assertEqual(preview['count'], 1)
        with read_snapshot(self.runtime.context.handle) as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM annotations WHERE image_id=1')]
        result = self.clean(preview)['result']
        self.assertEqual(result['deleted'], 1, result)
        row = self.request('GET', self.p+'/images/1')
        self.assertTrue(row['file_missing']); self.assertEqual(row['review_status'], 'reject')
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM annotations WHERE image_id=1')], before)
        self.assertFalse(self.request('GET', self.p+'/images/2')['file_missing'])
        self.request('POST', self.p+'/images/review', {'ids': [1], 'target': 'keep', 'expected_versions': {'1': row['version']}}, status=409)
        self.assertEqual(self.preview('rejected_files')['count'], 0)
        self.assertEqual(json.loads((self.root/result['audit']).read_text())['status'], 'complete')

    def test_review_change_invalidates_whole_preview_before_deletion(self):
        self.import_inputs(2); self.reject(1); self.reject(2)
        preview = self.preview('rejected_files')
        self.keep([2])
        result = self.clean(preview, expected='failed')
        self.assertEqual(result['error']['code'], 'cleanup:preview_stale')
        self.assertEqual(len(list((self.root/'images').glob('*.png'))), 2)

    def test_changed_file_and_added_candidates_invalidate_preview(self):
        target = self.file('cache/thumbnails/'+'a'*64+'.webp')
        preview = self.preview(); target.write_bytes(b'changed')
        self.clean(preview, expected='failed'); self.assertTrue(target.exists())
        preview = self.preview(); self.file('cache/thumbnails/'+'b'*64+'.webp')
        self.clean(preview, expected='failed'); self.assertTrue(target.exists())

    def test_wrong_digest_confirmation_and_extra_paths_rejected(self):
        preview = self.preview()
        base = {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'confirm': True}
        for extra, status in (({'confirm': False}, 422), ({'confirm': 1}, 422), ({'path': 'project.db'}, 422), ({'digest': '0'*64}, 409)):
            self.request('POST', self.p+'/storage/cleanup', {**base, **extra}, status=status)
        self.request('POST', self.p+'/storage/cleanup/preview', {'kind': 'database'}, status=422)
        self.request('POST', self.p+'/storage/scans', {'path': 'C:/'}, status=422)
        self.assertFalse((self.root/'.cleanup').exists())

    def test_auth_idempotency_and_project_scope(self):
        self.assertEqual(self.client.post(self.p+'/storage/scans', json={}).status_code, 428)
        self.assertEqual(self.client.get(self.p+'/storage/summary', headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        preview = self.preview()
        self.request('POST', '/api/projects/'+str(uuid4())+'/storage/cleanup', {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'confirm': True}, status=410)

    def test_project_reopen_expires_preview(self):
        preview = self.preview()
        self.runtime._close_current(); view = self.runtime.open_now(self.root)
        self.p = '/api/projects/'+view['project_id']
        self.request('POST', self.p+'/storage/cleanup', {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'confirm': True}, status=410)
        self.assertIsNone(self.request('GET', self.p+'/storage/summary')['scan'])

    def test_exports_only_completed_directories_and_keep_originals(self):
        self.import_inputs(); self.keep([1])
        record = self.run_export(self.export_preview())
        protected = self.file('exports/1/.partial-held/.export-state.json', b'{"status":"writing"}')
        loose = self.file('exports/unrelated.txt')
        preview = self.preview('exports')
        self.assertGreater(preview['count'], 2); self.assertEqual(len(preview['skipped']), 1)
        result = self.clean(preview)['result']
        self.assertEqual(result['failed'], 0, result)
        self.assertTrue(protected.exists()); self.assertTrue(loose.exists())
        self.assertEqual(len(list((self.root/'images').glob('*.png'))), 1)
        updated = self.request('GET', self.p+'/exports/'+record['export_id'])
        self.assertEqual(updated['status'], 'cleaned')
        self.request('GET', self.p+'/exports/'+record['export_id']+'/report', status=409)

    def test_partial_export_failure_keeps_receipts_and_reports(self):
        self.import_inputs(); self.keep([1]); record = self.run_export(self.export_preview())
        preview = self.preview('exports')
        original = storage_files.unlink_verified
        def remove(root, row, dirs):
            if row['path'].endswith('.png'):
                raise PermissionError('private error')
            return original(root, row, dirs)
        with patch('stylelora.services.storage.unlink_verified', side_effect=remove):
            result = self.clean(preview)['result']
        self.assertEqual(result['status'], 'partial')
        directory = self.root/record['relative_dir']
        self.assertTrue((directory/'export_report.json').exists())
        self.assertTrue((directory/'.export-state.json').exists())
        self.assertNotIn('private error', str(result))
        self.assertEqual(self.request('GET', self.p+'/exports/'+record['export_id'])['status'], 'cleanup_partial')
        self.runtime._close_current(); view = self.runtime.open_now(self.root)
        self.p = '/api/projects/'+view['project_id']
        self.assertEqual(self.request('GET', self.p+'/exports/'+record['export_id'])['status'], 'cleanup_partial')
        self.request('GET', self.p+'/exports/'+record['export_id']+'/report', status=409)

    def test_file_lease_blocks_preview_and_cleanup(self):
        self.file('cache/thumbnails/'+'a'*64+'.webp'); preview = self.preview()
        with self.runtime.context.handle._file_operations:
            accepted = self.request('POST', self.p+'/storage/cleanup/preview', {'kind': 'thumbnails'}, status=202)
            result = self.wait_operation(accepted, expected='failed')
            self.assertEqual(result['error']['code'], 'storage:leased')
            result = self.clean(preview, expected='failed')
            self.assertEqual(result['error']['code'], 'storage:leased')

    def test_deleted_file_crash_reopen_marks_missing_and_preserves_audit(self):
        self.import_inputs(); self.reject(); preview = self.preview('rejected_files')
        def fault(stage):
            if stage == 'cleanup_after_unlink':
                raise RuntimeError('simulated crash before DB commit')
        self.runtime.context.handle._fault_hook = fault
        result = self.clean(preview)['result']
        self.assertEqual(result['failed'], 1)
        self.runtime.context.handle._fault_hook = None
        self.runtime._close_current(); view = self.runtime.open_now(self.root)
        self.p = '/api/projects/'+view['project_id']
        self.assertTrue(self.request('GET', self.p+'/images/1')['file_missing'])
        self.assertTrue((self.root/result['audit']).exists())
        self.assertEqual(self.preview('rejected_files')['count'], 0)

    def test_hardlink_bytes_are_logical_and_other_link_survives(self):
        target = self.file('cache/thumbnails/'+'a'*64+'.webp')
        other = self.base/'external.webp'; os.link(target, other)
        preview = self.preview(); self.assertEqual(preview['hardlinked_files'], 1)
        result = self.clean(preview)['result']
        self.assertEqual(result['deleted'], 1, result)
        self.assertEqual(other.read_bytes(), b'fixture')

    def test_inaccessible_inventory_reports_partial_and_cannot_cleanup(self):
        self.file('cache/thumbnails/'+'a'*64+'.webp')
        real = storage_files.os.scandir
        def entries(path):
            if str(path).endswith('thumbnails'):
                raise PermissionError('private')
            return real(path)
        with patch('stylelora.services.storage_files.os.scandir', side_effect=entries):
            scan = self.scan(); self.assertTrue(scan['partial']); self.assertEqual(scan['error_count'], 1)
            accepted = self.request('POST', self.p+'/storage/cleanup/preview', {'kind': 'thumbnails'}, status=202)
            result = self.wait_operation(accepted, expected='failed')
            self.assertEqual(result['error']['code'], 'cleanup:inventory_incomplete')

    def test_candidate_limit_never_silently_truncates(self):
        self.file('cache/thumbnails/'+'a'*64+'.webp'); self.file('cache/thumbnails/'+'b'*64+'.webp')
        with patch('stylelora.services.storage.MAX_CANDIDATES', 1):
            accepted = self.request('POST', self.p+'/storage/cleanup/preview', {'kind': 'thumbnails'}, status=202)
            result = self.wait_operation(accepted, expected='failed')
        self.assertEqual(result['error']['code'], 'cleanup:too_many_files')

    def test_scan_limit_reports_lower_bound(self):
        with patch('stylelora.services.storage_files.MAX_ENTRIES', 1):
            result = self.scan()
        self.assertTrue(result['partial']); self.assertEqual(result['errors'][0]['code'], 'storage:scan_limit')

    def test_cancel_cleanup_denied_and_duplicate_keys_reuse_operation(self):
        target = self.file('cache/thumbnails/'+'a'*64+'.webp'); preview = self.preview()
        first = self.clean(preview)
        task = self.runtime.supervisor.tasks[first['operation_id']]
        self.assertFalse(task.cancelable)
        again = self.clean(preview)
        self.assertEqual(again['operation_id'], first['operation_id'])
        self.assertFalse(target.exists())

    def test_windows_junction_is_not_followed_or_deleted(self):
        import _winapi
        outside = self.base/'outside'; outside.mkdir(); target = outside/'keep.txt'; target.write_text('keep')
        link = self.root/'cache'/'thumbnails'
        _winapi.CreateJunction(str(outside), str(link))
        try:
            result = self.scan()
            self.assertTrue(result['partial'])
            accepted = self.request('POST', self.p+'/storage/cleanup/preview', {'kind': 'thumbnails'}, status=202)
            self.wait_operation(accepted, expected='failed')
            self.assertEqual(target.read_text(), 'keep')
        finally:
            link.rmdir()

    def test_windows_birth_and_write_time_are_not_confused(self):
        target = self.file('cache/thumbnails/'+'a'*64+'.webp')
        time.sleep(.02); target.write_bytes(b'later contents')
        result = self.clean(self.preview())['result']
        self.assertEqual(result['deleted'], 1, result)

    def test_expired_preview_is_not_accepted(self):
        preview = self.preview()
        registry = self.runtime.previews
        with registry.lock:
            old = registry.values[preview['preview_id']]
            registry.values[preview['preview_id']] = (0, *old[1:])
        self.request('POST', self.p+'/storage/cleanup', {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'confirm': True}, status=410)

    def test_media_read_holds_file_lease_until_bytes_are_read(self):
        from stylelora.services.media import read_media, read_bounded
        self.import_inputs(); self.file('cache/thumbnails/'+'a'*64+'.webp'); preview = self.preview()
        entered, release = threading.Event(), threading.Event()
        def read(path, maximum):
            entered.set(); release.wait(5)
            return read_bounded(path, maximum)
        output = []
        with patch('stylelora.services.media.read_bounded', side_effect=read):
            thread = threading.Thread(target=lambda: output.append(read_media(self.runtime.context, 1)))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                result = self.clean(preview, expected='failed')
                self.assertEqual(result['error']['code'], 'storage:leased')
            finally:
                release.set(); thread.join(5)
        self.assertTrue(output)

    def test_unfinished_model_job_blocks_rejected_file_deletion(self):
        from stylelora.project_db.repositories import jobs
        from stylelora.project_db.types import JobRequest, Scope
        self.import_inputs(); self.reject()
        submit_write(self.runtime.context.handle, lambda db: jobs.create_batch(db, str(uuid4()), (JobRequest('wd', 'selected', Scope(selected_ids=(1,))),))).result()
        result = self.clean(self.preview('rejected_files'))['result']
        self.assertEqual(result['results'][0]['code'], 'storage:leased')
        self.assertEqual(len(list((self.root/'images').glob('*.png'))), 1)

    def test_late_file_replacement_is_rejected_by_open_handle_identity(self):
        target = self.file('cache/thumbnails/'+'a'*64+'.webp')
        preview = self.preview()
        original = storage_files.unlink_verified
        def replace(root, row, dirs):
            target.unlink(); target.write_bytes(b'replacement')
            return original(root, row, dirs)
        with patch('stylelora.services.storage.unlink_verified', side_effect=replace):
            result = self.clean(preview)['result']
        self.assertEqual(result['failed'], 1); self.assertEqual(target.read_bytes(), b'replacement')
