import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from stylelora.exchange.exporter import export_images
from stylelora.exchange.errors import ExportInterrupted
from stylelora.project_db.storage import atomic_json
from .api_support import APITest


class ExportRecoveryTests(APITest):
    def interrupted(self):
        image = self.import_inputs()['items'][0]['image_id']; self.keep([image])
        handle = self.runtime.context.handle
        def fail(stage):
            if stage == 'export_before_publish':
                raise OSError('fixture interruption')
        handle._fault_hook = fail
        try:
            with self.assertRaises(ExportInterrupted) as raised:
                export_images(handle)
        finally:
            handle._fault_hook = None
        rel = raised.exception.relative_directory
        return self.root/rel

    def scan(self):
        return self.wait_operation(self.request('POST', self.p+'/exports/recovery/scan', {}, status=202))['result']

    def act(self, preview, action='publish', *, key=None, expected='done'):
        body = {'preview_id': preview['preview_id'], 'digest': preview['digest'],
                'candidate_id': preview['candidates'][0]['candidate_id'], 'action': action, 'confirm': True}
        accepted = self.request('POST', self.p+'/exports/recovery/actions', body, version=preview['version'], key=key, status=202)
        return self.wait_operation(accepted, expected=expected)

    def test_complete_staging_is_verified_then_published_once(self):
        partial = self.interrupted(); report = json.loads((partial/'export_report.json').read_text(encoding='utf-8'))
        preview = self.scan()
        self.assertTrue(preview['candidates'][0]['can_publish'], preview)
        self.assertTrue(partial.is_dir())
        key = str(uuid4())
        done = self.act(preview, key=key)
        self.assertEqual(done['result']['status'], 'published')
        self.assertFalse(partial.exists())
        target = self.root/report['directory']
        self.assertTrue(target.is_dir())
        self.assertEqual(self.act(preview, key=key)['operation_id'], done['operation_id'])
        self.assertEqual(self.request('GET', self.p+'/exports/'+report['export_id'])['status'], 'finished')
        self.assertEqual(self.request('GET', self.p+'/exports/recovery/history')['items'][0]['observed'], 'moved')

    def test_unrecognized_directory_can_only_archive_and_preserves_bytes(self):
        unknown = self.root/'exports/1/unknown'
        unknown.mkdir(parents=True); (unknown/'user.txt').write_bytes(b'preserve me')
        preview = self.scan()
        self.assertFalse(preview['candidates'][0]['can_publish'])
        result = self.act(preview, 'archive')['result']
        self.assertEqual(result['deleted_files'], 0)
        self.assertFalse(unknown.exists())
        self.assertEqual((self.root/result['directory']/'user.txt').read_bytes(), b'preserve me')
        self.assertEqual(self.scan()['decisions'][0]['observed'], 'moved')

    def test_changed_bytes_after_preview_block_move(self):
        partial = self.interrupted(); preview = self.scan()
        next(partial.glob('*.txt')).write_text('changed caption', encoding='utf-8')
        failed = self.act(preview, expected='failed')
        self.assertEqual(failed['error']['code'], 'export:recovery_stale')
        self.assertTrue(partial.exists())
        self.assertFalse((self.root/'exports/.recovery').exists())

    def test_extra_files_mismatched_report_and_foreign_project_block_publication(self):
        partial = self.interrupted()
        (partial/'unclaimed.bin').write_bytes(b'keep')
        self.assertFalse(self.scan()['candidates'][0]['can_publish'])
        (partial/'unclaimed.bin').unlink()
        report = json.loads((partial/'export_report.json').read_text(encoding='utf-8'))
        report['project']['created_at'] = '2000-01-01T00:00:00Z'
        atomic_json(partial/'export_report.json', report)
        self.assertFalse(self.scan()['candidates'][0]['can_publish'])
        self.assertTrue(partial.exists())

    def test_destination_collision_does_not_overwrite(self):
        partial = self.interrupted(); preview = self.scan()
        target = self.root/preview['candidates'][0]['publish_directory']; target.mkdir()
        (target/'mine').write_bytes(b'mine')
        failed = self.act(preview, expected='failed')
        self.assertEqual(failed['error']['code'], 'export:destination_exists')
        self.assertEqual((target/'mine').read_bytes(), b'mine')
        self.assertTrue(partial.exists())

    def test_lost_rename_ack_is_resolved_by_directory_identity(self):
        partial = self.interrupted(); preview = self.scan()
        original = Path.rename
        def uncertain(source, target):
            original(source, target)
            raise OSError('lost confirmation')
        with patch.object(Path, 'rename', uncertain):
            result = self.act(preview)['result']
        self.assertEqual(result['status'], 'published')
        self.assertFalse(partial.exists())

    def test_confirmation_version_and_path_fields_are_strict(self):
        self.interrupted(); preview = self.scan()
        body = {'preview_id': preview['preview_id'], 'digest': preview['digest'],
                'candidate_id': preview['candidates'][0]['candidate_id'], 'action': 'publish', 'confirm': True}
        self.request('POST', self.p+'/exports/recovery/actions', body, status=428)
        self.request('POST', self.p+'/exports/recovery/actions', body, version='old', status=409)
        for changed in ({**body, 'confirm': 1}, {**body, 'path': 'elsewhere'}, {**body, 'action': 'delete'}):
            self.request('POST', self.p+'/exports/recovery/actions', changed, version=preview['version'], status=422)

    def test_hardlinked_output_requires_new_export_and_can_only_archive(self):
        import os
        partial = self.interrupted()
        caption = next(partial.glob('*.txt'))
        os.link(caption, self.base/'shared-caption.txt')
        candidate = self.scan()['candidates'][0]
        self.assertFalse(candidate['can_publish'])
        self.assertEqual(candidate['reason'], 'export:recovery_hardlink_requires_new_export')

    def test_malformed_history_fails_with_actionable_error_and_preserves_file(self):
        from uuid import uuid4
        from stylelora.project_db.validation import digest
        root = self.root/'exports/.recovery'; root.mkdir()
        path = root/(str(uuid4())+'.json')
        atomic_json(path, {'content': {}, 'digest': digest({})})
        result = self.request('GET', self.p+'/exports/recovery/history', status=409)
        self.assertEqual(result['error']['code'], 'export:recovery_record_corrupt')
        self.assertTrue(path.exists())
