from dataclasses import replace
import hashlib
import json
import struct
import zlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from stylelora.generation.artifacts import inspect_and_prepare, register_artifact
from stylelora.generation.metadata import inspect_png
from stylelora.project_db import commands
from stylelora.project_db.connection import submit_write
from stylelora.project_db.artifacts import artifact_from_receipt, load_receipt, prepare_artifact, receipt_path
from stylelora.project_db.errors import ActiveOperations, ArtifactConflict, ProjectClosed, ValidationError
from stylelora.project_db.project import close_project
from stylelora.project_db.recovery import inspect_sent
from stylelora.project_db.repositories import annotations, images, sources
from stylelora.project_db.types import ItemSeed, SourceDraft
from stylelora.project_db.validation import decode, digest
from stylelora.runtime.errors import ServiceError
from stylelora.tagging.catalog import TagCatalog
from .support import DatabaseTest
from .api_support import APITest
from .test_generation_handoff import draft


def png(path, comment=None, *, size=(64, 64), duplicate=False, compressed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    if comment is not None:
        value = json.dumps(comment, ensure_ascii=False) if isinstance(comment, dict) else comment
        info.add_text('Comment', value, zip=compressed)
        if duplicate:
            info.add_text('Comment', value)
    Image.new('RGB', size, (25, 113, 79)).save(path, pnginfo=info)
    return path


class PngInspectionTests(DatabaseTest):
    def inspect(self, comment=None, *, style='my style', sent='my style, forest', stripped=None, **kwargs):
        path = png(self.root/'sample.png', comment, **kwargs)
        return inspect_png(path, style_prompt=style, sent_prompt=sent, metadata_stripped=stripped,
                           expected_width=64, expected_height=64)

    def test_full_png_fingerprint_and_raw_comment_are_retained(self):
        comment = {'prompt': 'MY_STYLE, forest', 'seed': 867, 'steps': 28, 'scale': 5.0,
                   'uc': 'masterpiece', 'extra': {'unknown': ['保留', 1]}}
        result = self.inspect(comment)
        self.assertEqual(result.validation, 'pass')
        self.assertEqual(result.png_meta, {'schema_version': 1, 'comment': comment})
        data = (self.root/'sample.png').read_bytes()
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(result.file_size, len(data))
        self.assertEqual((result.width, result.height), (64, 64))
        self.assertEqual(len(result.phash), 16)

    def test_complete_tags_case_underscore_and_whitespace_not_substrings(self):
        result = self.inspect({'prompt': 'MY___STYLE, artist'}, style='my style, art')
        self.assertEqual(result.validation, 'mismatch')
        self.assertEqual(result.missing_style_tags, ('art',))
        self.assertEqual(result.reason_codes, ('meta:style_tag_missing',))

    def test_added_quality_tags_exclude_sent_tags_and_negative_prompt(self):
        result = self.inspect({'prompt': 'my style, masterpiece, no_text, very aesthetic', 'uc': 'forest'},
                              sent='my style, masterpiece')
        self.assertEqual(result.unexpected_quality_tags, ('no text', 'very aesthetic'))
        self.assertEqual(result.reason_codes, ('meta:unexpected_quality_tags',))
        self.assertEqual(self.inspect({'prompt': 'my style', 'uc': 'masterpiece'}).validation, 'pass')

    def test_missing_and_stripped_metadata_never_report_missing_style(self):
        for stripped, code in ((None, 'meta:missing'), (False, 'meta:missing'), (True, 'meta:stripped')):
            value = self.inspect(stripped=stripped)
            self.assertEqual((value.validation, value.reason_codes, value.png_meta), ('unverifiable', (code,), None))
        self.assertEqual(self.inspect({'prompt': 'my style'}, stripped=True).validation, 'pass')

    def test_invalid_json_duplicate_keys_nonfinite_and_duplicate_comment(self):
        for comment in ('{bad', '[]', '{"prompt":"my style","prompt":"other"}',
                        '{"prompt":"my style","n":NaN}', '{"prompt":"my style","n":1e999}',
                        '{"prompt":"my style","n":"\\ud800"}'):
            with self.subTest(comment=comment):
                result = self.inspect(comment)
                self.assertEqual(result.reason_codes, ('meta:invalid_comment',))
                self.assertIsNone(result.png_meta)
        self.assertEqual(self.inspect({'prompt': 'my style'}, duplicate=True).reason_codes, ('meta:invalid_comment',))

    def test_versioned_tokenizer_does_not_strip_weights_or_escaped_commas(self):
        for prompt in ('(my style:1.2), forest', '{my style}, forest', 'my style\\, forest', 'my style, BREAK'):
            result = self.inspect({'prompt': prompt})
            self.assertEqual(result.validation, 'unverifiable')
            self.assertEqual(result.reason_codes, ('meta:unsupported_syntax',))
            self.assertEqual(result.missing_style_tags, ())
        self.assertEqual(self.inspect({'prompt': 'my style'}, style='(my style:1.2)').validation, 'unverifiable')
        self.assertEqual(self.inspect({'prompt': 'my style'}, sent='my style, [forest]').validation, 'unverifiable')

    def test_v4_prompt_fallback_and_ambiguity_are_conservative(self):
        v4 = {'caption': {'base_caption': 'MY_STYLE, forest', 'char_captions': []}, 'use_coords': False}
        self.assertEqual(self.inspect({'v4_prompt': v4}).validation, 'pass')
        self.assertEqual(self.inspect({'prompt': 'forest, my style', 'v4_prompt': v4}).validation, 'pass')
        for value, code in (({'prompt': 'other', 'v4_prompt': v4}, 'meta:ambiguous_prompt'),
                            ({'prompt': 1}, 'meta:invalid_comment'), ({'seed': 1}, 'meta:missing_prompt'),
                            ({'v4_prompt': {'caption': {'base_caption': 'my style', 'char_captions': [{}]}}},
                             'meta:unsupported_structure')):
            result = self.inspect(value)
            self.assertEqual(result.reason_codes, (code,))
            self.assertEqual(result.validation, 'unverifiable')
            self.assertEqual(result.png_meta['comment'], value)

    def test_compressed_comment_and_bounded_comment(self):
        self.assertEqual(self.inspect({'prompt': 'my style'}, compressed=True).validation, 'pass')
        with patch('stylelora.generation.metadata.MAX_COMMENT', 5):
            self.assertEqual(self.inspect({'prompt': 'my style'}).reason_codes, ('meta:invalid_comment',))

    def test_invalid_png_crc_truncation_trailing_bytes_jpeg_and_size_mismatch(self):
        path = png(self.root/'sample.png', {'prompt': 'my style'})
        data = path.read_bytes()
        for bad in (b'not a png', data[:-12], data + b'trailing', data[:40] + bytes([data[40] ^ 1]) + data[41:]):
            path.write_bytes(bad)
            with self.assertRaises(ServiceError) as error:
                inspect_png(path, style_prompt='', sent_prompt='', expected_width=64, expected_height=64)
            self.assertEqual(error.exception.code, 'artifact:invalid_png')
        Image.new('RGB', (64,64)).save(path, format='JPEG')
        with self.assertRaises(ServiceError):
            inspect_png(path, style_prompt='', sent_prompt='', expected_width=64, expected_height=64)
        with self.assertRaises(ServiceError) as error:
            self.inspect(size=(65, 64))
        self.assertEqual(error.exception.code, 'artifact:size_mismatch')

    def test_animation_and_decode_resource_limits(self):
        path = self.root/'animated.png'
        Image.new('RGB', (64,64), 'red').save(path, save_all=True, append_images=[Image.new('RGB', (64,64), 'blue')])
        with self.assertRaises(ServiceError):
            inspect_png(path, style_prompt='', sent_prompt='', expected_width=64, expected_height=64)
        with patch('stylelora.generation.metadata.MAX_BYTES', 16), self.assertRaises(ServiceError) as error:
            self.inspect()
        self.assertEqual(error.exception.code, 'artifact:too_large')
        with patch('stylelora.generation.metadata.MAX_PIXELS', 10), self.assertRaises(ServiceError) as error:
            self.inspect()
        self.assertEqual(error.exception.code, 'artifact:invalid_png')

    def test_valid_crc_with_invalid_compressed_pixels_still_fails_full_decode(self):
        path = png(self.root/'bad-data.png')
        data = path.read_bytes()
        start = data.index(b'IDAT') - 4
        length = struct.unpack_from('>I', data, start)[0]
        payload = b'broken zlib pixels'
        chunk = struct.pack('>I', len(payload)) + b'IDAT' + payload + struct.pack('>I', zlib.crc32(b'IDAT' + payload))
        path.write_bytes(data[:start] + chunk + data[start+12+length:])
        with self.assertRaises(ServiceError) as error:
            inspect_png(path, style_prompt='', sent_prompt='', expected_width=64, expected_height=64)
        self.assertEqual(error.exception.code, 'artifact:invalid_png')


class GenerationRegistrationTests(DatabaseTest):
    def generated(self, *, prompt='forest, A quiet place.', comment=None, sent=True):
        raw = draft(1)
        raw['config']['sizes'] = {'landscape': {'width':128,'height':64}, 'portrait': {'width':64,'height':128},
                                  'square': {'width':64,'height':64}}
        raw['config']['default_aspect'] = 'square'
        source = self.write(sources.create_source, SourceDraft(type='nai', **raw))
        self.write(commands.start_source, source.source_id, (ItemSeed(1, prompt),))
        item = self.item(source.source_id)
        if sent:
            item = self.write(commands.mark_sent, item.item_id).value
        path = png(self.root/f'images/.pending/{item.item_id}/1/out.png',
                   comment if comment is not None else {'prompt': 'geometric style, ' + prompt})
        return item, path

    def prepared(self, *, comment=None, prompt='forest, A quiet place.', catalog=True):
        item, path = self.generated(prompt=prompt, comment=comment)
        tags = TagCatalog('fixture-catalog', {'forest':'forest'}, {}, True) if catalog else None
        return inspect_and_prepare(self.project, item_id=item.item_id, attempt=1, saved_path=path, catalog=tags)

    def test_content_only_originals_metadata_cannot_override_source_or_caption(self):
        prepared = self.prepared(comment={'prompt': 'geometric style, forest', 'seed': 321, 'steps': 999, 'scale': 999})
        result = register_artifact(self.project, prepared)
        image = result.value
        self.assertEqual(image.review_status, 'unreviewed')
        self.assertEqual(self.read(annotations.get_current, image.image_id, 'original', 'tags').content[0].tag, 'forest')
        self.assertEqual(self.read(annotations.get_current, image.image_id, 'original', 'nl').content, 'A quiet place.')
        self.assertEqual(self.read(sources.get_source, image.source_id).config_json['profile']['steps'], 17)
        self.assertEqual(image.png_meta_json['comment']['steps'], 999)
        self.assertTrue((self.root/f'images/.checks/{prepared.artifact.item_id}/1.json').exists())
        self.assertEqual(self.read(sources.get_item, prepared.artifact.item_id).status, 'done')

    def test_mismatch_initial_review_is_atomic_and_replay_preserves_manual_decision(self):
        prepared = self.prepared(comment={'prompt': 'forest, masterpiece'})
        image = register_artifact(self.project, prepared).value
        self.assertEqual(image.review_status, 'reject')
        original = self.read(annotations.get_current, image.image_id, 'original', 'tags')
        self.assertEqual(original.params_json['generation_validation']['validation'], 'mismatch')
        self.write(images.set_review, image.image_id, expected='reject', target='keep')
        again = register_artifact(self.project, prepared)
        self.assertFalse(again.changed)
        self.assertEqual(again.value.review_status, 'keep')
        self.assertEqual(self.sql('SELECT count(*) FROM images'), ((1,),))
        self.assertEqual(self.sql('SELECT count(*) FROM annotations'), ((2,),))

    def test_unverifiable_image_is_registered_unreviewed_with_unmodified_file(self):
        prepared = self.prepared(comment='{broken json')
        path = self.root/prepared.artifact.relative_staging_path
        before = path.read_bytes()
        image = register_artifact(self.project, prepared).value
        self.assertEqual(image.review_status, 'unreviewed')
        self.assertIsNone(image.png_meta_json)
        self.assertEqual((self.root/'images'/image.filename).read_bytes(), before)

    def test_missing_catalog_or_unsupported_content_is_preserved_as_nl(self):
        for prompt, catalog, code in (('forest', False, 'original:catalog_missing'),
                                      ('(forest:1.2)', True, 'original:unsupported_syntax')):
            prepared = self.prepared(prompt=prompt, catalog=catalog)
            self.assertEqual(len(prepared.originals), 1)
            self.assertEqual((prepared.originals[0].type, prepared.originals[0].content), ('nl', prompt))
            self.assertIn(code, prepared.originals[0].params['warnings'])
            register_artifact(self.project, prepared)

    def test_wrong_path_state_attempt_and_bad_png_create_no_complete_evidence(self):
        item, path = self.generated(sent=False)
        with self.assertRaises(ServiceError):
            inspect_and_prepare(self.project, item_id=item.item_id, attempt=1, saved_path=path)
        self.write(commands.mark_sent, item.item_id)
        for saved, attempt in ((path.parent/'other.png', 1), (Path('out.png'), 1), (path, 2)):
            with self.assertRaises(ServiceError):
                inspect_and_prepare(self.project, item_id=item.item_id, attempt=attempt, saved_path=saved)
        with self.assertRaises(ValidationError):
            inspect_and_prepare(self.project, item_id=item.item_id, attempt=True, saved_path=path)
        path.write_bytes(b'not complete')
        with self.assertRaises(ServiceError):
            inspect_and_prepare(self.project, item_id=item.item_id, attempt=1, saved_path=path)
        self.assertFalse(receipt_path(self.root, item.item_id, 1).exists())
        self.assertEqual(self.read(sources.get_item, item.item_id).status, 'sent')
        self.assertEqual(inspect_sent(self.project, self.read(sources.get_item, item.item_id)).verdict, 'unknown')

    def test_changed_file_between_inspection_and_receipt_is_rejected(self):
        item, path = self.generated()
        from stylelora.generation import artifacts
        original = artifacts.prepare_artifact
        def changed(*args, **kwargs):
            png(path, {'prompt': 'other'})
            return original(*args, **kwargs)
        with patch.object(artifacts, 'prepare_artifact', side_effect=changed), self.assertRaises(ArtifactConflict):
            inspect_and_prepare(self.project, item_id=item.item_id, attempt=1, saved_path=path)
        self.assertFalse(receipt_path(self.root, item.item_id, 1).exists())

    def test_changed_file_after_receipt_cannot_be_registered_or_recovered(self):
        prepared = self.prepared()
        (self.root/prepared.artifact.relative_staging_path).write_bytes(b'changed')
        with self.assertRaises(ArtifactConflict):
            register_artifact(self.project, prepared)
        self.reopen()
        self.assertEqual(self.read(sources.get_item, prepared.artifact.item_id).status, 'sent')
        self.assertEqual(self.sql('SELECT count(*) FROM images'), ((0,),))

    def test_check_archive_persists_without_any_original_annotation(self):
        source, _ = self.source()
        item = self.write(commands.mark_sent, self.item(source.source_id).item_id).value
        path = png(self.root/f'images/.pending/{item.item_id}/1/out.png')
        check = {'schema_version': 1, 'review_status': 'reject', 'summary': {'schema_version': 1, 'reason': 'fixture'}}
        artifact = prepare_artifact(self.project, item_id=item.item_id, attempt=1,
            relative_staging_path=path.relative_to(self.root).as_posix(), width=64, height=64, registration_check=check)
        image = self.write(commands.complete_item, artifact).value
        self.assertEqual(image.review_status, 'reject')
        self.assertEqual(self.sql('SELECT count(*) FROM annotations'), ((0,),))
        archive = decode((self.root/f'images/.checks/{item.item_id}/1.json').read_text())
        self.assertEqual(archive['content']['check'], check)
        self.assertEqual(archive['digest'], digest(archive['content']))

    def test_corrupt_check_blocks_recovery_and_never_overwrites_evidence(self):
        prepared = self.prepared(comment={'prompt':'forest'})
        archive = self.root/f'images/.checks/{prepared.artifact.item_id}/1.json'
        archive.write_text('{}')
        with self.assertRaises(ArtifactConflict):
            register_artifact(self.project, prepared)
        self.reopen()
        self.assertEqual(archive.read_text(), '{}')
        self.assertEqual(self.read(sources.get_item, prepared.artifact.item_id).status, 'sent')

    def test_crash_before_and_after_rename_recovers_check_and_originals_atomically(self):
        for point in ('artifact_before_rename', 'artifact_after_rename', 'artifact_before_done'):
            prepared = self.prepared(comment={'prompt': 'forest'})
            def fail(name):
                if name == point:
                    raise OSError('injected crash')
            self.project._fault_hook = fail
            with self.assertRaises(OSError):
                register_artifact(self.project, prepared)
            self.project._fault_hook = None
            self.assertEqual(self.read(sources.get_item, prepared.artifact.item_id).status, 'sent')
            self.reopen()
            item = self.read(sources.get_item, prepared.artifact.item_id)
            self.assertEqual(item.status, 'done')
            self.assertEqual(self.read(images.get_image, item.image_id).review_status, 'reject')
            self.assertFalse(receipt_path(self.root, item.item_id, 1).exists())
        self.assertEqual(self.sql('SELECT count(*) FROM images'), ((3,),))

    def test_after_commit_crash_and_reopen_preserve_review_without_duplicates(self):
        prepared = self.prepared(comment={'prompt': 'forest'})
        def fail(name):
            if name == 'artifact_after_commit':
                raise OSError('after commit')
        self.project._fault_hook = fail
        with self.assertRaises(OSError):
            register_artifact(self.project, prepared)
        self.project._fault_hook = None
        image_id = self.read(sources.get_item, prepared.artifact.item_id).image_id
        self.write(images.set_review, image_id, expected='reject', target='keep')
        self.reopen()
        self.assertEqual(self.read(images.get_image, image_id).review_status, 'keep')
        self.assertEqual(self.sql('SELECT count(*) FROM images'), ((1,),))
        self.assertFalse(register_artifact(self.project, prepared).changed)

    def test_old_v1_receipt_and_default_review_remain_compatible(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        receipt = load_receipt(self.root, artifact)
        self.assertNotIn('registration_check', receipt['artifact'])
        self.assertEqual(artifact_from_receipt(receipt), artifact)
        self.reopen()
        item = self.read(sources.get_item, artifact.item_id)
        self.assertEqual(self.read(images.get_image, item.image_id).review_status, 'unreviewed')

    def test_invalid_initial_review_or_tampered_check_cannot_register(self):
        prepared = self.prepared()
        for value in ({'schema_version': True, 'review_status':'reject', 'summary':{}},
                      {'schema_version': 1, 'review_status':'keep', 'summary':{'schema_version':1}},
                      {'schema_version': 1, 'review_status':'reject', 'summary':{'schema_version':1}, 'extra':1}):
            with self.assertRaises(ValidationError):
                self.write(commands.complete_item, replace(prepared.artifact, registration_check=value), prepared.originals)
        altered = {**prepared.artifact.registration_check, 'review_status':'reject'}
        with self.assertRaises(ArtifactConflict):
            self.write(commands.complete_item, replace(prepared.artifact, registration_check=altered), prepared.originals)

    def test_check_archive_write_failure_leaves_sent_and_no_complete_receipt(self):
        item, path = self.generated()
        with patch('stylelora.project_db.artifacts.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                inspect_and_prepare(self.project, item_id=item.item_id, attempt=1, saved_path=path)
        self.assertTrue(path.exists())
        self.assertFalse(receipt_path(self.root, item.item_id, 1).exists())
        self.assertEqual(self.read(sources.get_item, item.item_id).status, 'sent')
        self.assertEqual(self.sql('SELECT count(*) FROM images'), ((0,),))

    def test_already_sent_late_image_can_complete_canceled_source_without_reopening_it(self):
        prepared = self.prepared()
        source_id = prepared.artifact.registration_check['summary']['source_id']
        self.write(commands.transition_source, source_id, expected='running', target='canceled', reason='user')
        register_artifact(self.project, prepared)
        self.assertEqual(self.read(sources.get_source, source_id).status, 'canceled')
        self.assertEqual(self.read(sources.get_item, prepared.artifact.item_id).status, 'done')

    def test_close_during_inspection_waits_for_project_file_operation(self):
        item, path = self.generated()
        def inspect(*args, **kwargs):
            with self.assertRaises(ActiveOperations):
                close_project(self.project)
            return inspect_png(*args, **kwargs)
        with patch('stylelora.generation.artifacts.inspect_png', side_effect=inspect):
            prepared = inspect_and_prepare(self.project, item_id=item.item_id, attempt=1, saved_path=path)
        self.assertEqual(register_artifact(self.project, prepared).value.review_status, 'unreviewed')

    def test_closed_project_cannot_create_inspection_receipts(self):
        item, path = self.generated()
        close_project(self.project)
        with self.assertRaises(ProjectClosed):
            inspect_and_prepare(self.project, item_id=item.item_id, attempt=1, saved_path=path)
        self.assertFalse(receipt_path(self.root, item.item_id, 1).exists())


class GenerationArtifactAPITests(APITest):
    def test_local_registration_http_review_and_export_use_content_only(self):
        raw = draft(1)
        raw['config']['default_aspect'] = 'square'
        raw['config']['sizes']['square'] = {'width':64,'height':64}
        source = self.request('POST', self.p+'/sources', raw, status=201)
        project = self.runtime.context.handle
        def start(db):
            commands.start_source(db, source['source_id'], (ItemSeed(1, 'forest, A quiet place.'),))
            item = sources.get_item(db, db.execute('SELECT item_id FROM source_items WHERE source_id=?',
                                                  (source['source_id'],)).fetchone()[0])
            return commands.mark_sent(db, item.item_id).value
        item = submit_write(project, start).result()
        # A synthetic caller file exercises the local completion boundary only.
        path = png(self.root/f'images/.pending/{item.item_id}/1/out.png', {'prompt':'forest, masterpiece'})
        data = path.read_bytes()
        prepared = inspect_and_prepare(project, item_id=item.item_id, attempt=1, saved_path=path,
            catalog=TagCatalog('fixture', {'forest':'forest'}, {}, True))
        image = register_artifact(project, prepared).value
        view = self.request('GET', self.p+f'/images/{image.image_id}')
        self.assertEqual(view['review_status'], 'reject')
        layers = self.request('GET', self.p+f'/annotations/{image.image_id}')['layers']
        self.assertEqual(layers['original/tags']['content'], [{'tag':'forest','conf':None}])
        self.assertEqual(layers['original/nl']['content'], 'A quiet place.')
        self.assertEqual(layers['original/tags']['params_json']['generation_validation']['validation'], 'mismatch')
        preview = self.export_preview(include_nl=True)
        self.assertEqual([row['status'] for row in preview['images']], ['skipped'])
        self.keep([image.image_id])
        exported = self.run_export(self.export_preview(include_nl=True))
        report = self.request('GET', exported['report_url'])
        output = self.root/report['directory']
        self.assertEqual(next(output.glob('*.png')).read_bytes(), data)
        self.assertEqual(next(output.glob('*.txt')).read_text(encoding='utf-8'), 'http style, forest\n\nA quiet place.\n')
        self.assertFalse(self.request('GET', '/api/generation/capabilities')['generation'])
