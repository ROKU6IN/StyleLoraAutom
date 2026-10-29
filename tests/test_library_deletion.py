from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from stylelora.prompt_production.coverage import CoveragePlans
from stylelora.prompt_production.dictionaries import Dictionaries
from stylelora.prompt_production.sampling import sample
from stylelora.prompt_production.storage import ProductFiles
from stylelora.runtime.errors import ServiceError
from . import test_prompt_production as support
from . import test_prompt_composer as composer_support
from . import test_prompt_management as management_support


class LibraryDeletionTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait
    restart = management_support.ManagementTests.restart
    settled = management_support.ManagementTests.settled
    mock = composer_support.ComposerTests.mock
    inputs = composer_support.ComposerTests.inputs

    def resource(self, kind):
        identifier, key = str(uuid4()), str(uuid4())
        if kind == 'coverage':
            owner, prefix, field = self.service.coverage_plans, '/coverage-plans', 'coverage_id'
            body = {'format': 'json', 'content': json.dumps({'name': '删除覆盖样例', 'plan': {'target_count': 12, 'random_seed': 42}})}
        else:
            owner, prefix, field = self.service.dictionaries, '/dictionaries', 'dictionary_id'
            body = support.feature_body() if kind == 'features' else {'kind': 'prompts', 'name': '删除提示词样例', 'entries': []}
        create = {field: identifier, **body}
        self.request('POST', prefix, create, status=201, key=key)
        value = self.request('GET', prefix + '/' + identifier)
        group = 'coverage' if kind == 'coverage' else 'dictionaries/' + kind
        return owner, prefix, identifier, body, create, key, value, group

    def test_delete_all_library_kinds_with_version_and_durable_replay(self):
        for kind in ('features', 'prompts', 'coverage'):
            owner, prefix, identifier, body, create, create_key, value, group = self.resource(kind)
            path, key = prefix + '/' + identifier, str(uuid4())
            self.request('DELETE', path, status=428)
            self.request('DELETE', path, version='0' * 64, status=409)
            result = self.request('DELETE', path, version=value['version'], key=key)
            self.assertTrue(result['deleted'])
            self.assertEqual(result['resource_id'], identifier)
            self.assertEqual(self.request('DELETE', path, version=value['version'], key=key), result)
            self.assertEqual(self.request('DELETE', path, version=value['version']), result)
            self.assertFalse(self.service.files.path(group, identifier).exists())
            self.request('GET', path, status=410)
            self.request('GET', path + '/export?format=json', status=410)
            self.request('PUT', path, body, version=value['version'], status=410)
            self.request('POST', prefix, create, status=410, key=create_key)
            self.request('POST', prefix, create, status=410)
            self.assertEqual(self.request('GET', prefix)['items'], [])
            self.assertEqual(self.request('GET', prefix + '/deletions')['items'], [])
            marker = owner.deletions.marker(identifier)
            self.assertNotIn('plan', marker)
            self.assertNotIn('entries', marker)
            self.assertNotIn('dimensions', marker)
        self.restart()
        self.request('POST', prefix, create, status=410, key=create_key)
        self.assertEqual(self.request('DELETE', path, version=value['version'], key=key), result)

    def test_old_request_keys_remain_reserved_after_deletion(self):
        for kind in ('features', 'coverage'):
            _, prefix, identifier, body, create, create_key, value, _ = self.resource(kind)
            path, deletion_key = prefix + '/' + identifier, str(uuid4())
            self.request('DELETE', path, version=value['version'], key=create_key, status=409)
            self.request('DELETE', path, version=value['version'], key=deletion_key)
            other_id = str(uuid4())
            other = {**create, ('coverage_id' if kind == 'coverage' else 'dictionary_id'): other_id}
            self.request('POST', prefix, other, key=create_key, status=409)
            self.request('POST', prefix, other, key=deletion_key, status=409)
            created = self.request('POST', prefix, other, status=201)
            self.request('DELETE', prefix + '/' + other_id, version=created['version'], key=deletion_key, status=409)
            self.request('DELETE', path, version='0' * 64, key=deletion_key, status=409)

    def test_failed_barrier_write_preserves_live_payload(self):
        for kind in ('prompts', 'coverage'):
            owner, prefix, identifier, _, _, _, value, group = self.resource(kind)
            with patch('stylelora.prompt_production.storage.atomic_json', side_effect=OSError('disk full')):
                self.request('DELETE', prefix + '/' + identifier, version=value['version'], status=503)
            self.assertIsNone(owner.deletions.marker(identifier))
            self.assertEqual(self.request('GET', prefix + '/' + identifier), value)
            self.assertTrue(self.service.files.path(group, identifier).exists())

    def test_interrupted_unlink_is_hidden_recoverable_and_blocks_resurrection_after_restart(self):
        for kind in ('features', 'coverage'):
            owner, prefix, identifier, body, create, create_key, value, group = self.resource(kind)
            path, key = prefix + '/' + identifier, str(uuid4())
            with patch.object(self.service.files, 'remove_checked', side_effect=ServiceError(503, 'prompt:cleanup_incomplete')):
                result = self.request('DELETE', path, version=value['version'], key=key, status=503)
            self.assertEqual(result['error']['code'], 'library:delete_incomplete')
            self.assertEqual(result['error']['outcome'], 'unknown')
            self.assertTrue(self.service.files.path(group, identifier).exists())
            self.assertEqual(self.request('GET', prefix)['items'], [])
            self.assertEqual(self.request('GET', prefix + '/deletions')['items'][0]['resource_id'], identifier)
            self.request('GET', path, status=410)
            self.request('PUT', path, body, version=value['version'], status=410)
            self.request('POST', prefix, create, key=create_key, status=410)
            self.restart()
            self.assertEqual(self.request('GET', prefix + '/deletions')['total'], 1)
            self.request('DELETE', path, version=value['version'], key=key)
            self.assertFalse(self.service.files.path(group, identifier).exists())
            self.assertEqual(self.request('GET', prefix + '/deletions')['total'], 0)

    def test_unknown_marker_commit_never_claims_rejection_and_can_be_reconciled(self):
        owner, prefix, identifier, _, _, _, value, group = self.resource('coverage')
        original = self.service.files.put
        def commit_then_lose_readback(selected_group, selected_id, payload):
            original(selected_group, selected_id, payload)
            if selected_group == owner.deletions.group:
                raise ServiceError(503, 'prompt:io_error')
        key = str(uuid4())
        with patch.object(self.service.files, 'put', side_effect=commit_then_lose_readback):
            result = self.request('DELETE', prefix + '/' + identifier, version=value['version'], key=key, status=503)
        self.assertEqual(result['error']['outcome'], 'unknown')
        self.assertEqual(result['error']['code'], 'library:delete_unknown')
        self.assertTrue(self.service.files.path(group, identifier).exists())
        self.request('GET', prefix + '/' + identifier, status=410)
        self.assertEqual(self.request('GET', prefix + '/deletions')['total'], 1)
        self.request('DELETE', prefix + '/' + identifier, version=value['version'], key=key)
        self.assertFalse(self.service.files.path(group, identifier).exists())

    def test_changed_remaining_payload_is_never_force_deleted(self):
        for kind in ('prompts', 'coverage'):
            owner, prefix, identifier, _, _, _, value, group = self.resource(kind)
            with patch.object(self.service.files, 'remove_checked', side_effect=ServiceError(503, 'prompt:cleanup_incomplete')):
                self.request('DELETE', prefix + '/' + identifier, version=value['version'], status=503)
            changed = self.service.files.get(group, identifier)
            changed['name'] = '外部修改'
            self.service.files.put(group, identifier, changed)
            error = self.request('DELETE', prefix + '/' + identifier, version=value['version'], status=409)
            self.assertEqual(error['error']['code'], 'library:delete_conflict')
            self.assertEqual(self.service.files.get(group, identifier), changed)
            self.assertTrue(owner.deletions.marker(identifier))

    def test_corrupt_marker_blocks_reads_recreation_and_removal(self):
        owner, prefix, identifier, _, create, _, value, group = self.resource('coverage')
        with patch.object(self.service.files, 'remove_checked', side_effect=ServiceError(503, 'prompt:cleanup_incomplete')):
            self.request('DELETE', prefix + '/' + identifier, version=value['version'], status=503)
        original = self.service.files.get(group, identifier)
        marker = owner.deletions.marker(identifier)
        for changed in ({**marker, 'group': 'prompt_tasks'}, {**marker, 'revision': '1'}, {**marker, 'request_digest': '0' * 64},
                        {**marker, 'resource_id': str(uuid4())}, {**marker, 'schema_version': True}):
            self.service.files.put(owner.deletions.group, identifier, changed)
            self.request('GET', prefix + '/' + identifier, status=503)
            self.request('POST', prefix, create, status=503)
            self.request('DELETE', prefix + '/' + identifier, version=value['version'], status=503)
            self.assertEqual(self.service.files.get(group, identifier), original)

    def test_corrupt_live_file_is_not_deleted_or_marked(self):
        owner, prefix, identifier, _, _, _, value, group = self.resource('features')
        changed = self.service.files.get(group, identifier)
        changed['revision'] = 'bad'
        self.service.files.put(group, identifier, changed)
        self.request('DELETE', prefix + '/' + identifier, version=value['version'], status=503)
        self.assertIsNone(owner.deletions.marker(identifier))
        self.assertEqual(self.service.files.get(group, identifier), changed)

    def test_concurrent_update_and_delete_has_one_winner(self):
        for kind in ('features', 'prompts', 'coverage'):
            owner, _, identifier, body, _, _, value, _ = self.resource(kind)
            barrier = threading.Barrier(2)
            def mutate(deleting):
                barrier.wait()
                try:
                    return (owner.deletions.delete(identifier, value['version'], str(uuid4())) if deleting else
                            owner.save(identifier, body, value['version'], str(uuid4())))
                except ServiceError as error:
                    return error.status
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(mutate, [False, True]))
            self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
            self.assertTrue(any(result in (409, 410) for result in results if isinstance(result, int)))

    def test_snapshot_finishes_and_exports_after_input_dictionary_and_plan_deletion(self):
        dictionary = self.dictionary()
        _, prefix, identifier, _, _, _, coverage, _ = self.resource('coverage')
        request = self.sampling(dictionary, plan=json.loads(coverage['template'])['plan'])
        preview = self.request('POST', '/coverage/preview', request)
        entered, release = threading.Event(), threading.Event()
        def blocked(snapshot, *args, **kw):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test did not release sampling')
            return sample(snapshot, *args, **kw)
        task_id = str(uuid4())
        try:
            with patch('stylelora.prompt_production.tasks.sample', side_effect=blocked):
                self.request('POST', '/tasks', {'task_id': task_id, 'request': preview['request'], 'preview_digest': preview['preview_digest']}, status=202)
                self.assertTrue(entered.wait(5))
                snapshot = deepcopy(self.service.load(task_id)['snapshot'])
                self.request('DELETE', '/dictionaries/' + dictionary['dictionary_id'], version=dictionary['version'])
                self.request('DELETE', prefix + '/' + identifier, version=coverage['version'])
                release.set()
                done = self.wait(task_id)
        finally:
            release.set()
        self.assertEqual(self.service.load(task_id)['snapshot'], snapshot)
        self.assertTrue(done['ready'])
        self.assertEqual(self.service.seeds(task_id, done['revision'])['count'], 12)
        self.request('POST', '/coverage/preview', request, status=410)
        self.request('POST', '/tasks', {'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}, status=410)

    def test_deleted_append_target_preserves_finished_results_and_reports_failure(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(payload):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test did not release model')
            return composer_support.composed([tag for tags in payload['features'].values() for tag in tags])
        self.mock(blocked)
        target = self.dictionary({'kind': 'prompts', 'name': '自动回写目标', 'entries': []})
        try:
            task, _ = self.start(self.inputs(append_target_id=target['dictionary_id']))
            self.assertTrue(entered.wait(5))
            self.request('DELETE', '/dictionaries/' + target['dictionary_id'], version=target['version'])
            release.set()
            self.wait(task['task_id'])
            done = self.settled(task['task_id'])
        finally:
            release.set()
        self.assertTrue(done['ready'])
        self.assertEqual(self.service.load(task['task_id'])['execution']['append_error'], 'dictionary:deleted')
        self.assertEqual(self.service.seeds(task['task_id'], done['revision'])['count'], 1)
        self.request('POST', '/tasks/' + task['task_id'] + '/append-preview', {'target_id': target['dictionary_id'], 'indices': [0]}, status=410)

    def test_deleted_dictionary_cannot_receive_old_import_commit(self):
        owner, prefix, identifier, body, _, _, value, _ = self.resource('prompts')
        owner.deletions.delete(identifier, value['version'], str(uuid4()))
        self.request('POST', prefix + '/' + identifier + '/imports', {'preview_id': str(uuid4()), 'digest': '0' * 64}, version=value['version'], status=410)
        with self.assertRaises(ServiceError) as error:
            owner.save(identifier, body, value['version'], str(uuid4()), append=True)
        self.assertEqual(error.exception.status, 410)

    def test_paging_and_pending_recovery_cursors_detect_changes(self):
        first = self.resource('coverage')
        second = self.resource('coverage')
        page = self.request('GET', '/coverage-plans?limit=1')
        self.request('DELETE', '/coverage-plans/' + second[2], version=second[6]['version'])
        self.request('GET', '/coverage-plans?limit=1&cursor=' + page['next_cursor'], status=409)
        third = self.resource('coverage')
        with patch.object(self.service.files, 'remove_checked', side_effect=ServiceError(503, 'prompt:cleanup_incomplete')):
            for item in (first, third):
                self.request('DELETE', '/coverage-plans/' + item[2], version=item[6]['version'], status=503)
        page = self.request('GET', '/coverage-plans/deletions?limit=1')
        self.assertEqual(page['total'], 2)
        self.request('DELETE', '/coverage-plans/' + first[2], version=first[6]['version'])
        self.request('GET', '/coverage-plans/deletions?limit=1&cursor=' + page['next_cursor'], status=409)

    def test_auth_query_body_and_draining_contracts(self):
        for kind in ('features', 'coverage'):
            _, prefix, identifier, _, _, _, value, group = self.resource(kind)
            path = prefix + '/' + identifier
            self.assertEqual(self.client.delete(self.p + path).status_code, 401)
            headers = {**self.headers, 'Origin': 'https://example.invalid'}
            self.assertEqual(self.client.delete(self.p + path, headers=headers).status_code, 403)
            self.request('DELETE', path + '?force=1', version=value['version'], status=422)
            self.request('DELETE', path, {'force': True}, version=value['version'], status=422)
            self.request('GET', prefix + '/deletions?unknown=1', status=422)
            self.assertEqual(self.client.delete(self.p + path, headers=self.headers).status_code, 422)
            self.runtime.draining = True
            try:
                self.request('DELETE', path, version=value['version'], status=503)
            finally:
                self.runtime.draining = False
            self.assertTrue(self.service.files.path(group, identifier).exists())

    def test_memory_storage_has_identical_deletion_barriers(self):
        for cls in (Dictionaries, CoveragePlans):
            owner = cls(self.runtime, ProductFiles())
            identifier = str(uuid4())
            body = (support.feature_body() if cls is Dictionaries else {'format': 'json', 'content': '{"name":"memory","plan":{}}'})
            created = owner.save(identifier, body, None, str(uuid4()), create=True)
            owner.deletions.delete(identifier, created['version'], str(uuid4()))
            self.assertEqual(owner.list(), [])
            self.assertEqual(owner.deletions.pending(), [])
            with self.assertRaises(ServiceError) as error:
                owner.save(identifier, body, None, str(uuid4()), create=True)
            self.assertEqual(error.exception.status, 410)
