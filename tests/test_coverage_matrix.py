import itertools
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from stylelora.prompt_production.matrix import CoverageViews, cell_entries
from stylelora.prompt_production.sampling import cell_id
from stylelora.runtime.errors import ServiceError
from . import test_prompt_production as support
from . import test_prompt_review as review_support


class MatrixTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait

    def inputs(self, n=205, enabled=True):
        d = self.dictionary({'kind': 'prompts', 'name': 'matrix', 'entries': [
            support.entry(['no humans', 'forest', 'sunlight', 'item ' + str(i)], rating='general') for i in range(n)]})
        request = self.sampling(d, coverage_enabled=enabled, plan={'primary_dimensions': ['scene', 'lighting'],
            'cell_values': {'scene': ['forest'], 'lighting': ['sunlight']}, 'target_count': n, 'random_seed': 9})
        return d, request

    def detail(self, view, cell, **params):
        return '/coverage/previews/' + view['preview_id'] + '/entries?' + urlencode({'digest': view['digest'], 'cell': cell, **params})

    def test_full_preview_pagination_is_frozen_read_only_and_not_truncated_to_examples(self):
        d, request = self.inputs()
        preview = self.request('POST', '/coverage/preview', request)
        self.assertEqual(len(preview['examples']), 100)
        self.assertTrue(preview['examples_truncated'])
        self.assertEqual(preview['matrix']['primary_dimensions'], ['scene', 'lighting'])
        cell = next(iter(preview['cell_targets']))
        view = preview['coverage_view']
        self.request('PUT', '/dictionaries/' + d['dictionary_id'], {'kind': 'prompts', 'name': 'new', 'entries': []}, version=d['version'])
        rows, cursor = [], None
        with patch.object(self.service, 'freeze', side_effect=AssertionError('must not resample')):
            while True:
                page = self.request('GET', self.detail(view, cell, limit=37, **({'cursor': cursor} if cursor else {})))
                self.assertEqual(page['total'], 205)
                rows += page['items']
                cursor = page['next_cursor']
                if not cursor:
                    break
        self.assertEqual(len(rows), 205)
        self.assertEqual([row['index'] for row in rows], list(range(205)))
        self.assertEqual(len({tuple(row['entry']['tags']) for row in rows}), 205)
        self.assertEqual(set(rows[0]), {'index', 'entry', 'rating', 'verified', 'cell'})
        self.assertEqual(self.service.files.ids('prompt_tasks'), [])
        self.assertIsNone(self.runtime.context)

    def test_three_dimensions_exclusions_and_deficits_are_not_invented_zero_targets(self):
        dims = {'scene': ['forest', 'beach'], 'lighting': ['sunlight', 'night'], 'view': ['wide shot', 'upper body']}
        d = self.dictionary({'kind': 'prompts', 'name': '3D', 'entries': [support.entry(['no humans', *values], rating='general')
            for values in itertools.product(*dims.values())]})
        request = self.sampling(d, plan={'primary_dimensions': list(dims), 'cell_values': dims,
            'target_count': 7, 'random_seed': 7, 'exclude': [{'all_tags': ['beach', 'night', 'upper body']}]})
        preview = self.request('POST', '/coverage/preview', request)
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['matrix']['cell_values'], dims)
        excluded = cell_id(list(dims), ['beach', 'night', 'upper body'])
        self.assertNotIn(excluded, preview['cell_targets'])
        self.request('GET', self.detail(preview['coverage_view'], excluded), status=404)
        request['plan']['target_count'] = 14
        missing = self.request('POST', '/coverage/preview', request)
        self.assertFalse(missing['ready'])
        cell = next(iter(missing['cell_targets']))
        self.assertEqual(missing['deficits'][cell], 2)
        self.assertEqual(self.request('GET', self.detail(missing['coverage_view'], cell))['total'], 0)

    def test_random_pool_has_a_single_explicit_pool_and_can_be_browsed(self):
        _, request = self.inputs(3, enabled=False)
        preview = self.request('POST', '/coverage/preview', request)
        self.assertEqual(preview['matrix'], {'enabled': False, 'primary_dimensions': [], 'cell_values': {}})
        self.assertEqual(self.request('GET', self.detail(preview['coverage_view'], 'random'))['total'], 3)

    def test_task_details_bind_revision_and_preserve_frozen_values_after_dictionary_edit(self):
        d, request = self.inputs(53)
        created, _ = self.start(request)
        task = self.wait(created['task_id'])
        cell = next(iter(task['coverage']['cell_targets']))
        path = '/tasks/' + task['task_id'] + '/coverage/entries?'
        params = {'cell': cell, 'expected_revision': task['revision'], 'limit': 50}
        first = self.request('GET', path + urlencode(params))
        self.assertEqual(first['total'], 53)
        self.request('PUT', '/dictionaries/' + d['dictionary_id'], {'kind': 'prompts', 'name': 'new', 'entries': []}, version=d['version'])
        self.assertEqual(len(self.request('GET', path + urlencode({**params, 'cursor': first['next_cursor']}))['items']), 3)
        stored = self.service.load(task['task_id'])
        stored['revision'] += 1
        self.service.files.put('prompt_tasks', task['task_id'], stored)
        self.request('GET', path + urlencode({**params, 'cursor': first['next_cursor']}), status=409)
        self.request('GET', path + urlencode({**params, 'expected_revision': stored['revision'], 'cursor': first['next_cursor']}), status=422)

    def test_cursor_and_digest_cannot_be_mixed_across_previews_or_cells(self):
        _, request = self.inputs(3)
        first = self.request('POST', '/coverage/preview', request)
        second = self.request('POST', '/coverage/preview', request)
        cell = next(iter(first['cell_targets']))
        page = self.request('GET', self.detail(first['coverage_view'], cell, limit=1))
        self.request('GET', self.detail(second['coverage_view'], cell, cursor=page['next_cursor']), status=422)
        self.request('GET', self.detail({**first['coverage_view'], 'digest': '0' * 64}, cell), status=409)
        with self.assertRaises(ServiceError):
            cell_entries(first['cell_targets'], [], 'random')

    def test_preview_expiration_release_and_three_view_retention(self):
        _, request = self.inputs(1)
        previews = [self.request('POST', '/coverage/preview', request) for _ in range(4)]
        cell = next(iter(previews[-1]['cell_targets']))
        self.assertEqual(len(self.service.coverage_views.values), 3)
        self.request('GET', self.detail(previews[0]['coverage_view'], cell), status=410)
        view = previews[1]['coverage_view']
        self.service.coverage_views.values[view['preview_id']]['deadline'] = time.monotonic() - 1
        self.request('GET', self.detail(view, cell), status=410)
        view = previews[-1]['coverage_view']
        url = '/coverage/previews/' + view['preview_id'] + '?digest='
        self.request('DELETE', url + '0' * 64, status=409)
        self.assertEqual(self.request('DELETE', url + view['digest']), {'released': True})
        self.assertEqual(self.request('DELETE', url + view['digest']), {'released': True})
        self.request('GET', self.detail(view, cell), status=410)
        self.assertTrue(previews[-1]['ready'])

    def test_byte_limit_preserves_preview_and_total_bytes_evict_oldest(self):
        _, request = self.inputs(2)
        with patch('stylelora.prompt_production.matrix.MAX_VIEW_BYTES', 1):
            preview = self.request('POST', '/coverage/preview', request)
        self.assertTrue(preview['ready'])
        self.assertIsNone(preview['coverage_view'])
        self.assertEqual(preview['coverage_view_reason'], 'coverage:detail_limit')
        preview = self.request('POST', '/coverage/preview', request)
        old = preview['coverage_view']['preview_id']
        size = self.service.coverage_views.values[old]['size']
        with patch('stylelora.prompt_production.matrix.MAX_VIEW_BYTES', size):
            self.request('POST', '/coverage/preview', request)
        self.assertNotIn(old, self.service.coverage_views.values)
        self.assertEqual(len(self.service.coverage_views.values), 1)

    def test_strict_detail_query_and_session_auth(self):
        _, request = self.inputs(1)
        preview = self.request('POST', '/coverage/preview', request)
        cell = next(iter(preview['cell_targets']))
        url = self.detail(preview['coverage_view'], cell)
        for suffix in ('&digest=' + 'a' * 64, '&cell=random', '&limit=0', '&limit=50&limit=20', '&unknown=yes'):
            self.request('GET', url + suffix, status=422)
        self.request('GET', self.detail(preview['coverage_view'], 'x' * 4097), status=422)
        self.assertEqual(self.client.get(self.p + url).status_code, 401)
        created, _ = self.start(request)
        task = self.wait(created['task_id'])
        for revision in ('0', '-1', '1.0', 'true', '1' * 30, '１２'):
            self.request('GET', '/tasks/' + task['task_id'] + '/coverage/entries?' + urlencode({'cell': cell, 'expected_revision': revision}), status=422)

    def test_unavailable_and_deleted_tasks_do_not_serve_detail(self):
        _, request = self.inputs(1)
        created, _ = self.start(request)
        task = self.wait(created['task_id'])
        self.runtime.supervisor.tasks[self.service.active[task['task_id']]].finished.wait(10)
        value = self.service.load(task['task_id'])
        path = '/tasks/' + task['task_id'] + '/coverage/entries?' + urlencode({'cell': next(iter(task['coverage']['cell_targets'])), 'expected_revision': task['revision']})
        value.update(status='paused', ready=False, result=None)
        self.service.files.put('prompt_tasks', task['task_id'], value)
        self.request('GET', path, status=409)
        self.request('POST', '/task-cleanup', {'tasks': [{'task_id': task['task_id'], 'expected_revision': task['revision']}]})
        self.request('GET', path, status=410)

    def test_view_copies_do_not_mutate_retained_data(self):
        report = {'cell_targets': {'random': 1}, 'outcomes': [{'index': 0, 'entry': support.entry(['forest']), 'cell': 'random', 'rating': None, 'verified': False}]}
        views = CoverageViews()
        view = views.add(report)
        report['outcomes'].clear()
        first = views.get(view['preview_id'], view['digest'], 'random')
        first[0]['entry']['tags'].append('beach')
        self.assertEqual(views.get(view['preview_id'], view['digest'], 'random')[0]['entry']['tags'], ['forest'])


class ReviewMatrixTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait
    inputs = review_support.PromptReviewTests.inputs
    mock = review_support.PromptReviewTests.mock
    task = review_support.PromptReviewTests.task
    preview = review_support.PromptReviewTests.preview
    commit = review_support.PromptReviewTests.commit

    def test_reviewed_results_replace_counts_and_details_without_serving_old_revision(self):
        task, rows = self.task()
        calls = len(self.calls)
        cell = next(iter(task['coverage']['cell_targets']))
        path = '/tasks/' + task['task_id'] + '/coverage/entries?'
        old_query = urlencode({'cell': cell, 'expected_revision': task['revision']})
        self.assertEqual(self.request('GET', path + old_query)['total'], 0)
        preview = self.preview(task, rows[0])
        self.assertIn('matrix', preview['coverage'])
        result = self.commit(task, preview)
        self.request('GET', path + old_query, status=409)
        current = self.request('GET', path + urlencode({'cell': cell, 'expected_revision': result['revision']}))
        self.assertEqual(current['total'], 1)
        self.assertEqual(current['items'][0]['entry']['natural_language'], 'Trees stand beside a river.')
        self.assertNotIn('dreamy', str(current))
        self.assertEqual(len(self.calls), calls)
