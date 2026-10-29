from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from stylelora.prompt_production.coverage import CoveragePlans, MAX_TEMPLATE
from stylelora.prompt_production.storage import ProductFiles
from stylelora.prompt_production.sampling import cell_id
from stylelora.runtime.errors import ServiceError
from . import test_prompt_production as support


class CoveragePlanTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait

    def document(self, **plan):
        return {'format': 'json', 'content': json.dumps({'schema_version': 1, 'name': '覆盖样例',
                'plan': {'target_count': 12, 'random_seed': 42, **plan}}, ensure_ascii=False)}

    def create(self, document=None, key=None):
        identifier = str(uuid4())
        self.request('POST', '/coverage-plans', {'coverage_id': identifier, **(document or self.document())}, status=201, key=key)
        return self.request('GET', '/coverage-plans/' + identifier)

    def test_create_update_cas_and_durable_receipts(self):
        identifier, key = str(uuid4()), str(uuid4())
        body = {'coverage_id': identifier, **self.document()}
        created = self.request('POST', '/coverage-plans', body, status=201, key=key)
        self.assertEqual(created, self.request('POST', '/coverage-plans', body, status=201, key=key))
        self.request('POST', '/coverage-plans', {**body, **self.document(target_count=5)}, status=409, key=key)
        self.request('POST', '/coverage-plans', {**body, 'coverage_id': str(uuid4())}, status=409, key=key)
        self.request('POST', '/coverage-plans', body, status=409)
        path = '/coverage-plans/' + identifier
        self.request('PUT', path, self.document(target_count=24), status=428)
        changed_key = str(uuid4())
        updated = self.request('PUT', path, self.document(target_count=24), version=created['version'], key=changed_key)
        self.assertEqual(updated['revision'], 2)
        self.request('PUT', path, self.document(target_count=36), version=created['version'], status=409)
        restarted = CoveragePlans(self.runtime, ProductFiles(self.service.files.directory))
        self.assertEqual(restarted.get(identifier)['target_count'], 24)
        self.assertEqual(restarted.save(identifier, self.document(target_count=24), created['version'], changed_key), updated)
        self.assertEqual(restarted.save(identifier, self.document(), None, key, create=True), created)

    def test_json_yaml_lossless_portable_roundtrip(self):
        key = cell_id(['scene', 'lighting'], ['forest', 'night'])
        saved = self.create(self.document(cell_weights={key: 7}, constraints=[{'dimension': 'count', 'tag': 'no humans', 'minimum_ratio': .15}],
                                          exclude=[{'all_tags': ['1girl'], 'none_tags': ['forest']}], no_humans_excludes=['clothing']))
        template = json.loads(saved['template'])
        for format in ('json', 'yaml'):
            exported = self.request('GET', '/coverage-plans/' + saved['coverage_id'] + '/export?format=' + format)
            preview = self.request('POST', '/coverage-plans/preview', {'format': format, 'content': exported['content']})
            self.assertEqual(json.loads(preview['template']), template)
            self.assertEqual(preview['cell_count'], 6)
            self.assertNotIn(saved['coverage_id'], exported['content'])
            self.assertNotIn('_operations', exported['content'])
            clone = self.create({'format': format, 'content': exported['content']})
            self.assertNotEqual(clone['coverage_id'], saved['coverage_id'])
            self.assertEqual(clone['template'], saved['template'])
        self.assertEqual(self.request('GET', '/coverage-plans')['total'], 3)
        self.request('GET', '/coverage-plans/' + saved['coverage_id'] + '/export?format=txt', status=422)

    def test_preview_has_no_disk_or_task_side_effects(self):
        preview = self.request('POST', '/coverage-plans/preview', {'format': 'yaml', 'content': '\ufeffname: 本地\nplan:\n  target_count: 9\n  random_seed: null\n'})
        self.assertEqual(preview['target_count'], 9)
        self.assertIsNone(preview['random_seed'])
        self.assertTrue(preview['browser_compatible'])
        self.assertEqual(self.service.files.ids('coverage'), [])
        self.assertEqual(self.service.list(), [])

    def test_uint64_seed_never_crosses_browser_boundary_as_number(self):
        saved = self.create(self.document(random_seed=2**64-1))
        self.assertEqual(saved['random_seed'], str(2**64-1))
        self.assertFalse(saved['browser_compatible'])
        self.assertEqual(json.loads(saved['template'])['plan']['random_seed'], 2**64-1)
        exported = self.request('GET', '/coverage-plans/' + saved['coverage_id'] + '/export?format=yaml')
        preview = self.request('POST', '/coverage-plans/preview', {'format': 'yaml', 'content': exported['content']})
        self.assertEqual(preview['template'], saved['template'])
        self.assertTrue(self.create(self.document(random_seed=2**53-1))['browser_compatible'])

    def test_reject_duplicate_keys_at_every_level(self):
        documents = [('json', '{"name":"a","name":"b","plan":{}}'),
                     ('json', '{"name":"a","plan":{"target_count":1,"target_count":2}}'),
                     ('yaml', 'name: a\nname: b\nplan: {}'),
                     ('yaml', 'name: a\nplan:\n  target_count: 1\n  target_count: 2')]
        for format, content in documents:
            with self.subTest(format=format, content=content):
                result = self.request('POST', '/coverage-plans/preview', {'format': format, 'content': content}, status=422)
                self.assertEqual(result['error']['code'], 'coverage:invalid_template')
        self.assertFalse(self.service.files.ids('coverage'))

    def test_yaml_objects_aliases_merges_non_json_types_and_multiple_documents_rejected(self):
        samples = ['!!python/object/apply:os.system [echo unsafe]', 'name: !!python/name:os.system\nplan: {}',
                   'name: &name a\nplan: {x: *name}', 'name: a\nplan: &plan {x: *plan}',
                   'name: a\nplan: {<<: {target_count: 1}}', 'name: 2026-10-26\nplan: {}',
                   'name: a\nplan: !!set {x: null}', 'name: !!binary YQ==\nplan: {}',
                   '1: a\nplan: {}', '? [a,b]\n: c', 'name: a\nplan: {}\n---\nname: b\nplan: {}']
        for content in samples:
            with self.subTest(content=content):
                self.request('POST', '/coverage-plans/preview', {'format': 'yaml', 'content': content}, status=422)

    def test_template_schema_rejects_unknown_fields_invalid_weights_and_nonfinite(self):
        samples = [self.document(target_count=True), self.document(random_seed=-1), self.document(random_seed=2**64),
                   self.document(schema_version=2), self.document(primary_dimensions=['scene']),
                   self.document(cell_weights={'forest|night': 3}), self.document(constraints=[{'dimension': 'x', 'tag': 'x', 'minimum_ratio': float('nan')}]),
                   {'format': 'json', 'content': '{"name":"a","plan":{},"coverage_id":"ignore"}'},
                   {'format': 'yaml', 'content': 'name: a\nplan:\n  constraints: [{dimension: x, tag: x, minimum_ratio: .inf}]'}]
        for doc in samples:
            with self.subTest(doc=doc):
                self.request('POST', '/coverage-plans/preview', doc, status=422)
        self.request('POST', '/coverage-plans/preview', {'format': 'xml', 'content': ''}, status=422)

    def test_structure_and_utf8_limits(self):
        for format in ('json', 'yaml'):
            self.request('POST', '/coverage-plans/preview', {'format': format, 'content': '[' * 40 + '0' + ']' * 40}, status=422)
            self.request('POST', '/coverage-plans/preview', {'format': format, 'content': '[' + ','.join('0' for _ in range(100001)) + ']'}, status=422)
        self.request('POST', '/coverage-plans/preview', {'format': 'json', 'content': '中' * (MAX_TEMPLATE // 2)}, status=413)

    def test_paging_detects_changed_list(self):
        first, second = self.create(), self.create()
        page = self.request('GET', '/coverage-plans?limit=1')
        self.assertEqual(page['total'], 2)
        self.assertNotIn('template', page['items'][0])
        self.request('PUT', '/coverage-plans/' + second['coverage_id'], self.document(target_count=24), version=second['version'])
        self.request('GET', '/coverage-plans?limit=1&cursor=' + page['next_cursor'], status=409)
        self.request('GET', '/coverage-plans/' + first['coverage_id'] + '?unknown=1', status=422)

    def test_failed_atomic_write_preserves_original_and_allows_retry(self):
        saved = self.create()
        key, path = str(uuid4()), '/coverage-plans/' + saved['coverage_id']
        with patch('stylelora.prompt_production.storage.atomic_json', side_effect=OSError('disk failure')):
            self.request('PUT', path, self.document(target_count=24), version=saved['version'], key=key, status=503)
        self.assertEqual(self.request('GET', path), saved)
        self.request('PUT', path, self.document(target_count=24), version=saved['version'], key=key)
        self.assertEqual(self.request('GET', path)['revision'], 2)

    def test_concurrent_edit_has_one_winner(self):
        saved = self.create()
        barrier = threading.Barrier(2)
        def edit(count):
            barrier.wait()
            try:
                return self.service.coverage_plans.save(saved['coverage_id'], self.document(target_count=count), saved['version'], str(uuid4()))
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(edit, [24, 36]))
        self.assertEqual(sum(isinstance(value, dict) for value in values), 1)
        self.assertIn('concurrency:version_conflict', values)

    def test_corrupt_file_cannot_be_overwritten(self):
        saved = self.create()
        value = self.service.files.get('coverage', saved['coverage_id'])
        value['revision'] = 'broken'
        self.service.files.put('coverage', saved['coverage_id'], value)
        self.request('GET', '/coverage-plans/' + saved['coverage_id'], status=503)
        self.request('PUT', '/coverage-plans/' + saved['coverage_id'], self.document(), version=saved['version'], status=503)
        self.assertEqual(self.service.files.get('coverage', saved['coverage_id']), value)

    def test_saved_plan_edits_do_not_mutate_existing_task_snapshot(self):
        saved = self.create()
        request = self.sampling(self.dictionary(), plan=json.loads(saved['template'])['plan'])
        task, _ = self.start(request)
        completed = self.wait(task['task_id'])
        snapshot = deepcopy(self.service.load(task['task_id'])['snapshot'])
        self.request('PUT', '/coverage-plans/' + saved['coverage_id'], self.document(target_count=24, random_seed=77), version=saved['version'])
        self.assertEqual(self.service.load(task['task_id'])['snapshot'], snapshot)
        self.assertEqual(completed['random_seed'], 42)
        self.assertEqual(completed['requested'], 12)
        self.assertNotIn(saved['coverage_id'], json.dumps(snapshot))
        frozen = self.request('POST', '/tasks/' + task['task_id'] + '/seeds', {'expected_revision': completed['revision']})
        self.assertTrue(frozen)
