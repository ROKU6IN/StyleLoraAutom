"""Source-bound diagnostics using source-derived schemas and synthetic values."""
import asyncio
from copy import deepcopy
import json
import threading
from types import SimpleNamespace
import unittest
from uuid import uuid4

from stylelora.backends.nai_launcher.readonly import settings_view, status_view
from stylelora.generation.launcher_check import compare_source, HIGH_MODEL, MEDIUM_MODEL
from .api_support import APITest
from .test_generation_handoff import draft
from .test_launcher_readonly import SourceFixture, TOKEN


def fixture():
    mock = SourceFixture()
    mock.settings.update(draft()['config']['profile'])
    mock.settings.update(uc_preset=3, available_models=[{'id': HIGH_MODEL, 'efforts': ['medium', 'high']}])
    return mock


def inspection(mock):
    return {'status': 'ready', 'reason': None, 'checked_at': '2026-10-28T01:00:00Z', 'version': '4.3.1', 'warnings': [],
            'settings': settings_view(mock.settings), 'activity': status_view(mock.status)}


class LauncherComparisonTests(unittest.TestCase):
    def setUp(self):
        self.mock = fixture()
        body = draft()
        self.source = SimpleNamespace(config_json=body['config'], negative_prompt=body['negative_prompt'])

    def compare(self):
        return compare_source(self.source, inspection(self.mock))

    def test_matching_fields_do_not_prove_generation_or_mutate_source(self):
        before = deepcopy(self.source.config_json)
        result = self.compare()
        self.assertTrue(result['settings_match'])
        self.assertFalse(result['generation_enabled'])
        self.assertEqual(result['issues'], [])
        self.assertIn('inherited_references', result['unverified'])
        self.assertIn('batch_size', result['unverified'])
        self.assertIn('durable_attempt_outcome', result['unverified'])
        self.assertEqual(self.source.config_json, before)
        field = next(field for field in result['fields'] if field['key'] == 'uc_preset')
        self.assertIsNone(field['requested'])
        self.assertEqual(field['expected'], 3)

    def test_fixed_quality_and_none_are_not_silently_adopted_from_launcher(self):
        self.mock.settings.update(quality_toggle=True, uc_preset=0)
        result = self.compare()
        self.assertFalse(result['settings_match'])
        self.assertEqual({row['key'] for row in result['fields'] if not row['matches']}, {'quality_toggle', 'uc_preset'})
        self.assertIn('launcher_check:settings_mismatch', result['issues'])

    def test_medium_model_mapping_and_negative_lock(self):
        self.source.config_json['profile']['effort'] = 'medium'
        self.mock.settings.update(model=MEDIUM_MODEL, effort='medium', locked_by_effort=['steps', 'negative_prompt'])
        result = self.compare()
        self.assertTrue(result['settings_match'])
        self.assertIn('launcher_check:negative_locked', result['issues'])
        self.assertTrue(next(row for row in result['fields'] if row['key'] == 'steps')['locked'])
        self.assertNotIn(self.source.negative_prompt, json.dumps(result))

    def test_medium_identifier_with_high_effort_maps_to_high(self):
        self.source.config_json['profile']['model'] = MEDIUM_MODEL
        self.assertTrue(self.compare()['settings_match'])

    def test_blank_negative_does_not_claim_conflict(self):
        self.source.negative_prompt = '  '
        self.mock.settings['locked_by_effort'] = ['negative_prompt']
        self.assertNotIn('launcher_check:negative_locked', self.compare()['issues'])

    def test_effort_not_supported_or_model_unlisted_is_reported(self):
        for model, efforts, code in ((HIGH_MODEL, [], 'launcher_check:effort_unsupported'),
                                     ('other', ['high'], 'launcher_check:model_unlisted')):
            with self.subTest(code=code):
                self.mock.settings['available_models'] = [{'id': model, 'efforts': efforts}]
                result = self.compare()
                self.assertIn(code, result['issues'])
                self.assertFalse(result['generation_enabled'])

    def test_page_geometry_is_not_compared_as_request_geometry(self):
        self.mock.settings.update(width=1024, height=1024)
        result = self.compare()
        self.assertTrue(result['settings_match'])
        self.assertEqual(result['request_sizes']['portrait'], {'width': 512, 'height': 768})
        self.assertIn('resolution_support', result['unverified'])

    def test_activity_never_means_attempt_settlement(self):
        self.mock.status['queue']['pending'] = 1
        self.assertIn('launcher_check:activity_present', self.compare()['issues'])
        self.mock.status['queue']['pending'] = 0
        self.mock.status['generation']['status'] = 'completed'
        result = self.compare()
        self.assertNotIn('launcher_check:activity_present', result['issues'])
        self.assertIn('durable_attempt_outcome', result['unverified'])


class LauncherCheckAPITests(APITest):
    def setUp(self):
        super().setUp()
        self.mock = fixture()
        self.runtime.backends.client_factory = self.mock.client
        self.source = self.request('POST', self.p+'/sources', draft(), status=201)
        self.source_path = self.p+f"/sources/{self.source['source_id']}"
        self.path = self.source_path+'/launcher-check'

    def start(self, **kwargs):
        return self.request('POST', self.path, {}, version=kwargs.pop('version', self.source['version']), status=202, **kwargs)

    def run_check(self, **kwargs):
        return self.wait_operation(self.start(**kwargs))['result']

    def edit(self):
        body = draft()
        body['config']['profile']['steps'] = 19
        self.source = self.request('PUT', self.source_path, body, version=self.source['version'])

    def slow(self):
        started, release = threading.Event(), threading.Event()
        async def hold(request, body):
            started.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            return await self.mock.reply(request, body)
        self.mock.tool_handler = hold
        self.addCleanup(release.set)
        return started, release

    def test_get_is_local_and_fresh_check_is_bound_and_no_source_mutation(self):
        self.assertIsNone(self.request('GET', self.path)['result'])
        self.assertEqual(self.mock.requests, [])
        before = self.request('GET', self.source_path)
        global_before = self.request('GET', '/api/backends')
        result = self.run_check()
        self.assertTrue(result['settings_match'])
        self.assertEqual(result['source_version'], before['version'])
        self.assertEqual(self.mock.calls(), ['get_generation_settings', 'get_generation_status'])
        self.assertEqual(self.request('GET', self.source_path), before)
        self.assertEqual(self.request('GET', '/api/backends'), global_before)
        self.assertFalse(self.request('GET', '/api/generation/capabilities')['generation'])
        self.assertEqual(self.request('GET', self.path)['result'], result)
        for hidden in (TOKEN, 'geometric style', 'unwanted objects', 'private_path', 'recent_images'):
            self.assertNotIn(hidden, json.dumps(result))

    def test_each_new_check_reads_fresh_values(self):
        self.assertTrue(self.run_check()['settings_match'])
        self.mock.settings['steps'] = 22
        self.assertFalse(self.run_check()['settings_match'])
        self.assertEqual(self.mock.initializations, 2)

    def test_same_key_replays_and_binds_original_source_version(self):
        key = str(uuid4())
        first = self.run_check(key=key)
        self.assertEqual(self.run_check(key=key), first)
        old = self.source['version']
        self.edit()
        self.assertEqual(self.run_check(version=old, key=key), first)
        failed = self.request('POST', self.path, {}, version=self.source['version'], key=key, status=409)
        self.assertEqual(failed['error']['code'], 'idempotency:key_conflict')
        self.assertEqual(len(self.mock.calls()), 2)

    def test_missing_or_stale_version_never_connects(self):
        self.request('POST', self.path, {}, status=428)
        self.request('POST', self.path, {}, version='stale', status=409)
        self.assertEqual(self.mock.requests, [])

    def test_strict_body_query_auth_and_project_scope(self):
        self.request('POST', self.path, {'source_id': 2}, version=self.source['version'], status=422)
        self.request('POST', self.path+'?endpoint=other', {}, version=self.source['version'], status=422)
        self.request('GET', self.path+'?token=secret', status=422)
        self.request('GET', self.path.replace(self.pid, str(uuid4())), status=410)
        self.request('GET', self.p+'/sources/99999/launcher-check', status=404)
        self.assertEqual(self.client.post(self.path, json={}, headers={'Authorization':'Bearer wrong'}).status_code, 401)
        self.assertEqual(self.mock.requests, [])

    def test_import_sources_are_rejected(self):
        self.import_inputs()
        imported = next(s for s in self.request('GET', self.p+'/sources')['items'] if s['type']=='import')
        path = self.p+f"/sources/{imported['source_id']}/launcher-check"
        self.request('GET', path, status=422)
        self.request('POST', path, {}, version=imported['version'], status=422)
        self.assertEqual(self.mock.requests, [])

    def test_changed_source_marks_old_result_stale(self):
        previous = self.run_check()
        self.edit()
        current = self.request('GET', self.path)
        self.assertTrue(current['stale'])
        self.assertEqual(current['result'], previous)
        self.assertEqual(len(self.mock.calls()), 2)

    def test_midflight_edit_discards_result_and_refuses_new_version_coalescing(self):
        started, release = self.slow()
        accepted = self.start()
        self.assertTrue(started.wait(2))
        self.edit()
        busy = self.request('POST', self.path, {}, version=self.source['version'], status=409)
        self.assertEqual(busy['error']['code'], 'launcher_check:busy')
        release.set()
        failed = self.wait_operation(accepted, expected='failed')
        self.assertEqual(failed['error']['code'], 'launcher_check:source_changed')
        self.assertIsNone(self.request('GET', self.path)['result'])

    def test_cancel_coalesces_and_preserves_previous_result(self):
        previous = self.run_check()
        started, _ = self.slow()
        accepted = self.start()
        self.assertTrue(started.wait(2))
        self.assertEqual(self.start()['status_url'], accepted['status_url'])
        current = self.request('GET', accepted['status_url'])
        self.request('POST', accepted['status_url']+'/cancel', {}, version=current['version'])
        self.wait_operation(accepted, expected='canceled')
        self.assertEqual(self.request('GET', self.path)['result'], previous)
        self.assertEqual(self.mock.deletes, 2)

    def test_unknown_version_replaces_old_result_with_unavailable(self):
        self.run_check()
        self.mock.version = '4.3.2'
        result = self.run_check()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['settings_match'])
        self.assertEqual(result['fields'], [])
        self.assertEqual(result['reason'], 'backend:readonly_version_unreviewed')
        self.assertEqual(len(self.mock.calls()), 2)

    def test_project_close_cancels_read_and_clears_cache(self):
        started, _ = self.slow()
        accepted = self.start()
        self.assertTrue(started.wait(2))
        project = self.request('GET', self.p)
        self.wait_operation(self.request('POST', self.p+'/close', {}, version=project['version'], status=202))
        self.assertFalse(self.runtime.launcher_checks.entries)
        self.assertEqual(self.runtime.supervisor.tasks[accepted['task']['id']].status, 'canceled')
        self.request('GET', accepted['status_url'], status=410)
        self.assertEqual(self.mock.deletes, 1)

    def test_queued_check_of_edited_source_never_connects(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = [threading.Event(), threading.Event()]
        def hold(index, task):
            started[index].set()
            release.wait(5)
        for index in range(2):
            self.runtime.supervisor.submit('fixture.hold', lambda task, i=index: hold(i, task))
        self.assertTrue(all(event.wait(2) for event in started))
        accepted = self.start()
        self.edit()
        release.set()
        self.wait_operation(accepted, expected='failed')
        self.assertEqual(self.mock.requests, [])
