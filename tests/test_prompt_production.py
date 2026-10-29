from copy import deepcopy
import itertools
import json
from pathlib import Path
import tempfile
import random
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from stylelora.api.app import create_app
from stylelora.project_db.validation import digest
from stylelora.prompt_production import exchange
from stylelora.prompt_production.sampling import cell_id, constraint_bounds, quotas, sample
from stylelora.prompt_production.tasks import PromptProduction
from stylelora.prompt_production.types import DictionaryInput, Entry, Plan, SamplingRequest
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.lifecycle import Runtime


def entry(tags, **kw):
    return Entry(tags=tags, **kw).model_dump()


def feature_body():
    return {'kind': 'features', 'name': '覆盖词典', 'dimensions': {
        'scene': [{'tag': tag, 'rating': 'general'} for tag in ('forest', 'beach', 'city street')],
        'lighting': [{'tag': tag, 'rating': 'general'} for tag in ('sunlight', 'night')],
        'count': [{'tag': tag, 'rating': 'general'} for tag in ('no humans', '1girl', '1boy', '2girls')],
        'view': [{'tag': tag, 'rating': 'general'} for tag in ('wide shot', 'full body', 'upper body')],
        'angle': [{'tag': tag, 'rating': 'general'} for tag in ('from side', 'from above')],
        'clothing': [{'tag': 'outfit ' + str(i), 'rating': 'general'} for i in range(5)]}}


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='prompt-production-', dir=Path('.test-tmp'))
        self.root = Path(self.temp.name)
        self.runtime = Runtime(product_dir=self.root/'tagging', llm_dir=self.root/'product')
        self.service = self.runtime.prompts
        self.client = TestClient(create_app(self.runtime), base_url=self.runtime.origin)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer ' + self.runtime.token, 'Origin': self.runtime.origin}
        self.p = '/api/prompt-production'

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.runtime.shutdown()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, status=200, version=None, key=None):
        headers = {**self.headers, 'Idempotency-Key': key or str(uuid4())}
        if version is not None:
            headers['If-Match'] = '"' + version + '"'
        response = self.client.request(method, self.p + path, json=body, headers=headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def dictionary(self, body=None):
        value = {'dictionary_id': str(uuid4()), **(body or feature_body())}
        self.request('POST', '/dictionaries', value, status=201)
        return self.request('GET', '/dictionaries/' + value['dictionary_id'])

    def sampling(self, dictionary, **kw):
        request = {'feature_dictionary_ids' if dictionary['kind'] == 'features' else 'prompt_dictionary_ids': [dictionary['dictionary_id']],
                   'plan': {'target_count': 12, 'random_seed': 42}, **kw}
        return SamplingRequest.model_validate(request).model_dump()

    def start(self, request):
        preview = self.request('POST', '/coverage/preview', request)
        body = {'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}
        return self.request('POST', '/tasks', body, status=202), body

    def wait(self, identifier, status='done'):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.request('GET', '/tasks/' + identifier)
            if value['status'] == status:
                return value
            time.sleep(.01)
        self.fail(str(value))

    def test_strict_schema_normalization_and_invalid_contracts(self):
        self.assertEqual(entry(['Wide_Shot', ' wide   shot '])['tags'], ['wide shot'])
        for payload in ({'tags': []}, {'tags': ['(red:1.2)']}, {'tags': ['a'], 'rating': 'safe'}, {'tags': ['a'], 'aspect': 'large'}):
            with self.assertRaises(ValidationError):
                Entry.model_validate(payload)
        for plan in ({'target_count': True}, {'schema_version': True}, {'schema_version': 2},
                     {'random_seed': -1}, {'constraints': [{'dimension': 'count', 'tag': 'a', 'minimum_ratio': float('nan')}]},
                     {'constraints': [{'dimension': 'count', 'tag': 'a', 'minimum_ratio': True}]}):
            with self.assertRaises(ValidationError):
                Plan.model_validate(plan)
        self.request('POST', '/dictionaries', {'dictionary_id': str(uuid4()), 'kind': 'features', 'name': 'bad', 'dimensions': {}}, status=422)

    def test_dictionary_cas_receipts_atomic_failure_and_restart(self):
        identifier, key = str(uuid4()), str(uuid4())
        body = {'dictionary_id': identifier, **feature_body()}
        created = self.request('POST', '/dictionaries', body, status=201, key=key)
        self.assertEqual(created, self.request('POST', '/dictionaries', body, status=201, key=key))
        changed = {**feature_body(), 'name': '改名'}
        self.request('PUT', '/dictionaries/' + identifier, changed, status=428)
        self.request('PUT', '/dictionaries/' + identifier, changed, version='wrong', status=409)
        with patch('stylelora.prompt_production.storage.atomic_json', side_effect=OSError('private path')):
            result = self.request('PUT', '/dictionaries/' + identifier, changed, version=created['version'], status=503)
            self.assertNotIn('private path', json.dumps(result))
        self.assertEqual(self.request('GET', '/dictionaries/' + identifier)['name'], body['name'])
        update_key = str(uuid4())
        updated = self.request('PUT', '/dictionaries/' + identifier, changed, version=created['version'], key=update_key)
        restored = PromptProduction(self.runtime, self.root/'product')
        self.assertEqual(restored.dictionaries.get(identifier)['revision'], 2)
        self.assertEqual(restored.dictionaries.save(identifier, DictionaryInput.model_validate(changed).model_dump(), 'stale', update_key), updated)
        self.request('POST', '/dictionaries', {**body, 'name': 'conflict'}, key=key, status=409)

    def test_dictionary_variants_and_rating_conflict(self):
        values = [entry(['forest'], natural_language='River', aspect='landscape', rating='general'),
                  entry(['forest'], natural_language='River', aspect='square', rating='general'),
                  entry(['forest'], natural_language='Lake', aspect='landscape', rating='general')]
        d = self.dictionary({'kind': 'prompts', 'name': '内容', 'entries': values + values[:1]})
        self.assertEqual(len(d['entries']), 3)
        self.request('PUT', '/dictionaries/' + d['dictionary_id'], {'kind': 'prompts', 'name': '内容',
                     'entries': values + [{**values[0], 'rating': 'explicit'}]}, version=d['version'], status=409)
        report = self.service.preview(self.sampling(d, coverage_enabled=False, plan={'target_count': 2, 'random_seed': 7}))
        self.assertFalse(report['ready'])
        self.assertEqual(report['available'], 1)

    def test_dictionary_full_edit_retains_order_null_fields_and_frozen_task(self):
        original = [entry(['no humans', 'forest'], natural_language='A forest.', aspect='landscape', rating='general'),
                    entry(['no humans', 'beach'], rating='general')]
        d = self.dictionary({'kind': 'prompts', 'name': '编辑前', 'entries': original})
        task, _ = self.start(self.sampling(d, coverage_enabled=False, plan={'target_count': 2, 'random_seed': 7}))
        self.wait(task['task_id'])
        old_task = self.service.files.get('prompt_tasks', task['task_id'])
        old_outcomes = self.request('GET', '/tasks/' + task['task_id'] + '/outcomes')
        changed = [entry(['no humans', 'BEACH', 'beach'], natural_language='New text.', aspect='square'),
                   entry(['no humans', 'mountain'], rating='sensitive')]
        body = {'kind': 'prompts', 'name': '编辑后', 'entries': changed}
        key = str(uuid4())
        result = self.request('PUT', '/dictionaries/' + d['dictionary_id'], body, version=d['version'], key=key)
        self.assertEqual(result, self.request('PUT', '/dictionaries/' + d['dictionary_id'], body, version=d['version'], key=key))
        saved = self.request('GET', '/dictionaries/' + d['dictionary_id'])
        self.assertEqual(saved['entries'], changed)
        self.assertEqual(saved['entries'][0]['tags'], ['no humans', 'beach'])
        self.assertIsNone(saved['entries'][0]['rating'])
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(self.service.files.get('prompt_tasks', task['task_id']), old_task)
        self.assertEqual(self.request('GET', '/tasks/' + task['task_id'] + '/outcomes'), old_outcomes)

    def test_dictionary_invalid_full_edits_do_not_partially_save(self):
        d = self.dictionary()
        path = '/dictionaries/' + d['dictionary_id']
        for dimensions in ({}, {'scene': []}, {'scene': [{'tag': ''}]},
                           {'scene': [{'tag': 'forest'}], ' scene ': [{'tag': 'beach'}]},
                           {'scene': [{'tag': '(forest:1.2)'}]}):
            self.request('PUT', path, {'kind': 'features', 'name': '非法编辑', 'dimensions': dimensions}, version=d['version'], status=422)
            self.assertEqual(self.request('GET', path), d)

    def test_json_csv_roundtrip_and_private_receipts_not_exported(self):
        d = self.dictionary({'kind': 'prompts', 'name': '中文', 'entries': [entry(['forest'], natural_language='A river, "blue".\nA tree.', aspect='landscape', rating='general')]})
        for format in ('json', 'csv'):
            response = self.client.get(self.p + '/dictionaries/' + d['dictionary_id'] + '/export?format=' + format, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'_operations', response.content)
            payload, report = exchange.parse(response.content, format, 'prompts', None, self.runtime.catalog.snapshot())
            self.assertTrue(report['valid'], report)
            self.assertEqual(payload['entries'], d['entries'])
        response = self.client.get(self.p + '/dictionaries/' + d['dictionary_id'] + '/export?format=txt&acknowledge_lossy=true',
                                   headers={**self.headers, 'If-Match': '"' + d['version'] + '"'})
        self.assertIn('aspect', response.headers['X-Prompt-Lossy-Fields'])

    def test_import_diagnostics_include_errors_beyond_first_hundred_and_commit_guard(self):
        d = self.dictionary({'kind': 'prompts', 'name': '导入', 'entries': []})
        content = 'tags,natural_language,aspect,rating\n' + '\n'.join('"[""forest""]",,,general' for _ in range(100)) + '\n"[]",,,general\n'
        response = self.client.post(self.p + '/imports/preview', headers={**self.headers, 'Idempotency-Key': str(uuid4())},
                                    files={'file': ('content.csv', content.encode(), 'text/csv')}, data={'kind': 'prompts', 'format': 'csv'})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertFalse(preview['valid'])
        self.assertEqual(preview['issues'][0]['row'], 102)
        self.request('POST', '/dictionaries/' + d['dictionary_id'] + '/imports', {'preview_id': preview['preview_id'], 'digest': preview['digest']}, version=d['version'], status=422)
        self.assertEqual(self.request('GET', '/dictionaries/' + d['dictionary_id'])['entries'], [])

    def test_import_append_replay_and_feature_rating_merge(self):
        d = self.dictionary({'kind': 'features', 'name': '特征', 'dimensions': {'scene': [{'tag': 'forest', 'rating': 'general'}]}})
        response = self.client.post(self.p + '/imports/preview', headers={**self.headers, 'Idempotency-Key': str(uuid4())},
                                    files={'file': ('../ignored.txt', b'forest\texplicit\nbeach\tgeneral', 'text/plain')},
                                    data={'kind': 'features', 'format': 'txt', 'dimension': 'scene'})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        body, key = {'preview_id': preview['preview_id'], 'digest': preview['digest']}, str(uuid4())
        result = self.request('POST', '/dictionaries/' + d['dictionary_id'] + '/imports', body, version=d['version'], key=key)
        again = self.request('POST', '/dictionaries/' + d['dictionary_id'] + '/imports', body, version=d['version'], key=key)
        self.assertEqual(result, again)
        self.runtime.previews.values.clear()
        self.assertEqual(result, self.request('POST', '/dictionaries/' + d['dictionary_id'] + '/imports', body, version=d['version'], key=key))
        saved = self.request('GET', '/dictionaries/' + d['dictionary_id'])
        self.assertEqual(saved['dimensions']['scene'][0]['rating'], 'explicit')
        self.assertEqual(len(saved['dimensions']['scene']), 2)

    def test_import_digest_binds_content_beyond_preview_sample(self):
        raw = {'schema_version': 1, 'kind': 'prompts', 'dictionary_id': str(uuid4()), 'revision': 1,
               'name': '完整摘要', 'entries': [entry(['item ' + str(i)]) for i in range(101)]}
        _, first = exchange.parse(json.dumps(raw).encode(), 'json', 'prompts', None, self.runtime.catalog.snapshot())
        raw['entries'][100]['tags'] = ['changed last item']
        _, second = exchange.parse(json.dumps(raw).encode(), 'json', 'prompts', None, self.runtime.catalog.snapshot())
        self.assertEqual(first['examples'], second['examples'])
        self.assertNotEqual(first['content_digest'], second['content_digest'])

    def test_seed_determinism_order_independence_and_exact_six_cell_quota(self):
        d = self.dictionary()
        body = self.sampling(d, plan={'target_count': 100, 'random_seed': 123})
        frozen = self.service.freeze(body)
        first = sample(frozen)
        self.assertTrue(first['ready'])
        self.assertEqual(sorted(first['cell_targets'].values()), [16, 16, 17, 17, 17, 17])
        self.assertEqual(first['cell_targets'], first['cell_counts'])
        self.assertEqual(len({tuple(sorted(row['entry']['tags'])) for row in first['outcomes']}), 100)
        changed = deepcopy(frozen)
        changed['dictionaries'][0]['dimensions'] = dict(reversed(list(changed['dictionaries'][0]['dimensions'].items())))
        for rows in changed['dictionaries'][0]['dimensions'].values():
            rows.reverse()
        self.assertEqual(first, sample(changed))

    def test_overlapping_ratios_upper_bounds_and_no_humans_template_rule(self):
        d = self.dictionary()
        body = self.sampling(d, plan={'target_count': 100, 'random_seed': 17, 'no_humans_excludes': ['clothing'],
             'constraints': [{'dimension': 'count', 'tag': 'no humans', 'minimum_ratio': .15},
                             {'dimension': 'view', 'tag': 'wide shot', 'minimum_ratio': .1}]})
        result = sample(self.service.freeze(body))
        self.assertTrue(result['ready'], result['status'])
        self.assertGreaterEqual(result['constraint_counts']['no humans'], 15)
        self.assertGreaterEqual(result['constraint_counts']['wide shot'], 10)
        for row in result['outcomes']:
            if 'no humans' in row['entry']['tags']:
                self.assertFalse(any(tag.startswith('outfit ') for tag in row['entry']['tags']))
        # Exact decimal rounding, not a floating-point ceil(100 * .07) surprise.
        plan = Plan(target_count=100, constraints=[{'dimension': 'v', 'tag': 'a', 'minimum_ratio': .07}]).model_dump()
        self.assertEqual(constraint_bounds(plan)['a'][0], 7)

    def test_unsatisfiable_and_search_exhaustion_are_distinct(self):
        d = self.dictionary()
        frozen = self.service.freeze(self.sampling(d))
        self.assertEqual(sample(frozen, node_limit=1)['status'], 'search_exhausted')
        self.assertEqual(sample(frozen, candidate_limit=1)['status'], 'search_exhausted')
        frozen['request']['plan']['target_count'] = 2
        self.assertEqual(sample(frozen)['status'], 'unsatisfiable')
        frozen['request']['plan']['target_count'] = 12
        frozen['request']['plan']['constraints'] = [{'dimension': 'count', 'tag': '1girl', 'minimum_count': 8, 'maximum_count': 7,
                                                   'minimum_ratio': None, 'maximum_ratio': None}]
        self.assertEqual(sample(frozen)['status'], 'unsatisfiable')

    def test_minimum_constraint_preserves_random_diversity_after_its_prefix(self):
        body = feature_body()
        body['dimensions'].pop('clothing')
        body['dimensions']['season'] = [{'tag': tag, 'rating': 'general'} for tag in ('spring', 'summer', 'autumn', 'winter')]
        d = self.dictionary(body)
        request = self.sampling(d, plan={'target_count': 100, 'random_seed': 42,
            'constraints': [{'dimension': 'count', 'tag': 'no humans', 'minimum_ratio': .15},
                            {'dimension': 'view', 'tag': 'wide shot', 'minimum_ratio': .1}]})
        report = self.service.preview(request)
        self.assertTrue(report['ready'])
        self.assertGreaterEqual(report['constraint_counts']['no humans'], 15)
        self.assertLess(report['constraint_counts']['no humans'], 100)
        self.assertEqual(report['cell_counts'], report['cell_targets'])

    def test_weights_exclusions_and_unclassified_candidates(self):
        d = self.dictionary()
        body = self.sampling(d)
        plan = body['plan']
        plan['exclude'] = [{'all_tags': ['beach', 'night'], 'none_tags': []}]
        weighted = cell_id(['scene', 'lighting'], ['forest', 'sunlight'])
        plan['cell_weights'] = {weighted: 9}
        result = self.service.preview(body)
        self.assertEqual(len(result['cell_targets']), 5)
        self.assertGreater(result['cell_targets'][weighted], 2)
        plain = self.dictionary({'kind': 'prompts', 'name': '缺少主维', 'entries': [entry(['forest'])]})
        result = self.service.preview(self.sampling(plain))
        self.assertEqual(result['diagnostics']['unclassified'], 1)
        self.assertFalse(result['ready'])

    def test_fence_unknown_keyword_lower_bound_content_and_no_paid_calls(self):
        d = self.dictionary({'kind': 'prompts', 'name': '分级', 'entries': [
            entry(['forest'], rating=None), entry(['beach'], rating='explicit'),
            entry(['city'], rating='general'), entry(['oil painting'], rating='general')]})
        body = self.sampling(d, coverage_enabled=False, plan={'target_count': 1, 'random_seed': 42})
        self.runtime.vision.policy.current['keywords'] = [{'tag': 'city', 'rating': 'questionable'}]
        with patch.object(self.runtime.llm, 'complete_json', side_effect=AssertionError('No remote call')):
            result = self.service.preview(body)
        self.assertTrue(result['ready'])
        self.assertEqual(result['diagnostics']['fenced'], 2)
        self.assertEqual(result['diagnostics']['forbidden_content'], 1)
        self.assertFalse(result['examples'][0]['verified'])
        body['fence']['nsfwfallback'] = True
        self.assertFalse(self.service.preview(body)['ready'])
        body['optimize'] = True
        self.request('POST', '/coverage/preview', body)

    def test_task_http_frozen_seeds_persistence_and_no_project(self):
        d = self.dictionary()
        started, body = self.start(self.sampling(d))
        done = self.wait(started['task_id'])
        seeds = self.request('POST', '/tasks/' + done['task_id'] + '/seeds', {'expected_revision': done['revision']})
        self.assertEqual(seeds['count'], 12)
        self.assertEqual([v['seq'] for v in seeds['items']], list(range(1, 13)))
        self.assertEqual(set(seeds['items'][0]), {'seq', 'prompt', 'aspect'})
        self.assertIsNone(self.runtime.context)
        restarted = PromptProduction(self.runtime, self.root/'product')
        self.assertEqual(restarted.seeds(done['task_id'], done['revision']), seeds)
        value = restarted.load(done['task_id'])
        self.assertNotIn('project_id', value)
        self.assertNotIn('source_id', value)
        self.assertEqual(restarted.get(done['task_id'])['status'], 'done')

    def test_preview_stale_and_creation_replay_after_dictionary_change(self):
        d = self.dictionary()
        preview = self.request('POST', '/coverage/preview', self.sampling(d))
        body = {'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}
        key = str(uuid4())
        accepted = self.request('POST', '/tasks', body, key=key, status=202)
        done = self.wait(accepted['task_id'])
        self.request('PUT', '/dictionaries/' + d['dictionary_id'], {**feature_body(), 'name': '新版'}, version=d['version'])
        self.assertEqual(self.request('POST', '/tasks', body, key=key, status=202)['task_id'], done['task_id'])
        self.request('POST', '/tasks', {**body, 'task_id': str(uuid4())}, status=409)
        self.request('POST', '/tasks', body, status=409)
        self.assertEqual(len(self.service.files.ids('prompt_tasks')), 1)

    def test_pause_resume_cancel_discards_late_results(self):
        d = self.dictionary()
        preview = self.service.preview(self.sampling(d))
        entered, release = threading.Event(), threading.Event()
        original = sample
        def held(snapshot, **kwargs):
            entered.set()
            release.wait(5)
            return original(snapshot, **kwargs)
        body = {'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}
        with patch('stylelora.prompt_production.tasks.sample', side_effect=held):
            try:
                task = self.request('POST', '/tasks', body, status=202)
                self.assertTrue(entered.wait(2))
                task = self.request('POST', '/tasks/' + task['task_id'] + '/control', {'action': 'pause', 'expected_revision': task['revision']})
                self.assertEqual(task['status'], 'paused')
                self.request('POST', '/tasks/' + task['task_id'] + '/seeds', {'expected_revision': task['revision']}, status=409)
                task = self.request('POST', '/tasks/' + task['task_id'] + '/control', {'action': 'resume', 'expected_revision': task['revision']})
                task = self.request('POST', '/tasks/' + task['task_id'] + '/control', {'action': 'cancel', 'expected_revision': task['revision']})
            finally:
                release.set()
        self.runtime.supervisor.pool.shutdown(wait=True)
        self.assertEqual(self.request('GET', '/tasks/' + task['task_id'])['status'], 'canceled')
        self.assertIsNone(self.service.load(task['task_id'])['result'])

    def test_restart_pauses_interrupted_and_resume_uses_frozen_dictionary(self):
        d = self.dictionary()
        task, body = self.start(self.sampling(d))
        done = self.wait(task['task_id'])
        old = self.service.load(done['task_id'])
        expected = old['result']['items']
        old.update(status='running', ready=False, result=None)
        self.service.files.put('prompt_tasks', done['task_id'], old)
        self.runtime.prompts = restored = PromptProduction(self.runtime, self.root/'product')
        self.assertEqual(restored.get(done['task_id'])['status'], 'paused')
        current = restored.get(done['task_id'])
        self.request('PUT', '/dictionaries/' + d['dictionary_id'], {**feature_body(), 'name': '不同版本'}, version=d['version'])
        restored.control(done['task_id'], {'action': 'resume', 'expected_revision': current['revision']})
        deadline = time.monotonic() + 5
        while restored.get(done['task_id'])['status'] != 'done' and time.monotonic() < deadline:
            time.sleep(.01)
        current = restored.get(done['task_id'])
        self.assertEqual(restored.seeds(done['task_id'], current['revision'])['items'], expected)

    def test_pagination_is_stable_and_stale_cursor_rejected(self):
        self.dictionary()
        self.dictionary()
        first = self.request('GET', '/dictionaries?limit=1')
        self.assertTrue(first['has_more'])
        second = self.request('GET', '/dictionaries?limit=1&cursor=' + first['next_cursor'])
        self.assertNotEqual(first['items'][0]['dictionary_id'], second['items'][0]['dictionary_id'])
        self.dictionary()
        self.request('GET', '/dictionaries?limit=1&cursor=' + first['next_cursor'], status=409)

    def test_incomplete_task_never_exports_seeds_and_corruption_never_resamples(self):
        d = self.dictionary({'kind': 'prompts', 'name': '不足', 'entries': [entry(['forest'])]})
        task, _ = self.start(self.sampling(d, coverage_enabled=False, plan={'target_count': 2, 'random_seed': 7}))
        done = self.wait(task['task_id'])
        self.assertFalse(done['ready'])
        self.request('POST', '/tasks/' + done['task_id'] + '/seeds', {'expected_revision': done['revision']}, status=409)
        path = self.service.files.path('prompt_tasks', task['task_id'])
        path.write_text('{"damaged":true}', encoding='utf-8')
        restored = PromptProduction(self.runtime, self.root/'product')
        with self.assertRaises(ServiceError) as cm:
            restored.get(task['task_id'])
        self.assertEqual(cm.exception.code, 'prompt:corrupt_snapshot')
        self.assertEqual(restored.list()[0]['reason'], 'prompt:corrupt_snapshot')
        self.assertEqual(path.read_text(encoding='utf-8'), '{"damaged":true}')

    def test_auth_origin_and_reserved_capabilities(self):
        self.assertEqual(self.client.get(self.p + '/dictionaries').status_code, 401)
        self.assertEqual(self.client.get(self.p + '/dictionaries', headers={**self.headers, 'Origin': 'https://example.invalid'}).status_code, 403)
        self.assertFalse(self.request('GET', '/launcher/capabilities')['available'])
        d = self.dictionary()
        self.request('POST', '/coverage/preview', self.sampling(d, live_features=True), status=503)

    def test_solver_matches_exhaustive_small_overlapping_constraint_cases(self):
        d = self.dictionary({'kind': 'prompts', 'name': '求解器', 'entries': [entry(['forest'])]})
        frozen = self.service.freeze(self.sampling(d, coverage_enabled=False, plan={'target_count': 4, 'random_seed': 12}))
        rng = random.Random(39)
        for _ in range(30):
            rows = [entry(['item ' + str(i)] + [tag for tag in ('a', 'b', 'c') if rng.choice([True, False])], rating='general') for i in range(9)]
            lower, upper = rng.randrange(5), rng.randrange(5)
            constraints = [{'dimension': 'test', 'tag': 'a', 'minimum_count': lower, 'maximum_count': 4, 'minimum_ratio': None, 'maximum_ratio': None},
                           {'dimension': 'test', 'tag': 'b', 'minimum_count': 0, 'maximum_count': upper, 'minimum_ratio': None, 'maximum_ratio': None}]
            frozen['dictionaries'][0]['entries'] = rows
            frozen['request']['plan']['constraints'] = constraints
            expected = any(sum('a' in row['tags'] for row in subset) >= lower and sum('b' in row['tags'] for row in subset) <= upper
                           for subset in itertools.combinations(rows, 4))
            result = sample(frozen)
            self.assertEqual(result['ready'], expected, (constraints, result['status']))
            self.assertNotEqual(result['status'], 'search_exhausted')

    def test_duplicate_dictionary_copies_do_not_change_sampling_weight(self):
        d = self.dictionary({'kind': 'prompts', 'name': '原始', 'entries': [entry(['item ' + str(i)], rating='general') for i in range(20)]})
        first = self.service.freeze(self.sampling(d, coverage_enabled=False, plan={'target_count': 10, 'random_seed': 3}))
        second = deepcopy(first)
        second['dictionaries'].append({**deepcopy(second['dictionaries'][0]), 'dictionary_id': str(uuid4())})
        self.assertEqual(sample(first), sample(second))

    def test_atomic_write_error_after_replace_reconciles_without_duplicate(self):
        from stylelora.project_db.storage import atomic_json
        identifier, key = str(uuid4()), str(uuid4())
        body = {'dictionary_id': identifier, **feature_body()}
        def uncertain(path, value):
            atomic_json(path, value)
            raise OSError('after replace')
        with patch('stylelora.prompt_production.storage.atomic_json', side_effect=uncertain):
            saved = self.request('POST', '/dictionaries', body, status=201, key=key)
        self.assertEqual(saved, self.request('POST', '/dictionaries', body, status=201, key=key))
        self.assertEqual(self.request('GET', '/dictionaries/' + identifier)['revision'], 1)

    def test_request_keys_are_unique_across_task_and_dictionary_creation_ids(self):
        key = str(uuid4())
        self.request('POST', '/dictionaries', {'dictionary_id': str(uuid4()), **feature_body()}, status=201, key=key)
        self.request('POST', '/dictionaries', {'dictionary_id': str(uuid4()), **feature_body()}, status=409, key=key)
        d = self.dictionary()
        preview = self.service.preview(self.sampling(d))
        body = {'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}
        self.request('POST', '/tasks', body, key=key, status=202)
        self.wait(body['task_id'])
        self.request('POST', '/tasks', {**body, 'task_id': str(uuid4())}, key=key, status=409)
        restored = PromptProduction(self.runtime, self.root/'product')
        with self.assertRaises(ServiceError):
            restored.create({**body, 'task_id': str(uuid4())}, key)

    def test_full_outcomes_pagination_and_projection_beyond_browser_page(self):
        d = self.dictionary({'kind': 'prompts', 'name': '205 条', 'entries': [entry(['forest', 'item ' + str(i)], rating='general') for i in range(205)]})
        task, _ = self.start(self.sampling(d, coverage_enabled=False, plan={'target_count': 205, 'random_seed': 42}))
        done = self.wait(task['task_id'])
        first = self.request('GET', '/tasks/' + task['task_id'] + '/outcomes')
        self.assertEqual(len(first['items']), 200)
        last = self.request('GET', '/tasks/' + task['task_id'] + '/outcomes?cursor=' + first['next_cursor'])
        self.assertEqual(len(last['items']), 5)
        self.assertEqual(len({row['index'] for row in first['items'] + last['items']}), 205)
        self.assertEqual(self.request('POST', '/tasks/' + task['task_id'] + '/seeds', {'expected_revision': done['revision']})['count'], 205)

    def test_runtime_change_keeps_results_readable_but_prevents_resampling(self):
        d = self.dictionary()
        task, _ = self.start(self.sampling(d))
        done = self.wait(task['task_id'])
        value = self.service.load(task['task_id'])
        value['snapshot']['python_version'] = [3, 11, 1]
        self.service.files.put('prompt_tasks', task['task_id'], value)
        restored = PromptProduction(self.runtime, self.root/'product')
        self.assertEqual(restored.seeds(task['task_id'], done['revision'])['count'], 12)
        value.update(status='running', ready=False, result=None)
        restored.files.put('prompt_tasks', task['task_id'], value)
        restored = PromptProduction(self.runtime, self.root/'product')
        current = restored.get(task['task_id'])
        self.assertEqual(current['status'], 'paused')
        with self.assertRaises(ServiceError) as cm:
            restored.control(task['task_id'], {'action': 'resume', 'expected_revision': current['revision']})
        self.assertEqual(cm.exception.code, 'prompt:runtime_mismatch')


if __name__ == '__main__':
    unittest.main()
