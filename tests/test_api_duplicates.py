import hashlib
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from stylelora.imaging.phash import candidate_rows
from stylelora.project_db.connection import read_snapshot, submit_write
from .api_support import APITest


class DuplicateAPITests(APITest):
    def setUp(self):
        super().setUp()
        self.runtime.duplicates.temp_root = self.base/'scan-results'
        self.url = self.p + '/duplicates/scans'

    def start(self, threshold=1, scope=None, key=None):
        return self.request('POST', self.url, {'scope': scope or {'filter': {}}, 'threshold': threshold}, key=key, status=202)

    def finish(self, accepted, expected='done'):
        self.wait_operation({'status_url': accepted['operation_url']}, expected=expected)
        return self.request('GET', accepted['status_url'])

    def hashes(self, values):
        self.import_inputs(len(values))
        rows = self.request('GET', self.p + '/images')['items']
        submit_write(self.runtime.context.handle, lambda db: [db.execute('UPDATE images SET phash=? WHERE image_id=?', (value, row['image_id'])) for row, value in zip(rows, values)]).result()
        return rows

    def test_snapshot_pagination_unique_pairs_and_no_project_writes(self):
        rows = self.hashes(['0'*16, '0000000000000001', '0000000000000003'])
        with read_snapshot(self.runtime.context.handle) as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM images ORDER BY image_id')]
            jobs = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        content = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'images').glob('*.png')}
        accepted = self.start()
        self.finish(accepted)
        page = self.request('GET', accepted['status_url'] + '?limit=1')
        self.assertEqual(page['items'], [{'left_id': 1, 'right_id': 2, 'distance': 1}])
        self.assertEqual(page['input_count'], 3)
        second = self.request('GET', accepted['status_url'] + '?limit=1&cursor=' + page['next_cursor'])
        self.assertEqual(second['items'], [{'left_id': 2, 'right_id': 3, 'distance': 1}])
        self.assertFalse(second['has_more'])
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM images ORDER BY image_id')])
            self.assertEqual(jobs, db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        self.assertEqual(content, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'images').glob('*.png')})
        self.keep([rows[0]['image_id']])
        self.assertEqual(self.request('GET', accepted['status_url'] + '?limit=1')['next_cursor'], page['next_cursor'])

    def test_threshold_mandatory_strict_scope_and_limits(self):
        for body in ({'scope': {'filter': {}}}, {'threshold': True, 'scope': {'filter': {}}},
                     {'threshold': 65, 'scope': {'filter': {}}}, {'threshold': -1, 'scope': {'filter': {}}},
                     {'threshold': 1.0, 'scope': {'filter': {}}}, {'threshold': 1, 'scope': {}},
                     {'threshold': 1, 'scope': {'filter': {}, 'selected_ids': []}},
                     {'threshold': 1, 'scope': {'selected_ids': [1, 1]}},
                     {'threshold': 1, 'scope': {'path': 'untrusted'}}):
            self.request('POST', self.url, body, status=422)
        self.request('POST', self.url, {'scope': {'selected_ids': list(range(1, 10002))}, 'threshold': 1}, status=413)
        self.request('POST', self.url, {'scope': {'selected_ids': [999]}, 'threshold': 1}, status=404)

    def test_skip_null_bad_hash_missing_and_size_changed(self):
        rows = self.hashes(['0'*16, None, 'invalid', 'f'*16, '0'*16])
        (self.root/'images'/rows[0]['filename']).unlink()
        (self.root/'images'/rows[3]['filename']).write_bytes(b'changed')
        result = self.finish(self.start(64))
        self.assertEqual(result['skipped'], {'missing': 1, 'no_hash': 1, 'invalid_hash': 1, 'size_changed': 1})
        self.assertEqual(result['eligible'], 1)
        self.assertEqual(result['items'], [])
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertEqual(db.execute('SELECT SUM(file_missing) FROM images').fetchone()[0], 0)

    def test_scope_defaults_exclude_rejected_selected_is_explicit(self):
        rows = self.hashes(['0'*16]*3)
        image = rows[0]
        self.request('POST', self.p+'/images/review', {'ids': [image['image_id']], 'target': 'reject', 'expected_versions': {str(image['image_id']): image['version']}})
        ordinary = self.finish(self.start(0))
        self.assertEqual(ordinary['eligible'], 2)
        chosen = self.finish(self.start(0, {'selected_ids': [r['image_id'] for r in rows]}))
        self.assertEqual(chosen['eligible'], 3)
        self.assertEqual(chosen['upper_id'], 3)
        self.assertEqual(self.request('GET', self.p + '/images/1')['review_status'], 'reject')

    def test_idempotency_cursor_isolation_and_delete_cas(self):
        self.hashes(['0'*16]*3)
        key = str(uuid4())
        accepted = self.start(0, key=key)
        result = self.finish(accepted)
        self.assertEqual(accepted['scan_id'], self.start(0, key=key)['scan_id'])
        self.request('POST', self.url, {'scope': {'filter': {}}, 'threshold': 1}, key=key, status=409)
        page = self.request('GET', accepted['status_url']+'?limit=1')
        other = self.start(0)
        self.finish(other)
        self.request('GET', other['status_url']+'?cursor='+page['next_cursor'], status=422)
        self.request('DELETE', accepted['status_url'], status=428)
        self.request('DELETE', accepted['status_url'], version='stale', status=409)
        directory = self.runtime.duplicates.scans[accepted['scan_id']].directory.name
        self.request('DELETE', accepted['status_url'], version=result['version'], status=204)
        from pathlib import Path
        self.assertFalse(Path(directory).exists())
        self.request('GET', accepted['status_url'], status=410)
        self.assertEqual(len(self.request('GET', self.p+'/images')['items']), 3)

    def test_result_limit_fails_explicitly_without_partial_results(self):
        self.hashes(['0'*16]*3)
        self.runtime.duplicates.max_bytes = 16
        accepted = self.start(64)
        view = self.finish(accepted, expected='failed')
        self.assertEqual(view['reason'], 'duplicates:result_limit')
        self.assertEqual(view['items'], [])
        self.assertFalse(view['has_more'])
        self.assertIsNone(self.runtime.duplicates.scans[accepted['scan_id']].directory)

    def test_delete_running_scan_cancels_and_releases(self):
        self.hashes(['0'*16]*3)
        started, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            started.set()
            release.wait(5)
            yield from candidate_rows(*args, **kwargs)
        with patch('stylelora.services.duplicates.candidate_rows', side_effect=slow):
            try:
                accepted = self.start(64)
                self.assertTrue(started.wait(2))
                scan = self.runtime.duplicates.scans[accepted['scan_id']]
                view = self.request('GET', accepted['status_url'])
                self.assertEqual(view['items'], [])
                self.request('DELETE', accepted['status_url'], version=view['version'], status=204)
            finally:
                release.set()
            self.wait_operation({'status_url': accepted['operation_url']}, expected='canceled')
            self.assertIsNone(scan.directory)

    def test_expiry_and_maximum_live_results(self):
        scans = [self.start(0) for _ in range(3)]
        for scan in scans:
            self.finish(scan)
        self.request('POST', self.url, {'scope': {'filter': {}}, 'threshold': 0}, status=429)
        expired = self.runtime.duplicates.scans[scans[0]['scan_id']]
        expired.deadline = time.monotonic()-1
        self.request('GET', scans[0]['status_url'], status=410)
        self.assertIsNone(expired.directory)
        self.assertEqual(self.finish(self.start(0))['upper_id'], 0)

    def test_project_close_invalidates_and_cleans_results(self):
        self.import_inputs(2)
        accepted = self.start(64)
        self.finish(accepted)
        scan = self.runtime.duplicates.scans[accepted['scan_id']]
        project = self.request('GET', self.p)
        closing = self.request('POST', self.p+'/close', {}, version=project['version'], status=202)
        self.wait_operation(closing)
        self.assertIsNone(scan.directory)
        self.assertEqual(self.runtime.duplicates.scans, {})
        self.request('GET', accepted['status_url'], status=410)

    def test_scope_is_frozen_before_worker_and_result_list_survives_navigation(self):
        self.hashes(['0'*16]*3)
        started, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(5)
            yield from candidate_rows(*args, **kwargs)
        with patch('stylelora.services.duplicates.candidate_rows', side_effect=delayed):
            try:
                accepted = self.start(0)
                self.assertTrue(started.wait(2))
                submit_write(self.runtime.context.handle, lambda db: db.execute("UPDATE images SET phash='ffffffffffffffff',review_status='reject' WHERE image_id=1")).result()
            finally:
                release.set()
            self.assertEqual(self.finish(accepted)['upper_id'], 3)
        listing = self.request('GET', self.url)
        self.assertEqual(listing['items'][0]['scan_id'], accepted['scan_id'])
        self.assertEqual(listing['max_images'], 10000)
        self.assertEqual(self.finish(self.start(0))['upper_id'], 1)

    def test_filtered_scope_over_capacity_is_rejected_before_task(self):
        self.import_inputs()
        def fill(db):
            row = dict(db.execute('SELECT * FROM images LIMIT 1').fetchone())
            db.executemany('INSERT INTO images(filename,width,height,phash,sha256,file_size,source_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                           [(f'{i}.png', row['width'], row['height'], row['phash'], row['sha256'], row['file_size'], row['source_id'], row['created_at']) for i in range(2, 10002)])
        submit_write(self.runtime.context.handle, fill).result()
        self.request('POST', self.url, {'scope': {'filter': {}}, 'threshold': 0}, status=413)
        self.assertEqual(self.runtime.duplicates.scans, {})

    def test_unavailable_result_file_returns_safe_error(self):
        self.hashes(['0'*16]*2)
        accepted = self.start(0)
        self.finish(accepted)
        scan = self.runtime.duplicates.scans[accepted['scan_id']]
        from pathlib import Path
        (Path(scan.directory.name)/'0.pairs').unlink()
        self.assertEqual(self.request('GET', accepted['status_url'], status=503)['error']['code'], 'duplicates:result_unavailable')
