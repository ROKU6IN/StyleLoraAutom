import errno
import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from stylelora.annotations.editing import edit_manual
from stylelora.exchange.errors import ExchangeError, ExportInterrupted
from stylelora.exchange.exporter import export_images, preview_export
from stylelora.exchange.hardlink import hardlink_verified
from stylelora.project_db.errors import PreviewStale, UnsupportedScheme
from stylelora.project_db.project import close_project
from stylelora.project_db.repositories import schemes
from stylelora.project_db.types import ExportSchemeInput
from .api_support import APITest
from .exchange_support import ExchangeTest


class HardlinkTests(ExchangeTest):
    def scheme(self, **output):
        return self.write(schemes.create_export_scheme, ExportSchemeInput('硬链接', output_config_json={
            'schema_version': 1, 'image_mode': 'hardlink', **output}))

    def export(self, scheme, **kwargs):
        preview = preview_export(self.project, scheme_id=scheme.scheme_id)
        return export_images(self.project, scheme_id=scheme.scheme_id,
                             hardlink_ack_digest=preview['hardlink_ack_digest'], **kwargs)

    def test_same_file_caption_json_new_directories_and_unchanged_database(self):
        image = self.kept(metadata={'Description': 'metadata'})
        source = self.root / 'images' / image.filename
        original = source.read_bytes()
        scheme = self.scheme(subdirectory='训练/data', include_json=True)
        before = self.database_contents()
        preview = preview_export(self.project, scheme_id=scheme.scheme_id)
        self.assertIn('export:hardlink_shared_content', preview['warnings'])
        self.assertFalse(list((self.root / 'exports').iterdir()))
        first, second = self.export(scheme), self.export(scheme)
        self.assertNotEqual(first['directory'], second['directory'])
        for report in (first, second):
            self.assertEqual(report['counts']['exported'], 1)
            target = self.root / report['directory'] / report['images'][0]['output_path']
            self.assertTrue(os.path.samefile(source, target))
            self.assertEqual(target.with_suffix('.txt').read_text(encoding='utf-8'), 'my style, blue hair, smile\n')
            self.assertEqual(json.loads(target.with_suffix('.json').read_text(encoding='utf-8'))['Description'], 'metadata')
            self.assertEqual(report['hardlink_ack_digest'], preview['plan_digest'])
            for file in report['images'][0]['files']:
                data = (self.root / report['directory'] / file['path']).read_bytes()
                self.assertEqual(file['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.database_contents(), before)

    def test_shared_content_risk_is_real_while_default_copy_is_independent(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        original = source.read_bytes()
        copied = export_images(self.project)
        copy_path = self.root / copied['directory'] / copied['images'][0]['output_path']
        self.assertFalse(os.path.samefile(source, copy_path))
        copy_path.write_bytes(b'edited copy')
        self.assertEqual(source.read_bytes(), original)
        linked = self.export(self.scheme())
        target = self.root / linked['directory'] / linked['images'][0]['output_path']
        target.write_bytes(b'edited shared content')
        self.assertEqual(source.read_bytes(), b'edited shared content')
        target.unlink()
        self.assertTrue(source.is_file())

    def test_confirmation_is_bound_to_current_plan_before_any_directory_write(self):
        image = self.kept()
        scheme = self.scheme()
        preview = preview_export(self.project, scheme_id=scheme.scheme_id)
        for ack in (None, '0' * 64):
            with self.assertRaisesRegex(ExchangeError, 'hardlink_confirmation_required'):
                export_images(self.project, scheme_id=scheme.scheme_id, hardlink_ack_digest=ack)
        edit_manual(self.project, image.image_id, tags=('updated',))
        with self.assertRaisesRegex(ExchangeError, 'hardlink_confirmation_required'):
            export_images(self.project, scheme_id=scheme.scheme_id, hardlink_ack_digest=preview['hardlink_ack_digest'])
        with self.assertRaises(PreviewStale):
            export_images(self.project, hardlink_ack_digest=preview['hardlink_ack_digest'])
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_empty_plan_still_requires_confirmation(self):
        scheme = self.scheme()
        with self.assertRaisesRegex(ExchangeError, 'hardlink_confirmation_required'):
            export_images(self.project, scheme_id=scheme.scheme_id)
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_hardlink_with_pipeline_stays_unsupported(self):
        scheme = self.write(schemes.create_export_scheme, ExportSchemeInput('chain',
            output_config_json={'schema_version': 1, 'image_mode': 'hardlink'},
            pipeline_json=({'processor': 'metadata_strip', 'params': {}},)))
        with self.assertRaises(UnsupportedScheme):
            preview_export(self.project, scheme_id=scheme.scheme_id)
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_link_failure_reports_specific_reason_and_never_falls_back(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        scheme = self.scheme()
        for error, code in ((OSError(errno.EXDEV, 'fixture'), 'export:hardlink_cross_volume'),
                            (OSError(errno.ENOTSUP, 'fixture'), 'export:hardlink_unsupported'),
                            (NotImplementedError('fixture'), 'export:hardlink_unsupported')):
            with self.subTest(error=error), patch('stylelora.exchange.hardlink.os.link', side_effect=error), \
                    patch('stylelora.exchange.exporter.copy_verified') as copier:
                result = self.export(scheme)
                self.assertEqual(result['reason_counts'], {code: 1})
                self.assertEqual(result['counts']['exported'], 0)
                self.assertEqual(result['images'][0]['files'], [])
                self.assertFalse(list((self.root / result['directory']).rglob('*.png')))
                copier.assert_not_called()
                self.assertTrue(source.is_file())

    def test_precheck_rejects_cross_volume_without_attempting_link(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        target = self.inputs / 'link.png'
        original_stat = Path.stat
        device = source.stat().st_dev
        def stat_fixture(path, *args, **kwargs):
            return SimpleNamespace(st_dev=device + 1) if path == target.parent else original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat_fixture), patch('stylelora.exchange.hardlink.os.link') as link:
            with self.assertRaisesRegex(ExchangeError, 'hardlink_cross_volume'):
                hardlink_verified(source, target, image.sha256, image.file_size)
            link.assert_not_called()
        self.assertFalse(target.exists())

    def test_exclusive_target_and_failed_hash_cleanup_preserve_source(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        original = source.read_bytes()
        target = self.inputs / 'existing.png'
        target.write_bytes(b'existing')
        with self.assertRaisesRegex(ExchangeError, 'export:destination_exists'):
            hardlink_verified(source, target, image.sha256, image.file_size)
        self.assertEqual(target.read_bytes(), b'existing')
        target.unlink()
        with self.assertRaisesRegex(ExchangeError, 'input:changed'):
            hardlink_verified(source, target, '0' * 64, image.file_size)
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), original)

    def test_caption_failure_removes_link_and_does_not_modify_source(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        original, links = source.read_bytes(), source.stat().st_nlink
        self.fail_at('export_after_caption')
        report = self.export(self.scheme())
        self.assertEqual(report['counts']['failed'], 1)
        self.assertFalse(list((self.root / report['directory']).rglob('*.png')))
        self.assertFalse(list((self.root / report['directory']).rglob('*.txt')))
        self.assertEqual((source.read_bytes(), source.stat().st_nlink), (original, links))

    def test_disk_full_cancel_and_publish_failure_leave_only_interrupted_evidence(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        original, links = source.read_bytes(), source.stat().st_nlink
        scheme = self.scheme()
        for failure in ('full', 'cancel', 'publish'):
            cancel = [False]
            def fault(stage):
                if stage == 'export_after_image_copy' and failure == 'full':
                    raise OSError(errno.ENOSPC, 'fixture')
                if stage == 'export_after_caption' and failure == 'cancel':
                    cancel[0] = True
                if stage == 'export_before_publish' and failure == 'publish':
                    raise OSError('fixture')
            self.project._fault_hook = fault
            with self.subTest(failure=failure), self.assertRaises(ExportInterrupted) as caught:
                self.export(scheme, cancelled=lambda: cancel[0])
            partial = self.root / caught.exception.relative_directory
            receipt = json.loads((partial / '.export-state.json').read_text())
            self.assertEqual((receipt['image_mode'], receipt['status']), ('hardlink', 'interrupted'))
            self.assertFalse((self.root / receipt['directory']).exists())
            self.assertFalse(list(partial.rglob('*.png')))
            self.assertEqual((source.read_bytes(), source.stat().st_nlink), (original, links))

    def test_source_changes_before_publish_are_detected(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        scheme = self.scheme()
        def fault(stage):
            if stage == 'export_before_publish':
                source.write_bytes(b'changed externally')
        self.project._fault_hook = fault
        with self.assertRaises(ExportInterrupted) as caught:
            self.export(scheme)
        self.assertEqual(caught.exception.code, 'input:changed')
        self.assertEqual(source.read_bytes(), b'changed externally')
        self.assertFalse(list((self.root / caught.exception.relative_directory).rglob('*.png')))

    def test_cleanup_failure_is_recorded_without_publishing_or_deleting_source(self):
        image = self.kept()
        scheme = self.scheme()
        self.fail_at('export_before_publish')
        with patch('stylelora.exchange.exporter.remove_owned', side_effect=PermissionError('fixture')):
            with self.assertRaises(ExportInterrupted) as caught:
                self.export(scheme)
        partial = self.root / caught.exception.relative_directory
        receipt = json.loads((partial / '.export-state.json').read_text())
        self.assertTrue(receipt['hardlink_cleanup_pending'])
        self.assertFalse((self.root / receipt['directory']).exists())
        self.assertTrue(os.path.samefile(self.root / 'images' / image.filename, next(partial.glob('*.png'))))

    def test_identical_bytes_source_replacement_is_not_a_valid_hardlink(self):
        image = self.kept()
        source = self.root / 'images' / image.filename
        def fault(stage):
            if stage == 'export_before_publish':
                replacement = self.inputs / 'replacement.png'
                replacement.write_bytes(source.read_bytes())
                replacement.replace(source)
        self.project._fault_hook = fault
        with self.assertRaises(ExportInterrupted) as caught:
            self.export(self.scheme())
        self.assertEqual(caught.exception.code, 'export:hardlink_changed')
        self.assertTrue(source.is_file())

    def test_cli_requires_explicit_risk_acknowledgement(self):
        from stylelora.cli import main
        image = self.kept()
        scheme = self.scheme()
        close_project(self.project)
        args = ['export', str(self.root), '--scheme-id', str(scheme.scheme_id)]
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(args), 1)
        self.assertIn('hardlink_confirmation_required', error.getvalue())
        self.assertFalse(list((self.root / 'exports').iterdir()))
        with redirect_stdout(output):
            self.assertEqual(main([*args, '--acknowledge-hardlink']), 0)
        report = json.loads(output.getvalue())
        target = self.root / report['directory'] / report['images'][0]['output_path']
        self.assertTrue(os.path.samefile(self.root / 'images' / image.filename, target))
        self.reopen()


class HardlinkAPITests(APITest):
    def scheme(self, **config):
        return self.request('POST', self.p + '/export-schemes', {
            'name': '硬链接', 'output_config_json': {'image_mode': 'hardlink'}, **config}, status=201)

    def preview(self, scheme):
        job = self.request('POST', self.p + '/exports/preview', {'scheme_id': scheme['scheme_id']}, status=202)
        return self.wait_operation(job)['result']

    def test_confirmed_http_export_report_and_recovered_history(self):
        self.import_inputs(); self.keep([1])
        caps = self.request('GET', '/api/exchange/capabilities')
        self.assertEqual(caps['default_export']['output_config_json']['image_mode'], 'copy')
        self.assertIn('hardlink', caps['output_modes'])
        scheme = self.scheme()
        self.assertTrue(scheme['executable'])
        preview = self.preview(scheme)
        body = {'preview_id': preview['preview_id'], 'digest': preview['digest']}
        self.request('POST', self.p + '/exports', body, status=422)
        self.request('POST', self.p + '/exports', {**body, 'hardlink_ack_digest': '0' * 64}, status=409)
        self.assertFalse(list((self.root / 'exports').iterdir()))
        self.assertEqual(self.request('GET', self.p + '/exports')['items'], [])
        key = str(uuid4())
        body['hardlink_ack_digest'] = preview['hardlink_ack_digest']
        result = self.request('POST', self.p + '/exports', body, key=key, status=202)
        replay = self.request('POST', self.p + '/exports', body, key=key, status=202)
        self.assertEqual(result['export_id'], replay['export_id'])
        self.wait_operation({'operation_id': result['operation_id']})
        report = self.request('GET', self.p + '/exports/' + result['export_id'] + '/report')
        self.assertEqual(report['hardlink_ack_digest'], preview['plan_digest'])
        item = report['images'][0]
        self.assertTrue(os.path.samefile(self.root / 'images' / item['filename'], self.root / report['directory'] / item['output_path']))
        self.runtime.dataset.exchange.exports.clear()
        recovered = self.request('GET', self.p + '/exports/' + result['export_id'] + '/report')
        self.assertEqual(recovered, report)
        self.assertEqual(self.request('GET', self.p + '/exports/' + result['export_id'])['image_mode'], 'hardlink')

    def test_confirmation_cannot_cross_preview_or_copy_mode(self):
        self.import_inputs(); self.keep([1])
        scheme = self.scheme()
        first = self.preview(scheme)
        path = self.p + '/export-schemes/' + str(scheme['scheme_id'])
        updated = self.request('PUT', path, {**scheme['configuration'], 'name': '新版'},
                               version=scheme['version'], headers={'Idempotency-Key': str(uuid4())})
        second = self.preview(updated)
        self.request('POST', self.p + '/exports', {'preview_id': second['preview_id'], 'digest': second['digest'],
            'hardlink_ack_digest': first['hardlink_ack_digest']}, status=409)
        copied = self.export_preview()
        self.request('POST', self.p + '/exports', {'preview_id': copied['preview_id'], 'digest': copied['digest'],
            'hardlink_ack_digest': first['hardlink_ack_digest']}, status=409)
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_plan_change_after_confirmation_is_rechecked_by_worker(self):
        self.import_inputs(); self.keep([1])
        scheme = self.scheme()
        preview = self.preview(scheme)
        edit_manual(self.runtime.context.handle, 1, tags=('new tag',))
        result = self.request('POST', self.p + '/exports', {'preview_id': preview['preview_id'], 'digest': preview['digest'],
            'hardlink_ack_digest': preview['hardlink_ack_digest']}, status=202)
        job = self.wait_operation({'operation_id': result['operation_id']}, expected='failed')
        self.assertEqual(job['error']['code'], 'concurrency:version_conflict')
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_pipeline_is_unavailable_and_confirmation_is_strict_digest(self):
        scheme = self.scheme(pipeline_json=[{'processor': 'metadata_strip'}])
        self.assertFalse(scheme['executable'])
        self.assertEqual(scheme['unavailable_reasons'], ['pipeline:unsupported'])
        job = self.request('POST', self.p + '/exports/preview', {'scheme_id': scheme['scheme_id']}, status=202)
        self.assertEqual(self.wait_operation(job, expected='failed')['error']['code'], 'feature:unsupported')
        self.request('POST', self.p + '/exports', {'preview_id': str(uuid4()), 'digest': '0' * 64,
            'hardlink_ack_digest': True}, status=422)
        self.assertFalse(list((self.root / 'exports').iterdir()))
