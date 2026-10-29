"""Optional UTF-8/GBK fallback across durable imports and the HTTP boundary."""
import hashlib
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from stylelora.exchange.importer import _load_options, _report, import_images
from stylelora.exchange.types import ImportOptions
from stylelora.project_db.connection import read_snapshot
from stylelora.project_db.repositories import annotations, schemes, sources
from stylelora.project_db.project import close_project
from stylelora.project_db.types import ImportSchemeInput
from .api_support import APITest
from .exchange_support import ExchangeTest


class ImportEncodingTests(ExchangeTest):
    def test_cli_auto_import_returns_encoding_and_warning(self):
        from stylelora.cli import main
        self.input_image(encoding='gbk')
        close_project(self.project)
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(['import', str(self.root), str(self.inputs), '--encoding', 'auto'])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual((result['imported'], result['items'][0]['encoding']), (1, 'gbk'))
        self.assertEqual(result['items'][0]['warnings'], ['encoding:gbk_fallback'])
        self.reopen()

    def test_mixed_encodings_persist_and_reopen_without_input_directory(self):
        originals = {}
        for index, codec in enumerate(('utf-8', 'utf-8-sig', 'gbk')):
            path = self.input_image(f'{index}.png', color=(index * 50, 70, 80), encoding=codec)
            originals[path.name] = path.read_bytes()
        result = import_images(self.project, self.inputs, options=ImportOptions(encoding='auto'))
        self.assertEqual((result.imported, result.failed), (3, 0))
        self.assertEqual([item.encoding for item in result.items], ['utf-8', 'utf-8-sig', 'gbk'])
        self.assertEqual(result.items[-1].warnings, ('encoding:gbk_fallback',))
        for item in result.items:
            original = self.read(annotations.get_current, item.image_id, 'original', 'tags')
            raw = (self.inputs / item.path).with_suffix('.txt').read_bytes()
            self.assertEqual(original.params_json['sidecar_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual((self.inputs / item.path).read_bytes(), originals[item.path])
        self.reopen()
        self.inputs.rename(self.inputs.with_name('moved-inputs'))
        source = self.read(sources.get_source, result.source_id)
        _, entries = _load_options(source)
        self.assertEqual(_report(self.project, result.source_id, entries).items, result.items)

    def test_resume_keeps_auto_policy_when_saved_scheme_changes(self):
        self.input_image(encoding='gbk')
        scheme = self.write(schemes.create_import_scheme, ImportSchemeInput(
            'auto', params_json={'schema_version': 1, 'encoding': 'auto', 'tokenizer_version': 'literal-tags-v1'}))
        paused = import_images(self.project, self.inputs, scheme_id=scheme.scheme_id, cancelled=lambda: True)
        self.assertEqual(paused.status, 'paused')
        self.write(schemes.update_import_scheme, scheme.scheme_id,
                   ImportSchemeInput('strict', params_json={'schema_version': 1, 'encoding': 'utf-8', 'tokenizer_version': 'literal-tags-v1'}),
                   expected_updated_at=scheme.updated_at)
        self.reopen()
        resumed = import_images(self.project, self.inputs, source_id=paused.source_id)
        self.assertEqual((resumed.imported, resumed.failed), (1, 0))
        self.assertTrue(resumed.items[0].encoding_fallback)
        self.assertEqual(self.read(sources.get_source, resumed.source_id).config_json['params']['encoding'], 'auto')

    def test_receipt_recovery_keeps_encoding_evidence_without_duplicate_annotations(self):
        self.input_image(encoding='gbk')
        self.fail_at('import_after_receipt')
        first = import_images(self.project, self.inputs, options=ImportOptions(encoding='auto'))
        self.assertEqual(first.unresolved, 1)
        self.project._fault_hook = None
        self.reopen()
        result = import_images(self.project, self.inputs, source_id=first.source_id)
        self.assertEqual((result.imported, result.unresolved), (1, 0))
        self.assertEqual((result.items[0].encoding, result.items[0].warnings), ('gbk', ('encoding:gbk_fallback',)))
        self.assertEqual(self.sql('SELECT count(*) FROM annotations')[0][0], 2)

    def test_bad_encoding_fails_individually_and_does_not_publish_a_file(self):
        good = self.input_image('a.png', encoding='gbk')
        bad = self.input_image('b.png', color='blue')
        bad.with_suffix('.txt').write_bytes(b'\xef\xbb\xbf\xff')
        result = import_images(self.project, self.inputs, options=ImportOptions(encoding='auto'))
        self.assertEqual((result.imported, result.failed), (1, 1))
        self.assertEqual(result.items[1].error, 'encoding:invalid')
        self.assertIsNone(result.items[1].encoding)
        self.assertEqual(len(list((self.root / 'images').glob('*.png'))), 1)
        self.assertTrue(good.exists())


class ImportEncodingAPITests(APITest):
    def preview_encoding(self, options=None, scheme_id=None):
        grant = self.runtime.grant(self.inputs, 'import_root', self.pid)
        job = self.request('POST', self.p + '/imports/preview', {
            'root_handle': grant['grant_id'], 'options': options, 'scheme_id': scheme_id}, status=202)
        return self.wait_operation(job)['result']

    def test_capabilities_and_scheme_are_executable_but_default_is_strict(self):
        caps = self.request('GET', '/api/exchange/capabilities')
        self.assertIn('auto', caps['encodings'])
        self.assertEqual(caps['auto_encoding_policy'], 'utf8_then_gbk')
        self.assertEqual(caps['default_import']['params_json']['encoding'], 'utf-8-sig')
        scheme = self.request('POST', self.p + '/import-schemes', {'name': 'auto', 'params_json': {
            'encoding': 'auto', 'tokenizer_version': 'literal-tags-v1'}}, status=201)
        self.assertTrue(scheme['executable'])
        self.assertEqual(scheme['unavailable_reasons'], [])
        self.make_input().with_suffix('.txt').write_bytes('smile\n\n一名女孩站在树旁。'.encode('gbk'))
        strict = self.preview_encoding()
        self.assertFalse(strict['can_import'])
        preview = self.preview_encoding(scheme_id=scheme['scheme_id'])
        self.assertTrue(preview['can_import'])
        self.assertEqual(preview['encoding_fallback_count'], 1)

    def test_http_override_preview_import_and_durable_summary_agree(self):
        self.make_input().with_suffix('.txt').write_bytes('smile\n\n一名女孩站在树旁。'.encode('gbk'))
        preview = self.preview_encoding({'encoding': 'auto'})
        diagnostic = preview['diagnostics'][0]
        self.assertEqual((diagnostic['encoding'], diagnostic['encoding_fallback']), ('gbk', True))
        self.assertEqual(diagnostic['warnings'], ['encoding:gbk_fallback'])
        job = self.request('POST', self.p + '/imports', {
            'preview_id': preview['preview_id'], 'digest': preview['digest']}, status=202)
        result = self.wait_operation(job)['result']
        self.assertEqual(result['imported'], 1)
        self.inputs.rename(self.inputs.with_name('gone'))
        summary = self.request('GET', self.p + f"/imports/{result['source_id']}/summary")
        self.assertEqual(summary['items'], result['items'])
        with read_snapshot(self.runtime.context.handle) as db:
            original = annotations.get_current(db, 1, 'original', 'nl')
        self.assertEqual(original.content, '一名女孩站在树旁。')
        self.assertTrue(original.params_json['encoding_fallback'])
        default = self.request('GET', self.p + '/import-schemes')['items'][0]
        self.assertEqual(default['configuration']['params_json']['encoding'], 'utf-8-sig')

    def test_bom_and_undecodable_inputs_block_http_import_before_writes(self):
        path = self.make_input()
        for raw in (b'\xff', b'\xef\xbb\xbf\xff', b'\xef\xbb\xbf' + '蓝发'.encode('gbk')):
            path.with_suffix('.txt').write_bytes(raw)
            preview = self.preview_encoding({'encoding': 'auto'})
            self.assertFalse(preview['can_import'])
            self.assertEqual(preview['diagnostics'][0]['error'], 'encoding:invalid')
            self.request('POST', self.p + '/imports', {
                'preview_id': preview['preview_id'], 'digest': preview['digest']}, status=422)
        self.assertEqual(self.request('GET', self.p + '/sources')['items'], [])
        self.assertEqual(list((self.root / 'images').iterdir()), [])

    def test_fallback_total_covers_rows_beyond_bounded_preview(self):
        for index in range(101):
            path = self.make_input(f'{index:03}.png')
            if index == 100:
                path.with_suffix('.txt').write_bytes('smile\n\n中文段落。'.encode('gbk'))
        with patch('stylelora.services.exchange.inspect_image'):
            preview = self.preview_encoding({'encoding': 'auto'})
        self.assertEqual(len(preview['diagnostics']), 100)
        self.assertEqual(preview['encoding_fallback_count'], 1)
        self.assertFalse(any(row['encoding_fallback'] for row in preview['diagnostics']))
        self.assertTrue(preview['can_import'])

    def test_sidecar_changed_after_auto_preview_rejects_without_sources(self):
        path = self.make_input().with_suffix('.txt')
        path.write_bytes('smile\n\n中文段落。'.encode('gbk'))
        preview = self.preview_encoding({'encoding': 'auto'})
        path.write_text('smile\n\nchanged', encoding='utf-8')
        job = self.request('POST', self.p + '/imports', {
            'preview_id': preview['preview_id'], 'digest': preview['digest']}, status=202)
        self.wait_operation(job, expected='failed')
        self.assertEqual(self.request('GET', self.p + '/sources')['items'], [])
