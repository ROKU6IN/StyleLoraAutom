from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4
import threading
import unittest
from unittest.mock import patch

from stylelora.prompt_production.tasks import PromptProduction
from stylelora.runtime.errors import ServiceError
from . import test_prompt_production as support
from . import test_prompt_composer as composer_support


class PromptReviewTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait
    inputs = composer_support.ComposerTests.inputs
    mock = composer_support.ComposerTests.mock

    def task(self, n=1, **changes):
        self.mock(lambda p: composer_support.composed([t for tags in p['features'].values() for t in tags],
                                                       natural_language='A dreamy forest surrounds the river.'))
        request = self.inputs(n)
        request['plan']['target_count'] = n
        request.update(changes)
        created, _ = self.start(request)
        task = self.wait(created['task_id'])
        self.assertTrue(self.runtime.supervisor.tasks[self.service.active[task['task_id']]].finished.wait(10))
        task = self.service.get(task['task_id'])
        rows = self.service.outcomes(task['task_id'])
        self.assertTrue(all(row['status'] == 'review_required' for row in rows), rows)
        self.assertTrue(task['can_review'])
        return task, rows

    def preview(self, task, row, **changes):
        entry = deepcopy((row.get('review_candidate') or row['input'])['entry'])
        entry.update({'natural_language': 'Trees stand beside a river.', **changes})
        return self.request('POST', '/tasks/' + task['task_id'] + '/reviews/preview',
                            {'expected_revision': task['revision'], 'index': row['index'], 'entry': entry})

    def commit(self, task, preview, **options):
        return self.request('POST', '/tasks/' + task['task_id'] + '/reviews',
            {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'acknowledge': True}, **options)

    def test_edit_recheck_select_export_and_append_without_model_calls(self):
        task, rows = self.task()
        old = self.service.load(task['task_id'])
        calls = len(self.calls)
        preview = self.preview(task, rows[0])
        self.assertTrue(preview['valid'])
        self.assertTrue(preview['ready'])
        self.assertTrue(preview['selected'])
        self.assertEqual(self.service.load(task['task_id']), old)
        result = self.commit(task, preview)
        current = self.service.get(task['task_id'])
        self.assertTrue(current['ready'])
        self.assertEqual(result['revision'], task['revision'] + 1)
        reviewed = self.service.outcomes(task['task_id'])[0]
        self.assertEqual(reviewed['reason'], 'manual_review')
        self.assertIn('dreamy', reviewed['review_candidate']['entry']['natural_language'])
        self.assertIn('dreamy', reviewed['reply']['natural_language'])
        self.assertEqual(reviewed['input'], rows[0]['input'])
        self.assertEqual(reviewed['review']['previous_reason'], 'review_required')
        self.assertEqual(reviewed['review']['source'], 'model_output')
        seeds = self.request('POST', '/tasks/' + task['task_id'] + '/seeds', {'expected_revision': current['revision']})
        self.assertEqual(seeds['count'], 1)
        self.assertNotIn('dreamy', seeds['items'][0]['prompt'])
        exported = self.request('POST', '/tasks/' + task['task_id'] + '/results-preview', {'expected_revision': current['revision']})
        self.assertEqual(exported['count'], 1)
        target = self.dictionary({'kind': 'prompts', 'name': 'reviewed', 'entries': []})
        append = self.request('POST', '/tasks/' + task['task_id'] + '/append-preview', {'target_id': target['dictionary_id'], 'indices': [rows[0]['index']]})
        self.request('POST', '/tasks/' + task['task_id'] + '/append', {k: append[k] for k in ('target_id', 'indices', 'expected_revision', 'target_version')})
        self.assertEqual(len(self.service.dictionaries.get(target['dictionary_id'])['entries']), 1)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.service.load(task['task_id'])['snapshot'], old['snapshot'])

    def test_invalid_content_contract_and_fence_cannot_be_approved(self):
        task, rows = self.task()
        cases = [({'natural_language': 'A dreamy forest.'}, 'review_required'),
                 ({'natural_language': 'A watercolor painting.'}, 'forbidden_content'),
                 ({'natural_language': None}, 'invalid_contract'),
                 ({'tags': ['forest', 'no humans']}, 'invalid_contract'),
                 ({'aspect': 'portrait'}, 'invalid_contract'), ({'rating': 'explicit'}, 'fenced')]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                preview = self.preview(task, rows[0], **changes)
                self.assertFalse(preview['valid'])
                self.assertIn(reason, preview['issues'])
                self.commit(task, preview, status=422)
        self.assertEqual(self.service.get(task['task_id'])['revision'], task['revision'])

    def test_rating_cannot_lower_frozen_evidence(self):
        task, rows = self.task()
        value = self.service.load(task['task_id'])
        value['execution']['outcomes'][0]['rating'] = 'sensitive'
        self.service.files.put('prompt_tasks', task['task_id'], value)
        preview = self.preview(task, rows[0], rating='general')
        self.assertIn('rating_below_evidence', preview['issues'])
        self.assertEqual(preview['rating_lower_bound'], 'sensitive')
        preview = self.preview(task, rows[0], rating='sensitive')
        self.assertTrue(preview['valid'])

    def test_acknowledgement_required_and_replay_survives_restart_and_preview_expiry(self):
        task, rows = self.task()
        preview = self.preview(task, rows[0])
        body = {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'acknowledge': True}
        self.request('POST', '/tasks/' + task['task_id'] + '/reviews', {**body, 'acknowledge': False}, status=409)
        key = str(uuid4())
        result = self.commit(task, preview, key=key)
        self.runtime.previews.values.clear()
        restarted = PromptProduction(self.runtime, self.service.files.directory)
        self.assertEqual(restarted.review_commit(task['task_id'], body, key), result)
        with self.assertRaises(ServiceError) as caught:
            restarted.review_commit(task['task_id'], {**body, 'digest': '0' * 64}, key)
        self.assertEqual(caught.exception.code, 'prompt:request_key_conflict')
        self.assertTrue(restarted.get(task['task_id'])['ready'])

    def test_stale_preview_and_cross_task_preview_are_rejected(self):
        task, rows = self.task(2)
        first, second = self.preview(task, rows[0]), self.preview(task, rows[1])
        other, _ = self.task()
        self.commit(other, first, status=409)
        self.commit(task, first)
        self.commit(task, second, status=409)
        new_task = self.service.get(task['task_id'])
        self.assertFalse(new_task['ready'])
        partial = self.request('POST', '/tasks/' + task['task_id'] + '/results-preview', {'expected_revision': new_task['revision']})
        self.assertEqual(partial['count'], 1)
        self.commit(new_task, self.preview(new_task, rows[1]))
        self.assertTrue(self.service.get(task['task_id'])['ready'])

    def test_duplicate_tags_rejected_even_when_description_differs(self):
        task, rows = self.task(2)
        self.commit(task, self.preview(task, rows[0]))
        current = self.service.get(task['task_id'])
        preview = self.preview(current, rows[1], tags=rows[0]['review_candidate']['entry']['tags'])
        self.assertIn('duplicate', preview['issues'])

    def test_coverage_cell_drift_rejected_and_valid_repair_restores_quotas(self):
        task, rows = self.task(1, coverage_enabled=True,
            plan={'target_count': 1, 'random_seed': 42, 'cell_values': {'scene': ['forest'], 'lighting': ['sunlight']}})
        tags = [t for t in rows[0]['review_candidate']['entry']['tags'] if t != 'forest'] + ['beach']
        preview = self.preview(task, rows[0], tags=tags)
        self.assertIn('coverage_drift', preview['issues'])
        good = self.preview(task, rows[0])
        self.assertTrue(good['ready'])
        self.assertEqual(sum(good['coverage']['cell_counts'].values()), 1)

    def test_disk_failure_preserves_review_and_same_request_can_retry(self):
        task, rows = self.task()
        preview, key = self.preview(task, rows[0]), str(uuid4())
        before = self.service.load(task['task_id'])
        with patch('stylelora.prompt_production.storage.atomic_json', side_effect=OSError('disk full')):
            self.commit(task, preview, key=key, status=503)
        self.assertEqual(self.service.load(task['task_id']), before)
        self.commit(task, preview, key=key)
        self.assertTrue(self.service.get(task['task_id'])['ready'])

    def test_unknown_reply_after_commit_is_reconciled_from_durable_receipt(self):
        task, rows = self.task()
        preview, key = self.preview(task, rows[0]), str(uuid4())
        body = {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'acknowledge': True}
        with patch.object(self.service, 'emit', side_effect=ServiceError(503, 'test:lost_reply')):
            with self.assertRaises(ServiceError):
                self.service.review_commit(task['task_id'], body, key)
        self.assertTrue(self.service.review_commit(task['task_id'], body, key)['ready'])
        self.assertEqual(self.service.get(task['task_id'])['revision'], task['revision'] + 1)

    def test_concurrent_commits_only_one_revision_wins(self):
        task, rows = self.task(2)
        previews = [self.preview(task, row) for row in rows]
        barrier = threading.Barrier(2)
        def commit(preview):
            barrier.wait()
            try:
                return self.service.review_commit(task['task_id'], {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'acknowledge': True}, str(uuid4()))
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, previews))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn('prompt:revision_conflict', results)

    def test_running_pending_commits_and_changed_runtime_block_review(self):
        task, rows = self.task()
        self.service.pending_commits[task['task_id']] = self.service.load(task['task_id'])
        body = {'expected_revision': task['revision'], 'index': rows[0]['index'], 'entry': rows[0]['review_candidate']['entry']}
        self.request('POST', '/tasks/' + task['task_id'] + '/reviews/preview', body, status=409)
        self.service.pending_commits.clear()
        value = self.service.load(task['task_id'])
        for state in ('running', 'paused', 'canceled'):
            value.update(status=state, ready=False)
            self.service.files.put('prompt_tasks', task['task_id'], value)
            self.request('POST', '/tasks/' + task['task_id'] + '/reviews/preview', body, status=409)
        value.update(status='done')
        value['snapshot']['python_version'] = [0, 0, 0]
        self.service.files.put('prompt_tasks', task['task_id'], value)
        response = self.request('POST', '/tasks/' + task['task_id'] + '/reviews/preview', body, status=409)
        self.assertEqual(response['error']['code'], 'prompt:runtime_mismatch')

    def test_legacy_review_uses_input_and_does_not_invent_lost_model_reply(self):
        task, rows = self.task()
        value = self.service.load(task['task_id'])
        row = value['execution']['outcomes'][0]
        row.pop('review_candidate'); row.pop('reply')
        self.service.files.put('prompt_tasks', task['task_id'], value)
        entry = deepcopy(rows[0]['review_candidate']['entry'])
        entry['natural_language'] = 'Trees stand beside a river.'
        preview = self.request('POST', '/tasks/' + task['task_id'] + '/reviews/preview', {'expected_revision': task['revision'], 'index': row['index'], 'entry': entry})
        self.assertEqual(preview['source'], 'original_input')
        self.commit(task, preview)
        self.assertEqual(self.service.outcomes(task['task_id'])[0]['review']['source'], 'original_input')

    def test_frozen_policy_and_options_used_after_current_settings_change(self):
        task, rows = self.task()
        before = self.service.load(task['task_id'])['snapshot']
        with patch.object(self.runtime.catalog, 'snapshot', side_effect=AssertionError('must use frozen catalog')), \
             patch.object(self.runtime.llm, 'complete_json', side_effect=AssertionError('no model calls')):
            self.commit(task, self.preview(task, rows[0]))
        self.assertEqual(self.service.load(task['task_id'])['snapshot'], before)

    def test_old_exports_invalidated_cleanup_still_works_and_receipt_corruption_is_rejected(self):
        task, rows = self.task()
        exported = self.request('POST', '/tasks/' + task['task_id'] + '/results-preview', {'expected_revision': task['revision']})
        self.commit(task, self.preview(task, rows[0]))
        self.request('POST', '/tasks/' + task['task_id'] + '/results-export', {'preview_id': exported['preview_id'], 'digest': exported['digest']}, status=409)
        value = self.service.load(task['task_id'])
        bad = deepcopy(value)
        bad['execution']['outcomes'][0]['review']['entry_digest'] = '0' * 64
        self.service.files.put('prompt_tasks', task['task_id'], bad)
        self.request('GET', '/tasks/' + task['task_id'], status=503)
        self.service.files.put('prompt_tasks', task['task_id'], value)
        self.request('POST', '/task-cleanup', {'tasks': [{'task_id': task['task_id'], 'expected_revision': value['revision']}]})
        self.request('GET', '/tasks/' + task['task_id'], status=410)

    def test_details_are_read_only_and_strict_manual_contract(self):
        task, rows = self.task()
        before = self.service.load(task['task_id'])
        detail = self.request('GET', '/tasks/' + task['task_id'] + '/reviews/' + str(rows[0]['index']))
        self.assertIn('dreamy', detail['model_entry']['natural_language'])
        self.assertEqual(before, self.service.load(task['task_id']))
        body = {'expected_revision': task['revision'], 'index': rows[0]['index'], 'entry': rows[0]['review_candidate']['entry']}
        for bad in ({**body, 'index': True}, {**body, 'index': -1}, {**body, 'entry': {**body['entry'], 'rating': None}},
                    {**body, 'entry': {**body['entry'], 'aspect': None}}, {**body, 'override': True}):
            self.request('POST', '/tasks/' + task['task_id'] + '/reviews/preview', bad, status=422)
        self.commit(task, self.preview(task, rows[0]))
        detail = self.request('GET', '/tasks/' + task['task_id'] + '/reviews/' + str(rows[0]['index']))
        self.assertFalse(detail['can_review'])
        self.assertEqual(detail['review']['source'], 'model_output')
        self.assertNotIn('request_digest', detail['review'])

    def test_new_keyword_rating_and_new_character_cannot_bypass_checks(self):
        task, rows = self.task()
        value = self.service.load(task['task_id'])
        value['snapshot']['policy']['keywords'].append({'tag': 'restricted scene', 'rating': 'explicit'})
        value['snapshot']['catalog']['categories']['new character'] = 'character'
        self.service.files.put('prompt_tasks', task['task_id'], value)
        tags = rows[0]['review_candidate']['entry']['tags']
        preview = self.preview(task, rows[0], tags=tags + ['restricted scene'])
        self.assertIn('rating_below_evidence', preview['issues'])
        self.assertEqual(preview['rating_lower_bound'], 'explicit')
        preview = self.preview(task, rows[0], tags=tags + ['new character'])
        self.assertIn('invalid_contract', preview['issues'])

    def test_out_of_frame_content_stays_excluded_even_in_description(self):
        self.mock(lambda p: composer_support.composed([t for tags in p['features'].values() for t in tags if t != 'tree 0'],
            natural_language='A dreamy forest surrounds the river.', out_of_frame=['tree_0']))
        created, _ = self.start(self.inputs())
        task = self.wait(created['task_id'])
        self.assertTrue(self.runtime.supervisor.tasks[self.service.active[task['task_id']]].finished.wait(10))
        rows = self.service.outcomes(task['task_id'])
        self.assertEqual(rows[0]['status'], 'review_required')
        good = self.preview(task, rows[0])
        self.assertTrue(good['valid'])
        bad = self.preview(task, rows[0], natural_language='The tree 0 stands beside a river.')
        self.assertIn('invalid_contract', bad['issues'])
        bad = self.preview(task, rows[0], tags=rows[0]['review_candidate']['entry']['tags'] + ['tree 0'])
        self.assertIn('invalid_contract', bad['issues'])

    def test_manual_review_never_automatically_appends_to_configured_dictionary(self):
        target = self.dictionary({'kind': 'prompts', 'name': 'automatic target', 'entries': []})
        task, rows = self.task(append_target_id=target['dictionary_id'])
        with patch.object(self.service, '_automatic_append', side_effect=AssertionError('manual commit must not append')):
            self.commit(task, self.preview(task, rows[0]))
        self.assertEqual(self.service.dictionaries.get(target['dictionary_id'])['entries'], [])
