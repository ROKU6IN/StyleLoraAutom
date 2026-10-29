import hashlib
import io
import json
import os
import random
from contextlib import redirect_stdout
from importlib.resources import files
from unittest.mock import patch
from uuid import uuid4

from stylelora.annotations.editing import edit_manual
from stylelora.caption.sorters import GROUPS, PyramidSorter, load_pyramid, pyramid_capability
from stylelora.exchange.exporter import ExportOptions, export_images, preview_export
from stylelora.exchange.schemes import ExportScheme
from stylelora.project_db.errors import PreviewStale, SorterContractViolation, UnsupportedSorter
from stylelora.project_db.merge import merge_caption
from stylelora.project_db.project import close_project
from stylelora.project_db.repositories import schemes
from stylelora.project_db.types import ExportSchemeInput, Tag
from stylelora.runtime.errors import safe_error
from stylelora.tagging.catalog import TagCatalog
from .api_support import APITest
from .exchange_support import ExchangeTest


def resource_fixture(base):
    root = base / 'sorter-resources'
    (root / 'data').mkdir(parents=True, exist_ok=True)
    for name in ('prompt_pyramid_v1.json', 'prompt_pyramid_v1.md'):
        (root / 'data' / name).write_bytes(files('stylelora.caption').joinpath('data', name).read_bytes())
    return root, root / 'data' / 'prompt_pyramid_v1.json'


class PyramidTests(ExchangeTest):
    def scheme(self, **caption):
        body = ExportScheme(name='排序', caption_config_json={'tag_sort': 'prompt_pyramid', **caption}).model_dump()
        body['pipeline_json'] = tuple(body['pipeline_json'])
        return self.write(schemes.create_export_scheme, ExportSchemeInput(**body))

    def test_group_order_and_stable_ties_with_unknowns(self):
        sorter = load_pyramid()
        tags = ('unknown z', 'moonlight', 'outdoors', 'shirt', 'wide shot', 'smile', 'unknown a', 'blue hair', '1girl', 'running')
        self.assertEqual(sorter.sort(tags), ('blue hair', '1girl', 'smile', 'running', 'shirt', 'wide shot', 'outdoors', 'moonlight', 'unknown z', 'unknown a'))
        self.assertEqual(sum(sorter.counts(tags).values()), len(tags))
        self.assertEqual(sorter.sort(()), ())

    def test_aliases_only_classify_and_never_substitute_or_deduplicate(self):
        catalog = TagCatalog('fixture', {'azure hair': 'blue hair', 'blue hair': 'blue hair'}, {'blue hair': 'general'})
        tags = ('moonlight', 'azure hair', 'blue hair', 'smile')
        self.assertEqual(load_pyramid(catalog).sort(tags), ('azure hair', 'blue hair', 'smile', 'moonlight'))

    def test_explicit_catalog_categories_character_and_unknown_category(self):
        entries = {tag: tag for tag in ('hero (series)', 'custom lamp', 'smile', 'shirt', 'sunlight')}
        categories = {'hero (series)': 'character', 'custom lamp': 'pyramid:lighting', 'smile': 'pyramid:scene',
                      'shirt': 'artist', 'sunlight': 'not-a-known-category'}
        sorter = load_pyramid(TagCatalog('fixture', entries, categories))
        self.assertEqual(sorter.sort(('shirt', 'sunlight', 'custom lamp', 'smile', 'hero (series)')),
                         ('hero (series)', 'smile', 'custom lamp', 'shirt', 'sunlight'))
        for tag in ('blue hair ornament', 'shirt print', 'hero (series)', 'artist:example', 'sunset', 'tree'):
            self.assertEqual(load_pyramid().category(tag), 'unclassified')

    def test_frozen_catalog_maps_do_not_follow_mutations(self):
        catalog = TagCatalog('fixture', {'lamp': 'lamp'}, {'lamp': 'pyramid:lighting'})
        sorter = load_pyramid(catalog)
        metadata = sorter.metadata()
        catalog.categories['lamp'] = 'pyramid:subject'
        catalog.entries['lamp'] = 'smile'
        self.assertEqual(sorter.sort(('lamp', 'shirt')), ('shirt', 'lamp'))
        self.assertEqual(sorter.metadata(), metadata)
        with self.assertRaises(TypeError):
            sorter.ranks['new'] = 0

    def test_many_permutations_preserve_exact_strings_and_within_group_order(self):
        sorter = load_pyramid()
        values = ['unknown z', 'unknown a', 'moonlight', 'sunlight', 'outdoors', 'forest', 'shirt', 'dress', 'smile', 'running', '1girl', 'blue hair', 'wide shot']
        randomizer = random.Random(84)
        for _ in range(60):
            randomizer.shuffle(values)
            output = sorter.sort(tuple(values))
            self.assertEqual(set(output), set(values))
            self.assertEqual(len(output), len(values))
            self.assertEqual(sorter.sort(output), output)
            for group in (*GROUPS, 'unclassified'):
                self.assertEqual([tag for tag in output if sorter.category(tag) == group],
                                 [tag for tag in values if sorter.category(tag) == group])

    def test_resource_integrity_validation_and_capability_without_fallback(self):
        root, path = resource_fixture(self.root.parent)
        valid = json.loads(path.read_text(encoding='utf-8'))
        invalid = [b'{', b'{"schema_version":1,"schema_version":1}', b'\xff']
        for key, value in (('schema_version', True), ('version', 'other'), ('extra', 1)):
            invalid.append(json.dumps({**valid, key: value}).encode())
        duplicate = json.loads(json.dumps(valid)); duplicate['groups']['lighting'].append('smile')
        invalid.append(json.dumps(duplicate).encode())
        syntax = json.loads(json.dumps(valid)); syntax['groups']['lighting'].append('(shine:1.2)')
        invalid.append(json.dumps(syntax).encode())
        for raw in invalid:
            path.write_bytes(raw)
            with patch('stylelora.caption.sorters.files', return_value=root):
                with self.assertRaises(UnsupportedSorter): load_pyramid()
                self.assertFalse(pyramid_capability()['available'])
        path.unlink()
        with patch('stylelora.caption.sorters.files', return_value=root):
            with self.assertRaises(UnsupportedSorter): load_pyramid()

    def test_merge_rejects_add_remove_rename_duplicates_and_invalid_result_types(self):
        image = self.kept(caption='smile, shirt')
        config = {'tag_sort': 'prompt_pyramid'}
        before = self.database_contents()
        for invalid in (('smile',), ('smile', 'shirt', 'invented'), ('smile', 'renamed'), ('smile', 'smile'),
                        (['smile'], 'shirt'), None, 'smile, shirt', b'smile'):
            with self.subTest(invalid=invalid), self.assertRaises(SorterContractViolation) as caught:
                self.read(merge_caption, image.image_id, config=config, sorter=lambda _: invalid)
            self.assertEqual((safe_error(caught.exception).status, safe_error(caught.exception).code), (422, 'sorter:contract_violation'))
        self.assertEqual(self.database_contents(), before)

    def test_export_trigger_nl_counts_bytes_and_annotations_are_preserved(self):
        image = self.kept(caption='unknown_z, moonlight, outdoors, shirt, wide_shot, smile, 1girl, my_style, unknown_a\n\n完整描述。')
        scheme = self.scheme(include_nl=True)
        before = self.database_contents()
        source = self.root / 'images' / image.filename
        image_bytes = source.read_bytes()
        preview = preview_export(self.project, scheme_id=scheme.scheme_id)
        self.assertEqual(preview['images'][0]['tags'], ('my style', '1girl', 'smile', 'shirt', 'wide shot', 'outdoors', 'moonlight', 'unknown z', 'unknown a'))
        self.assertEqual(preview['images'][0]['sorting_counts']['unclassified'], 2)
        self.assertIn('sorter:unclassified', preview['images'][0]['warnings'])
        report = export_images(self.project, scheme_id=scheme.scheme_id, expected_digest=preview['plan_digest'])
        path = self.root / report['directory'] / report['images'][0]['output_path']
        self.assertEqual(path.with_suffix('.txt').read_bytes(), (', '.join(preview['images'][0]['tags']) + '\n\n完整描述。\n').encode())
        self.assertEqual(path.read_bytes(), image_bytes)
        self.assertEqual(report['sort_summary'], {'images_with_unclassified': 1, 'unclassified_tag_occurrences': 2})
        self.assertEqual(report['sorter'], preview['sorter'])
        self.assertEqual(report['trigger_count'], 1)
        self.assertEqual(self.database_contents(), before)

    def test_blocklist_is_separate_and_counts_only_remaining_content(self):
        self.kept(caption='moonlight, artist_name, smile, 1girl, sitting, running')
        scheme = self.scheme(blocklist_mode='warn')
        options = ExportOptions(blocked_tags=('artist_name',))
        preview = preview_export(self.project, scheme_id=scheme.scheme_id, options=options)
        self.assertEqual(preview['images'][0]['tags'], ('my style', '1girl', 'smile', 'sitting', 'running', 'moonlight', 'artist name'))
        removed = preview_export(self.project, scheme_id=scheme.scheme_id, options=ExportOptions(blocked_tags=('artist_name',), blocklist_mode='remove'))
        self.assertNotIn('artist name', removed['images'][0]['tags'])
        self.assertEqual(removed['images'][0]['sorting_counts']['unclassified'], 0)

    def test_wd_threshold_null_policy_and_manual_removal_keep_evidence_semantics(self):
        image = self.kept(caption='moonlight, 1girl')
        self.model_result(image.image_id, (Tag('shirt', 0.1), Tag('smile', None), Tag('running', 0.9)))
        edit_manual(self.project, image.image_id, tags=('1girl', 'shirt', 'smile', 'running'))
        strict = self.scheme(wd_min_confidence=0.5)
        include = self.scheme(wd_min_confidence=0.5, wd_null_confidence='include')
        before = self.database_contents()
        self.assertEqual(preview_export(self.project, scheme_id=strict.scheme_id)['images'][0]['tags'], ('my style', '1girl', 'running'))
        self.assertEqual(preview_export(self.project, scheme_id=include.scheme_id)['images'][0]['tags'], ('my style', '1girl', 'smile', 'running'))
        self.assertEqual(self.database_contents(), before)

    def test_default_order_and_unavailable_sorter_on_empty_selection(self):
        self.kept(caption='moonlight, shirt, 1girl')
        self.assertEqual(preview_export(self.project)['images'][0]['tags'], ('my style', 'moonlight', 'shirt', '1girl'))
        self.assertIsNone(preview_export(self.project)['sorter'])
        scheme = self.scheme(tag_layers=[])
        with patch('stylelora.caption.sorters.files', side_effect=FileNotFoundError):
            with self.assertRaises(UnsupportedSorter): preview_export(self.project, scheme_id=scheme.scheme_id)
            self.assertIsNone(preview_export(self.project)['sorter'])
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_resource_change_invalidates_plan_even_if_output_would_not_change(self):
        self.kept(caption='smile, 1girl')
        scheme = self.scheme()
        root, path = resource_fixture(self.root.parent)
        with patch('stylelora.caption.sorters.files', return_value=root):
            preview = preview_export(self.project, scheme_id=scheme.scheme_id)
            value = json.loads(path.read_text()); value['groups']['lighting'].append('unused exact light')
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(PreviewStale):
                export_images(self.project, scheme_id=scheme.scheme_id, expected_digest=preview['plan_digest'])
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_sorter_is_frozen_once_snapshot_is_made(self):
        self.kept(caption='moonlight, smile')
        scheme = self.scheme()
        root, path = resource_fixture(self.root.parent)
        original = path.read_bytes()
        def fault(stage):
            if stage == 'export_after_snapshot':
                value = json.loads(original)
                value['groups']['action_expression'].remove('smile')
                value['groups']['lighting'].append('smile')
                path.write_text(json.dumps(value), encoding='utf-8')
        self.project._fault_hook = fault
        with patch('stylelora.caption.sorters.files', return_value=root):
            report = export_images(self.project, scheme_id=scheme.scheme_id)
        txt = next((self.root / report['directory']).glob('*.txt')).read_text(encoding='utf-8')
        self.assertEqual(txt, 'my style, smile, moonlight\n')
        self.assertEqual(report['sorter']['rules_sha256'], hashlib.sha256(original).hexdigest())

    def test_hardlink_and_pyramid_share_the_same_frozen_plan(self):
        image = self.kept(caption='moonlight, smile, 1girl')
        body = ExportScheme(name='links', caption_config_json={'tag_sort': 'prompt_pyramid'}, output_config_json={'image_mode': 'hardlink'}).model_dump()
        body['pipeline_json'] = ()
        scheme = self.write(schemes.create_export_scheme, ExportSchemeInput(**body))
        preview = preview_export(self.project, scheme_id=scheme.scheme_id)
        report = export_images(self.project, scheme_id=scheme.scheme_id, hardlink_ack_digest=preview['hardlink_ack_digest'])
        path = self.root / report['directory'] / report['images'][0]['output_path']
        self.assertTrue(os.path.samefile(path, self.root / 'images' / image.filename))
        self.assertEqual(path.with_suffix('.txt').read_text(encoding='utf-8'), 'my style, 1girl, smile, moonlight\n')

    def test_cli_exports_the_selected_sorter_without_a_model(self):
        from stylelora.cli import main
        self.kept(caption='moonlight, smile, 1girl')
        scheme = self.scheme()
        close_project(self.project)
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(['export', str(self.root), '--scheme-id', str(scheme.scheme_id)])
        self.assertEqual(status, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['sorter']['name'], 'prompt_pyramid')
        self.assertEqual(next((self.root / report['directory']).glob('*.txt')).read_text(encoding='utf-8'), 'my style, 1girl, smile, moonlight\n')
        self.reopen()


class PyramidAPITests(APITest):
    def scheme(self):
        return self.request('POST', self.p + '/export-schemes', {'name': '排序', 'caption_config_json': {'tag_sort': 'prompt_pyramid'}}, status=201)

    def preview(self, scheme, expected='done'):
        job = self.request('POST', self.p + '/exports/preview', {'scheme_id': scheme['scheme_id']}, status=202)
        return self.wait_operation(job, expected=expected)

    def catalog(self, category):
        value = {'schema_version': 1, 'kind': 'tag_catalog', 'tags': [{'tag': 'blue_hair', 'category': category}]}
        response = self.client.post('/api/tags/imports/preview', files={'file': ('catalog.json', json.dumps(value).encode(), 'application/json')},
            data={'kind': 'catalog', 'format': 'json'}, headers={'Idempotency-Key': str(uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.request('POST', '/api/tags/imports/commit', {'preview_id': preview['preview_id'], 'digest': preview['digest']}, version=preview['version'])

    def test_capability_scheme_preview_export_and_persistent_report(self):
        self.import_inputs(); self.keep([1])
        caps = self.request('GET', '/api/exchange/capabilities')
        self.assertIn('prompt_pyramid', caps['sorters'])
        self.assertTrue(caps['sorter_details']['prompt_pyramid']['available'])
        self.assertEqual(caps['default_export']['caption_config_json']['tag_sort'], 'layer_first')
        scheme = self.scheme()
        self.assertTrue(scheme['executable'])
        preview = self.preview(scheme)['result']
        exported = self.run_export(preview)
        report = self.request('GET', exported['report_url'])
        self.assertEqual(report['sorter'], preview['sorter'])
        self.assertEqual(report['images'][0]['sorting_counts'], preview['images'][0]['sorting_counts'])
        self.assertEqual(report['counts']['exported'], 1)
        self.runtime.dataset.exchange.exports.clear()
        self.assertEqual(self.request('GET', exported['report_url'])['sorter'], report['sorter'])

    def test_unavailable_resources_disable_only_pyramid_and_fail_before_output(self):
        self.import_inputs(); self.keep([1])
        scheme = self.scheme()
        with patch('stylelora.caption.sorters.files', side_effect=FileNotFoundError):
            self.assertNotIn('prompt_pyramid', self.request('GET', '/api/exchange/capabilities')['sorters'])
            view = self.request('GET', self.p + '/export-schemes/' + str(scheme['scheme_id']))
            self.assertFalse(view['executable'])
            self.assertEqual(view['unavailable_reasons'], ['sorter:unsupported'])
            self.assertEqual(self.preview(scheme, 'failed')['error']['code'], 'sorter:unsupported')
            self.assertTrue(self.export_preview()['dry_run'])
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_sorter_contract_violation_is_visible_and_never_publishes(self):
        self.import_inputs(); self.keep([1])
        scheme = self.scheme()
        with patch.object(PyramidSorter, 'sort', return_value=('invented',)):
            self.assertEqual(self.preview(scheme, 'failed')['error']['code'], 'sorter:contract_violation')
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_updated_catalog_category_requires_new_preview(self):
        self.import_inputs(); self.keep([1])
        scheme = self.scheme()
        old = self.preview(scheme)['result']
        self.catalog('pyramid:lighting')
        self.request('POST', self.p + '/exports', {'preview_id': old['preview_id'], 'digest': old['digest']}, status=409)
        fresh = self.preview(scheme)['result']
        self.assertEqual(fresh['images'][0]['tags'], ['http style', 'smile', 'blue hair'])
        self.assertNotEqual(old['sorter']['catalog_sha256'], fresh['sorter']['catalog_sha256'])
        self.assertFalse(list((self.root / 'exports').iterdir()))

    def test_resource_change_during_http_preview_lifetime_rejects_worker(self):
        self.import_inputs(); self.keep([1])
        scheme = self.scheme()
        root, path = resource_fixture(self.base)
        with patch('stylelora.caption.sorters.files', return_value=root):
            preview = self.preview(scheme)['result']
            path.with_suffix('.md').write_text('changed local contract', encoding='utf-8')
            result = self.request('POST', self.p + '/exports', {'preview_id': preview['preview_id'], 'digest': preview['digest']}, status=202)
            self.assertEqual(self.wait_operation({'operation_id': result['operation_id']}, expected='failed')['error']['code'], 'concurrency:version_conflict')
        self.assertFalse(list((self.root / 'exports').iterdir()))
