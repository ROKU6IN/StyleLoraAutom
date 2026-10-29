from copy import deepcopy
import csv
import io
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from stylelora.api.app import create_app
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.lifecycle import Runtime
from . import test_prompt_production as support
from . import test_prompt_composer as composer_support


class ManagementTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait

    def settled(self, identifier):
        operation = self.service.active.get(identifier)
        if operation:
            self.assertTrue(self.runtime.supervisor.tasks[operation].finished.wait(10))
        return self.request('GET', '/tasks/' + identifier)

    def task(self, n=3):
        d = self.dictionary({'kind': 'prompts', 'name': 'cleanup fixture', 'entries': [
            support.entry(['no humans', 'forest', 'tree ' + str(i)], rating='general', aspect='landscape') for i in range(n)]})
        request = self.sampling(d, coverage_enabled=False, plan={'target_count': n, 'random_seed': 42})
        preview = self.request('POST', '/coverage/preview', request)
        body = {'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}
        key = str(uuid4())
        self.request('POST', '/tasks', body, status=202, key=key)
        self.wait(body['task_id'])
        return self.settled(body['task_id']), body, key

    @staticmethod
    def selection(*tasks):
        return {'tasks': [{'task_id': t['task_id'], 'expected_revision': t['revision']} for t in tasks]}

    def restart(self):
        self.client.__exit__(None, None, None)
        self.runtime = Runtime(product_dir=self.root/'tagging', llm_dir=self.root/'product')
        self.service = self.runtime.prompts
        self.client = TestClient(create_app(self.runtime), base_url=self.runtime.origin)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer ' + self.runtime.token, 'Origin': self.runtime.origin}

    def test_cleanup_storage_bytes_and_old_create_cannot_resurrect_after_restart(self):
        task, create, key = self.task()
        before = self.request('GET', '/task-storage')
        path = self.service.files.path('prompt_tasks', task['task_id'])
        self.assertEqual(before['storage_bytes'], path.stat().st_size)
        self.assertTrue(before['items'][0]['can_cleanup'])
        cleanup_key = str(uuid4())
        result = self.request('POST', '/task-cleanup', self.selection(task), key=cleanup_key)
        self.assertEqual(result['count'], 1)
        self.assertFalse(path.exists())
        after = self.request('GET', '/task-storage')
        self.assertEqual(after['storage_bytes'], 0)
        self.assertEqual(after['receipt_count'], 1)
        self.assertLess(after['receipt_bytes'], before['storage_bytes'])
        marker = self.service.tombstone(task['task_id'])
        for forbidden in ('snapshot', 'result', 'model', 'entries', 'request', 'project_id', 'source_id'):
            self.assertNotIn(forbidden, marker)
        self.assertEqual(self.request('GET', '/tasks')['items'], [])
        self.restart()
        with patch.object(self.service, '_submit', side_effect=AssertionError('No resurrection')):
            self.request('GET', '/tasks/' + task['task_id'], status=410)
            self.request('POST', '/tasks', create, key=key, status=410)
            self.request('POST', '/tasks', {**create, 'task_id': str(uuid4())}, key=key, status=409)
            replay = self.request('POST', '/task-cleanup', self.selection(task), key=cleanup_key)
        self.assertEqual(result, replay)

    def test_batch_preflight_revision_conflict_changes_nothing_and_duplicate_ids_rejected(self):
        first, _, _ = self.task()
        second, _, _ = self.task()
        body = self.selection(first, second)
        body['tasks'][1]['expected_revision'] -= 1
        self.request('POST', '/task-cleanup', body, status=409)
        self.assertEqual(self.request('GET', '/task-storage')['receipt_count'], 0)
        self.assertEqual(self.request('GET', '/tasks')['total'], 2)
        self.request('POST', '/task-cleanup', self.selection(first, first), status=422)

    def test_running_and_canceled_inflight_tasks_are_not_cleaned(self):
        d = self.dictionary()
        preview = self.request('POST', '/coverage/preview', self.sampling(d))
        body = {'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}
        entered, release = threading.Event(), threading.Event()
        from stylelora.prompt_production.tasks import sample
        def blocked(*args, **kw):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timeout')
            return sample(*args, **kw)
        with patch('stylelora.prompt_production.tasks.sample', side_effect=blocked):
            self.request('POST', '/tasks', body, status=202)
            self.assertTrue(entered.wait(5))
            try:
                running = self.request('GET', '/tasks/' + body['task_id'])
                self.request('POST', '/task-cleanup', self.selection(running), status=409)
                canceled = self.request('POST', '/tasks/' + body['task_id'] + '/control', {'action': 'cancel', 'expected_revision': running['revision']})
                self.request('POST', '/task-cleanup', self.selection(canceled), status=409)
            finally:
                release.set()
            canceled = self.settled(body['task_id'])
        self.request('POST', '/task-cleanup', self.selection(canceled))

    def test_unpersisted_known_reply_blocks_cleanup_and_export(self):
        task, _, _ = self.task()
        self.service.pending_commits[task['task_id']] = self.service.load(task['task_id'])
        self.request('POST', '/task-cleanup', self.selection(task), status=409)
        self.request('POST', '/tasks/' + task['task_id'] + '/results-preview', {'expected_revision': task['revision'], 'format': 'json'}, status=409)
        self.assertEqual(self.request('GET', '/task-storage')['items'][0]['cleanup_reason'], 'prompt:pending_result')
        self.service.pending_commits.clear()

    def test_crash_between_marker_and_unlink_retains_evidence_and_can_finish_after_restart(self):
        first, _, _ = self.task()
        second, _, _ = self.task()
        body, key = self.selection(first, second), str(uuid4())
        with patch.object(self.service.files, 'remove_checked', side_effect=ServiceError(503, 'fixture')):
            self.request('POST', '/task-cleanup', body, key=key, status=503)
        self.assertEqual(self.request('GET', '/task-storage')['receipt_count'], 1)
        self.restart()
        inventory = self.request('GET', '/task-storage')
        self.assertEqual(sum(row['status'] == 'cleanup_pending' for row in inventory['items']), 1)
        self.request('POST', '/task-cleanup', body, key=key)
        self.assertEqual(self.request('GET', '/task-storage')['total'], 0)
        self.request('POST', '/task-cleanup', self.selection(first), key=key, status=409)

    def test_cleanup_never_unlinks_changed_or_corrupt_snapshot(self):
        task, _, _ = self.task()
        body = self.selection(task)
        with patch.object(self.service.files, 'remove_checked', side_effect=ServiceError(503, 'fixture')):
            self.request('POST', '/task-cleanup', body, status=503)
        raw = self.service.files.get('prompt_tasks', task['task_id'])
        raw['revision'] += 1
        self.service.files.put('prompt_tasks', task['task_id'], raw)
        self.request('POST', '/task-cleanup', body, status=409)
        self.assertTrue(self.service.files.path('prompt_tasks', task['task_id']).exists())
        self.request('POST', '/tasks/' + task['task_id'] + '/seeds', {'expected_revision': task['revision']}, status=410)

    def test_receipt_write_failure_preserves_task_and_atomic_replace_reply_loss_is_reconciled(self):
        task, _, _ = self.task()
        with patch('stylelora.prompt_production.storage.atomic_json', side_effect=OSError('disk')):
            self.request('POST', '/task-cleanup', self.selection(task), status=503)
        self.assertIsNone(self.service.tombstone(task['task_id']))
        self.assertTrue(self.request('GET', '/tasks/' + task['task_id'])['ready'])
        from stylelora.project_db.storage import atomic_json
        def written(*args):
            atomic_json(*args)
            raise OSError('lost durable reply')
        with patch('stylelora.prompt_production.storage.atomic_json', side_effect=written):
            self.request('POST', '/task-cleanup', self.selection(task))
        self.assertEqual(self.request('GET', '/task-storage')['total'], 0)

    def test_partial_refined_export_preserves_structure_and_never_makes_seeds_ready(self):
        def response(payload):
            tags = [t for values in payload['features'].values() for t in values]
            if 'tree 0' in tags:
                return {'status': 'invalid_input', 'tags': [], 'natural_language': None, 'aspect': None,
                        'added': [], 'dropped': [], 'out_of_frame': [], 'reason': 'fixture'}
            return composer_support.composed(tags)
        composer_support.ComposerTests.mock(self, response)
        request = composer_support.ComposerTests.inputs(self, 4)
        request['plan']['target_count'] = 4
        task, _ = self.start(request)
        self.wait(task['task_id'])
        task = self.settled(task['task_id'])
        self.assertFalse(task['ready'])
        path = '/tasks/' + task['task_id']
        calls = len(self.calls)
        exported_rows = None
        for format in ('json', 'csv', 'txt'):
            preview = self.request('POST', path + '/results-preview', {'expected_revision': task['revision'], 'format': format})
            self.assertEqual(preview['count'], 3)
            self.assertFalse(preview['usable_for_start'])
            self.assertFalse(preview['source_ready'])
            body = {'preview_id': preview['preview_id'], 'digest': preview['digest']}
            if format == 'txt':
                self.request('POST', path + '/results-export', body, status=409)
                body['acknowledge_lossy'] = True
            exported = self.request('POST', path + '/results-export', body)
            self.assertEqual(preview['bytes'], len(exported['content'].encode('utf-8')))
            if format == 'json':
                document = json.loads(exported['content'])
                self.assertEqual(document['kind'], 'prompt_task_results')
                self.assertNotIn('items', document)
                exported_rows = document['rows']
                self.assertEqual(len(exported_rows), 3)
                self.assertTrue(all(row['entry']['aspect'] == 'landscape' for row in exported_rows))
            elif format == 'csv':
                rows = list(csv.DictReader(io.StringIO(exported['content'].lstrip('\ufeff'))))
                self.assertEqual([json.loads(r['tags']) for r in rows], [r['entry']['tags'] for r in exported_rows])
            else:
                self.assertEqual(len(exported['content'].splitlines()), 3)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.request('GET', path)['revision'], task['revision'])
        self.request('POST', path + '/seeds', {'expected_revision': task['revision']}, status=409)

    def test_full_export_is_not_browser_page_limited_and_preview_bound_to_task_revision(self):
        task, _, _ = self.task(205)
        path = '/tasks/' + task['task_id']
        preview = self.request('POST', path + '/results-preview', {'expected_revision': task['revision'], 'format': 'json'})
        self.assertEqual(preview['count'], 205)
        self.assertEqual(len(preview['examples']), 20)
        body = {'preview_id': preview['preview_id'], 'digest': preview['digest']}
        document = json.loads(self.request('POST', path + '/results-export', body)['content'])
        self.assertEqual(len(document['rows']), 205)
        self.request('POST', '/tasks/' + str(uuid4()) + '/results-export', body, status=409)
        value = self.service.load(task['task_id'])
        value['revision'] += 1
        self.service.files.put('prompt_tasks', task['task_id'], value)
        self.request('POST', path + '/results-export', body, status=409)
        self.request('POST', path + '/results-preview', {'expected_revision': task['revision'], 'format': 'json'}, status=409)

    def test_export_rechecks_content_fence_and_tag_set_dedup(self):
        task, _, _ = self.task()
        value = self.service.load(task['task_id'])
        original = deepcopy(value['result']['outcomes'][0])
        extra = []
        for index, status in ((20, 'failed'), (21, 'review_required'), (22, 'ok'), (23, 'ok'), (24, 'ok')):
            row = deepcopy(original)
            row.update(index=index, status=status)
            if index == 23:
                row['entry']['tags'] = ['no humans', 'watercolor']
            if index == 24:
                row.update(rating='explicit', verified=True)
            extra.append(row)
        value['result']['outcomes'].extend(extra)
        self.service.files.put('prompt_tasks', task['task_id'], value)
        preview = self.request('POST', '/tasks/' + task['task_id'] + '/results-preview', {'expected_revision': task['revision'], 'format': 'json'})
        self.assertEqual(preview['count'], 3)
        self.assertEqual(preview['excluded'], {'failed': 1, 'review_required': 1, 'duplicate_tags': 1, 'forbidden_content': 1, 'fenced': 1})

    def test_cleanup_invalidates_existing_export_preview_and_preserves_dictionary(self):
        task, _, _ = self.task()
        dictionaries = self.request('GET', '/dictionaries')
        path = '/tasks/' + task['task_id']
        preview = self.request('POST', path + '/results-preview', {'expected_revision': task['revision'], 'format': 'json'})
        self.request('POST', '/task-cleanup', self.selection(task))
        self.request('POST', path + '/results-export', {'preview_id': preview['preview_id'], 'digest': preview['digest']}, status=410)
        self.assertEqual(self.request('GET', '/dictionaries'), dictionaries)
        self.assertNotIn(preview['preview_id'], self.runtime.previews.values)

    def test_cleanup_frees_live_task_capacity_and_does_not_drop_replay_barrier(self):
        with patch.object(self.service.files, 'limit', side_effect=lambda group: 1 if group == 'prompt_tasks' else 2048):
            task, body, key = self.task()
            self.request('POST', '/task-cleanup', self.selection(task))
            next_task, _, _ = self.task()
            self.assertTrue(next_task['ready'])
            self.request('POST', '/tasks', body, key=key, status=410)

    def test_corrupt_task_is_reported_and_never_silently_cleaned(self):
        task, _, _ = self.task()
        path = self.service.files.path('prompt_tasks', task['task_id'])
        path.write_text('{broken', encoding='utf-8')
        inventory = self.request('GET', '/task-storage')
        self.assertEqual(inventory['items'][0]['status'], 'unavailable')
        self.assertFalse(inventory['items'][0]['can_cleanup'])
        self.request('POST', '/task-cleanup', self.selection(task), status=503)
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')
        self.assertEqual(self.request('GET', '/task-storage')['receipt_count'], 0)
