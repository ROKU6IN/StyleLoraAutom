from contextlib import contextmanager
import os
import threading
from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import images
from stylelora.services.verified_file import verified_file
from .api_support import APITest


class ImageVerificationTests(APITest):
    def setUp(self):
        super().setUp()
        self.import_inputs()
        self.file = self.root/'images'/self.row()['filename']
        self.original = self.file.read_bytes()
        self.url = self.p+'/images/1/verify-file'

    def row(self):
        return self.request('GET', self.p+'/images/1')

    def missing(self):
        submit_write(self.runtime.context.handle, lambda db: images.set_file_missing(db, 1, missing=True)).result()
        return self.row()

    def verify(self, *, expected='done', version=None, key=None):
        accepted = self.request('POST', self.url, {}, version=version or self.row()['version'], status=202, key=key)
        return self.wait_operation(accepted, expected=expected)

    def sql(self, statement):
        with read_snapshot(self.runtime.context.handle) as db:
            return [tuple(row) for row in db.execute(statement)]

    def test_restore_preserves_review_rating_annotations_and_original_bytes(self):
        self.keep([1])
        missing = self.missing()
        annotations = self.sql('SELECT * FROM annotations')
        result = self.verify()['result']
        self.assertTrue(result['changed'])
        self.assertFalse(result['image']['file_missing'])
        self.assertEqual(result['image']['review_status'], 'keep')
        self.assertNotEqual(result['image']['version'], missing['version'])
        self.assertEqual(result['verified_sha256'], missing['sha256'])
        self.assertEqual(result['verified_bytes'], len(self.original))
        self.assertEqual(self.sql('SELECT * FROM annotations'), annotations)
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertEqual(self.client.get(self.p+'/images/1/content').content, self.original)

    def test_actual_missing_wrong_size_and_same_size_wrong_hash_never_clear_flag(self):
        self.missing()
        for contents, reason in ((None, 'image:file_missing'), (b'wrong', 'image:hash_mismatch'),
                                 (b'X'+self.original[1:], 'image:hash_mismatch')):
            with self.subTest(reason=reason):
                if contents is None:
                    self.file.unlink()
                else:
                    self.file.write_bytes(contents)
                value = self.verify(expected='failed')
                self.assertEqual(value['error']['code'], reason)
                self.assertTrue(self.row()['file_missing'])
        self.file.write_bytes(self.original)
        self.assertTrue(self.verify()['result']['changed'])

    def test_verified_reject_remains_rejected_until_explicit_review(self):
        row = self.row()
        self.request('POST', self.p+'/images/review', {'ids': [1], 'target': 'reject', 'expected_versions': {'1': row['version']}})
        self.missing()
        result = self.verify()['result']['image']
        self.assertEqual(result['review_status'], 'reject')
        self.keep([1])
        self.assertEqual(self.row()['review_status'], 'keep')

    def test_noop_still_verifies_file_without_version_change_or_event(self):
        before = self.row()
        with patch.object(self.runtime, 'emit', wraps=self.runtime.emit) as emit:
            value = self.verify()['result']
        self.assertFalse(value['changed'])
        self.assertEqual(value['image']['version'], before['version'])
        self.assertFalse(any(call.args[0] == 'image.updated' for call in emit.call_args_list))
        self.file.write_bytes(b'bad')
        self.verify(expected='failed')

    def test_auth_body_queries_missing_and_stale_versions(self):
        row = self.missing()
        self.assertEqual(self.client.post(self.url, json={}, headers={'Authorization': 'Bearer bad'}).status_code, 401)
        self.assertEqual(self.client.post(self.url, json={}, headers={'If-Match': '"'+row['version']+'"'}).status_code, 428)
        self.request('POST', self.url, {}, status=428)
        self.request('POST', self.url, {}, version='stale', status=409)
        for body in ({'path': 'C:/outside.png'}, {'sha256': row['sha256']}, {'confirm': True}, {'file_missing': False}):
            self.request('POST', self.url, body, version=row['version'], status=422)
        self.request('POST', self.url+'?path=images/1.png', {}, version=row['version'], status=422)
        self.request('POST', self.p+'/images/999/verify-file', {}, version=row['version'], status=404)
        self.request('POST', '/api/projects/'+str(uuid4())+'/images/1/verify-file', {}, version=row['version'], status=410)
        self.assertTrue(self.row()['file_missing'])

    def test_idempotent_replay_uses_original_operation_and_binds_if_match(self):
        version = self.missing()['version']
        key = str(uuid4())
        first = self.verify(version=version, key=key)
        self.assertEqual(self.verify(version=version, key=key)['operation_id'], first['operation_id'])
        self.request('POST', self.url, {}, version=self.row()['version'], key=key, status=409)
        self.request('POST', self.url, {}, version=version, status=409)

    def test_failure_replay_does_not_hash_or_create_new_task(self):
        version = self.missing()['version']
        self.file.unlink()
        key = str(uuid4())
        first = self.verify(expected='failed', version=version, key=key)
        self.file.write_bytes(self.original)
        with patch('stylelora.services.image_verification.verified_file', side_effect=AssertionError('no replay')):
            again = self.verify(expected='failed', version=version, key=key)
        self.assertEqual(first['operation_id'], again['operation_id'])
        self.assertTrue(self.row()['file_missing'])
        self.verify()

    def test_file_lease_conflict_does_not_block_worker_or_clear_missing(self):
        self.missing()
        with self.runtime.context.handle._file_operations:
            result = self.verify(expected='failed')
        self.assertEqual(result['error']['code'], 'storage:leased')
        self.assertTrue(self.row()['file_missing'])

    def held_verification(self, entered, release):
        @contextmanager
        def hold(*args):
            with verified_file(*args) as check:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test timed out')
                yield check
        return hold

    def test_review_race_is_rechecked_in_writer_transaction(self):
        row = self.missing()
        entered, release = threading.Event(), threading.Event()
        with patch('stylelora.services.image_verification.verified_file', self.held_verification(entered, release)):
            accepted = self.request('POST', self.url, {}, version=row['version'], status=202)
            try:
                self.assertTrue(entered.wait(5))
                self.request('POST', self.p+'/images/review', {'ids': [1], 'target': 'reject', 'expected_versions': {'1': row['version']}})
            finally:
                release.set()
            value = self.wait_operation(accepted, expected='failed')
        self.assertEqual(value['error']['code'], 'concurrency:version_conflict')
        self.assertTrue(self.row()['file_missing'])

    def test_canceled_verification_does_not_publish_availability(self):
        row = self.missing()
        entered, release = threading.Event(), threading.Event()
        with patch('stylelora.services.image_verification.verified_file', self.held_verification(entered, release)):
            accepted = self.request('POST', self.url, {}, version=row['version'], status=202)
            try:
                self.assertTrue(entered.wait(5))
                op = self.runtime.supervisor.get(accepted['task']['id'])
                self.runtime.supervisor.cancel_task(op['operation_id'], op['version'])
            finally:
                release.set()
            self.wait_operation(accepted, expected='canceled')
        self.assertTrue(self.row()['file_missing'])

    def test_project_close_drains_file_handle_and_cancels_before_commit(self):
        row = self.missing()
        entered, release = threading.Event(), threading.Event()
        with patch('stylelora.services.image_verification.verified_file', self.held_verification(entered, release)):
            accepted = self.request('POST', self.url, {}, version=row['version'], status=202)
            self.assertTrue(entered.wait(5))
            closing = threading.Thread(target=self.runtime._close_current)
            closing.start()
            try:
                self.assertTrue(closing.is_alive())
            finally:
                release.set()
                closing.join(5)
            self.assertFalse(closing.is_alive())
        self.runtime.open_now(self.root)
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertTrue(images.get_image(db, 1).file_missing)
        self.assertEqual(self.runtime.supervisor.tasks[accepted['task']['id']].status, 'canceled')

    def test_commit_failure_retains_missing_and_publishes_no_image_event(self):
        self.missing()
        def fail(stage):
            if stage == 'before_commit':
                raise OSError('private path')
        self.runtime.context.handle._fault_hook = fail
        with patch.object(self.runtime, 'emit', wraps=self.runtime.emit) as emit:
            result = self.verify(expected='failed')
        self.runtime.context.handle._fault_hook = None
        self.assertTrue(self.row()['file_missing'])
        self.assertNotIn('private path', str(result))
        self.assertFalse(any(call.args[0] == 'image.updated' for call in emit.call_args_list))

    def test_lost_commit_reply_is_unknown_and_read_reconciles_without_replay(self):
        version = self.missing()['version']
        def fail(stage):
            if stage == 'after_commit':
                raise RuntimeError('lost acknowledgement')
        self.runtime.context.handle._fault_hook = fail
        key = str(uuid4())
        result = self.verify(expected='failed', version=version, key=key)
        self.runtime.context.handle._fault_hook = None
        self.assertEqual(result['error']['outcome'], 'unknown')
        self.assertFalse(self.row()['file_missing'])
        self.assertEqual(self.verify(expected='failed', version=version, key=key)['operation_id'], result['operation_id'])

    def test_success_event_contains_id_only_and_version_avoids_aba(self):
        first = self.missing()['version']
        with patch.object(self.runtime, 'emit', wraps=self.runtime.emit) as emit:
            self.verify()
        events = [call for call in emit.call_args_list if call.args[0] == 'image.updated']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].args[1], {'image_ids': [1], 'fields': ['file_missing']})
        self.assertNotIn(str(self.root), str(events))
        self.assertNotEqual(self.missing()['version'], first)
        self.request('POST', self.url, {}, version=first, status=409)

    def test_reopened_project_retains_verified_flag(self):
        self.missing()
        self.verify()
        self.runtime._close_current()
        current = self.runtime.open_now(self.root)
        self.assertFalse(self.request('GET', '/api/projects/'+current['project_id']+'/images/1')['file_missing'])

    def test_oversize_and_permission_errors_preserve_missing(self):
        self.missing()
        with patch('stylelora.services.verified_file.MAX_BYTES', 1):
            self.assertEqual(self.verify(expected='failed')['error']['code'], 'image:too_large')
        with patch('stylelora.services.image_verification.verified_file', side_effect=PermissionError('secret')):
            value = self.verify(expected='failed')
        self.assertEqual(value['error']['code'], 'image:file_unreadable')
        self.assertNotIn('secret', str(value))
        self.assertTrue(self.row()['file_missing'])

    def test_windows_handle_blocks_writes_and_unlink_until_commit(self):
        if os.name != 'nt':
            self.skipTest('Windows share-mode guarantee')
        self.missing()
        def attempt(stage):
            if stage == 'before_commit':
                with self.assertRaises(OSError):
                    self.file.write_bytes(b'changed')
                with self.assertRaises(OSError):
                    self.file.unlink()
                with self.assertRaises(OSError):
                    self.file.parent.rename(self.root/'replaced')
        self.runtime.context.handle._fault_hook = attempt
        self.verify()
        self.runtime.context.handle._fault_hook = None
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_existing_writer_handle_is_rejected(self):
        if os.name != 'nt':
            self.skipTest('Windows share-mode guarantee')
        self.missing()
        with self.file.open('r+b'):
            value = self.verify(expected='failed')
        self.assertEqual(value['error']['code'], 'image:file_busy')
        self.assertTrue(self.row()['file_missing'])

    def test_linked_image_directory_is_rejected_without_reading_target(self):
        if os.name != 'nt':
            self.skipTest('Windows junction fixture')
        import subprocess
        self.missing()
        original_dir = self.root/'images'
        moved = self.base/'held-images'
        original_dir.rename(moved)
        try:
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(original_dir), str(moved)], check=True, capture_output=True)
            value = self.verify(expected='failed')
            self.assertEqual(value['error']['code'], 'path:unsafe')
            self.assertTrue(self.row()['file_missing'])
        finally:
            if original_dir.is_junction():
                original_dir.rmdir()
            moved.rename(original_dir)
