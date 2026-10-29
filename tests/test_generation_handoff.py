from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import time
from unittest.mock import patch
from uuid import uuid4

from stylelora.generation.profile import build_request, SIZES, validate_capabilities
from stylelora.runtime.errors import ServiceError
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import sources
from stylelora.project_db.types import ItemSeed
from stylelora.project_db.validation import digest, primitive
from stylelora.prompt_production.types import SamplingRequest, DictionaryInput
from stylelora.tagging.library import CatalogSnapshot
from .api_support import APITest


def draft(n=2):
    return {'name': 'Generation draft', 'target_count': n, 'cost_policy': 'ask', 'style_prompt': 'geometric style',
        'negative_prompt': 'unwanted objects', 'config': {'schema_version': 1, 'backend': 'nai_launcher',
        'default_aspect': 'portrait', 'sizes': deepcopy(SIZES['small']), 'profile': {
            'model': 'nai-diffusion-5-full', 'effort': 'high', 'sampler': 'fixture-sampler', 'steps': 17, 'scale': 4.5,
            'cfg_rescale': 0.0, 'noise_schedule': 'fixture-schedule', 'uc_preset': None, 'quality_toggle': False,
            'smea': False, 'smea_dyn': False, 'variety_plus': False, 'decrisp': False}}}


class FakeDriver:
    """Only tests inject this driver. It never emits an image or calls a tool."""
    def __init__(self):
        self.received, self.active, self.stops = [], False, []
        self.contract = 'a' * 64

    def capabilities(self):
        config = draft()['config']
        return {'backend': 'nai_launcher', 'contract_digest': self.contract,
                'config_schema': {'type': 'object', 'additionalProperties': False,
                    'required': list(config), 'properties': {k: {'const': v} for k, v in config.items()}},
                'negative_locked_by_effort': ['medium']}

    def is_active(self, context, identifier):
        return self.active

    def dispatch(self, context, identifier):
        with read_snapshot(context.handle) as db:
            source = sources.get_source(db, identifier)
            items = [dict(row) for row in db.execute('SELECT seq,prompt,aspect FROM source_items WHERE source_id=? ORDER BY seq', (identifier,))]
        assert source.locked and source.status == 'running'
        self.received.append((primitive(source), items))

    def stop(self, context, identifier):
        self.stops.append(identifier)


class GenerationTests(APITest):
    def setup_driver(self):
        self.driver = FakeDriver()
        self.runtime.generation_sources.driver = self.driver
        self.runtime.catalog.state['catalog'] = {'complete': True, 'tags': [{'tag': 'forest', 'aliases': [], 'category': 'general'}]}
        self.runtime.catalog._snapshot = CatalogSnapshot(self.runtime.catalog.state)

    def task(self, n=2):
        service = self.runtime.prompts
        identifier = str(uuid4())
        body = DictionaryInput.model_validate({'kind': 'prompts', 'name': 'source prompts', 'entries': [
            {'tags': ['no humans', 'forest', 'tree ' + str(i)], 'rating': 'general', 'aspect': 'landscape' if i % 2 else None} for i in range(n)]}).model_dump()
        service.dictionaries.save(identifier, body, None, str(uuid4()), create=True)
        request = SamplingRequest.model_validate({'prompt_dictionary_ids': [identifier], 'coverage_enabled': False,
            'plan': {'target_count': n, 'random_seed': 42}}).model_dump()
        preview = service.preview(request)
        task = service.create({'task_id': str(uuid4()), 'request': preview['request'], 'preview_digest': preview['preview_digest']}, str(uuid4()))
        for _ in range(400):
            current = service.get(task['task_id'])
            if current['status'] == 'done':
                return current
            time.sleep(.01)
        self.fail('Prompt task did not finish')

    def prepared(self, n=2):
        source = self.request('POST', self.p + '/sources', draft(n), status=201)
        task = self.task(n)
        preview = self.request('POST', self.p + f"/sources/{source['source_id']}/handoff-preview", {
            'task_id': task['task_id'], 'expected_task_revision': task['revision']}, version=source['version'])
        body = {k: preview[k] for k in ('preview_id', 'digest', 'source_digest', 'seeds_digest', 'count')}
        return source, task, preview, body

    def test_draft_schema_defaults_cas_and_session_replay(self):
        capabilities = self.request('GET', '/api/generation/capabilities')
        self.assertFalse(capabilities['generation'])
        self.assertEqual(capabilities['size_presets'], SIZES)
        key = str(uuid4())
        created = self.request('POST', self.p + '/sources', draft(), status=201, key=key)
        self.assertEqual(created, self.request('POST', self.p + '/sources', draft(), status=201, key=key))
        changed = draft()
        changed['style_prompt'] = 'different style'
        self.request('POST', self.p + '/sources', changed, status=409, key=key)
        path = self.p + '/sources/' + str(created['source_id'])
        self.request('PUT', path, changed, status=428)
        updated = self.request('PUT', path, changed, version=created['version'])
        self.assertNotEqual(updated['version'], created['version'])
        self.request('PUT', path, draft(), version=created['version'], status=409)
        for name, change in [('quality', lambda d: d['config']['profile'].update(quality_toggle=True)),
                             ('bool', lambda d: d['config'].update(schema_version=True)),
                             ('size', lambda d: d['config']['sizes']['portrait'].update(width=768)),
                             ('endpoint', lambda d: d['config'].update(endpoint='private')),
                             ('model-url', lambda d: d['config']['profile'].update(model='http://invalid/private'))]:
            with self.subTest(name=name):
                bad = draft()
                change(bad)
                self.request('POST', self.p + '/sources', bad, status=422)

    def test_unreviewed_backend_preview_is_read_only_and_start_does_not_lock(self):
        source, task, preview, body = self.prepared()
        self.assertFalse(preview['can_start'])
        self.assertIn('generation:adapter_unavailable', preview['blockers'])
        self.assertIn('generation:trigger_invalid', preview['blockers'])
        self.assertIsNone(preview['estimated_anlas'])
        path = self.p + f"/sources/{source['source_id']}"
        self.request('POST', path + '/start', body, version=source['version'], status=503)
        self.assertFalse(self.request('GET', path)['locked'])
        self.assertEqual(self.request('GET', path + '/items')['items'], [])
        self.assertEqual(self.runtime.prompts.get(task['task_id'])['revision'], task['revision'])

    def test_full_handoff_atomic_projection_request_pixels_and_no_provenance(self):
        self.setup_driver()
        source, task, preview, body = self.prepared(205)
        self.assertTrue(preview['can_start'])
        self.assertEqual(len(preview['examples']), 100)
        self.assertTrue(preview['examples_truncated'])
        path = self.p + f"/sources/{source['source_id']}"
        with patch.object(self.runtime.llm, 'complete_json', side_effect=AssertionError('No LLM at source start')):
            started = self.request('POST', path + '/start', body, version=source['version'])
        self.assertTrue(started['locked'])
        self.assertTrue(started['dispatched'])
        row, items = self.driver.received[0]
        self.assertEqual(len(items), 205)
        self.assertEqual([i['seq'] for i in items], list(range(1, 206)))
        self.assertTrue(all(set(i) == {'seq', 'prompt', 'aspect'} for i in items))
        self.assertEqual(row['config_json'], draft()['config'])
        for private in (task['task_id'], 'dictionary_id', 'random_seed', 'rating', 'endpoint', 'token'):
            self.assertNotIn(private, json.dumps(row))
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM images').fetchone()[0], 0)
            source_row = sources.get_source(db, source['source_id'])
        for item in items:
            output = build_request(source_row, ItemSeed(**item))
            aspect = item['aspect'] or 'portrait'
            self.assertEqual((output['width'], output['height']), (SIZES['small'][aspect]['width'], SIZES['small'][aspect]['height']))
            self.assertTrue(output['positive'].startswith('geometric style, '))
            self.assertNotIn('http_style', output['positive'])
            self.assertEqual(output['characters'], [])
        self.request('PUT', path, draft(205), version=started['version'], status=409)

    def test_start_replay_ignores_task_and_preview_lifetime_but_compares_full_projection(self):
        self.setup_driver()
        source, task, preview, body = self.prepared()
        path = self.p + f"/sources/{source['source_id']}"
        first = self.request('POST', path + '/start', body, version=source['version'])
        self.runtime.previews.values.clear()
        self.runtime.prompts.files.memory.clear()
        self.runtime.generation_sources.driver = None
        replay = self.request('POST', path + '/start', body, version='obsolete')
        self.assertTrue(replay['reconciled'])
        self.assertFalse(replay['dispatched'])
        self.assertEqual(len(self.driver.received), 1)
        self.request('POST', path + '/start', {**body, 'seeds_digest': '0' * 64}, status=409)
        projection = {k: body[k] for k in ('source_digest', 'seeds_digest', 'count')}
        self.assertTrue(self.request('POST', path + '/reconcile-start', projection)['reconciled'])
        self.assertEqual(first['source_id'], replay['source_id'])

    def test_precommit_rollback_and_unknown_commit_never_dispatch(self):
        self.setup_driver()
        source, _, _, body = self.prepared()
        path = self.p + f"/sources/{source['source_id']}"
        def before(stage):
            if stage == 'before_commit':
                raise OSError('private fault')
        self.runtime.context.handle._fault_hook = before
        self.request('POST', path + '/start', body, version=source['version'], status=503)
        self.runtime.context.handle._fault_hook = None
        self.assertFalse(self.request('GET', path)['locked'])
        self.assertEqual(self.request('GET', path + '/items')['items'], [])
        def after(stage):
            if stage == 'after_commit':
                self.runtime.context.handle._fault_hook = None
                raise OSError('lost commit reply')
        self.runtime.context.handle._fault_hook = after
        result = self.request('POST', path + '/start', body, version=source['version'])
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(self.driver.received, [])
        self.assertEqual(len(self.request('GET', path + '/items')['items']), 2)

    def test_resume_reads_only_locked_source_and_blocks_unresolved_sent(self):
        self.setup_driver()
        source, _, _, body = self.prepared()
        path = self.p + f"/sources/{source['source_id']}"
        started = self.request('POST', path + '/start', body, version=source['version'])
        paused = self.request('POST', path + '/control', {'action': 'pause'}, version=started['version'])
        self.runtime.prompts.files.memory.clear()
        self.runtime.previews.values.clear()
        resumed = self.request('POST', path + '/control', {'action': 'resume'}, version=paused['version'])
        self.assertEqual(len(self.driver.received), 2)
        self.assertEqual(self.driver.received[0][1], self.driver.received[1][1])
        self.request('POST', path + '/control', {'action': 'resume'}, version='stale')
        self.assertEqual(len(self.driver.received), 2)
        item = self.request('GET', path + '/items')['items'][0]
        submit_write(self.runtime.context.handle, lambda db: commands.mark_sent(db, item['item_id'])).result()
        paused = self.request('POST', path + '/control', {'action': 'pause'}, version=resumed['version'])
        self.request('POST', path + '/control', {'action': 'resume'}, version=paused['version'], status=409)
        self.assertEqual(len(self.driver.received), 2)

    def test_changed_source_trigger_capability_and_target_count_require_new_preview(self):
        self.setup_driver()
        source, task, preview, body = self.prepared()
        path = self.p + f"/sources/{source['source_id']}"
        self.driver.contract = 'b' * 64
        self.request('POST', path + '/start', body, version=source['version'], status=409)
        self.driver.contract = 'a' * 64
        updated = self.request('PUT', path, {**draft(), 'negative_prompt': 'changed'}, version=source['version'])
        self.request('POST', path + '/start', body, version=updated['version'], status=409)
        bad = self.request('PUT', path, {**draft(), 'target_count': 3}, version=updated['version'])
        self.request('POST', path + '/handoff-preview', {'task_id': task['task_id'], 'expected_task_revision': task['revision']}, version=bad['version'], status=422)

    def test_dispatch_failure_keeps_frozen_seeds_paused_and_replay_does_not_dispatch(self):
        self.setup_driver()
        source, _, _, body = self.prepared()
        path = self.p + f"/sources/{source['source_id']}"
        with patch.object(self.driver, 'dispatch', side_effect=OSError('unknown')) as dispatch:
            result = self.request('POST', path + '/start', body, version=source['version'])
            replay = self.request('POST', path + '/start', body, version=source['version'])
            self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(replay['runtime']['pause_reason'], 'generation:dispatch_unknown')

    def test_trigger_in_style_normalizes_whitespace_and_requires_full_index(self):
        self.setup_driver()
        source = self.request('POST', self.p + '/sources', {**draft(), 'style_prompt': 'http   style'}, status=201)
        task = self.task()
        result = self.request('POST', self.p + f"/sources/{source['source_id']}/handoff-preview", {
            'task_id': task['task_id'], 'expected_task_revision': task['revision']}, version=source['version'])
        self.assertFalse(result['can_start'])
        self.assertEqual(result['trigger_validation']['status'], 'invalid')

    def test_actual_product_task_cleanup_does_not_change_locked_source_or_start_replay(self):
        self.setup_driver()
        source, task, _, body = self.prepared()
        path = self.p + f"/sources/{source['source_id']}"
        self.request('POST', path + '/start', body, version=source['version'])
        before = self.request('GET', path + '/items')
        operation = self.runtime.prompts.active[task['task_id']]
        self.assertTrue(self.runtime.supervisor.tasks[operation].finished.wait(5))
        self.request('POST', '/api/prompt-production/task-cleanup', {
            'tasks': [{'task_id': task['task_id'], 'expected_revision': task['revision']}]})
        self.request('GET', '/api/prompt-production/tasks/' + task['task_id'], status=410)
        self.runtime.previews.values.clear()
        self.assertTrue(self.request('POST', path + '/start', body, version=source['version'])['reconciled'])
        self.assertEqual(self.request('GET', path + '/items'), before)
        self.assertEqual(len(self.driver.received), 1)

    def test_concurrent_start_is_one_commit_and_one_dispatch(self):
        self.setup_driver()
        source, _, _, body = self.prepared()
        service, context = self.runtime.generation_sources, self.runtime.context
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: service.start(context, source['source_id'], body, source['version']), range(4)))
        self.assertEqual(sum(value['dispatched'] for value in results), 1)
        self.assertEqual(sum(value['reconciled'] for value in results), 3)
        self.assertEqual(len(self.driver.received), 1)
        self.assertEqual(len(self.driver.received[0][1]), 2)

    def test_restart_reconciliation_uses_only_project_database(self):
        from fastapi.testclient import TestClient
        from stylelora.api.app import create_app
        from stylelora.runtime.lifecycle import Runtime
        self.setup_driver()
        source, _, _, body = self.prepared()
        self.request('POST', self.p + f"/sources/{source['source_id']}/start", body, version=source['version'])
        self.client.__exit__(None, None, None)
        self.runtime = Runtime(authority='127.0.0.1:8301')
        self.client = TestClient(create_app(self.runtime), base_url='http://127.0.0.1:8301',
            headers={'Authorization': 'Bearer ' + self.runtime.token})
        self.client.__enter__()
        opened = self.runtime.open_now(self.root)
        self.pid, self.p = opened['project_id'], '/api/projects/' + opened['project_id']
        replay = self.request('POST', self.p + f"/sources/{source['source_id']}/start", body, version=source['version'])
        self.assertTrue(replay['reconciled'])
        self.assertEqual(replay['status'], 'paused')
        self.assertFalse(replay['dispatched'])
        self.assertIsNone(self.runtime.generation_sources.driver)
        self.assertEqual(self.runtime.prompts.files.memory, {})

    def test_catalog_changes_while_waiting_for_writer_prevent_lock(self):
        self.setup_driver()
        source, _, _, body = self.prepared()
        service = self.runtime.generation_sources
        original = service._trigger
        calls = []
        def trigger(context, row):
            result = original(context, row)
            calls.append(result)
            if len(calls) == 1:
                self.runtime.catalog.state['catalog']['complete'] = False
                self.runtime.catalog._snapshot = CatalogSnapshot(self.runtime.catalog.state)
            return result
        path = self.p + f"/sources/{source['source_id']}"
        with patch.object(service, '_trigger', side_effect=trigger):
            result = self.request('POST', path + '/start', body, version=source['version'], status=409)
        self.assertEqual(result['error']['code'], 'generation:trigger_changed')
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.request('GET', path)['locked'])
        self.assertEqual(self.request('GET', path + '/items')['items'], [])

    def test_preview_checks_task_revision_type_and_source_binding(self):
        self.setup_driver()
        source, task, _, body = self.prepared()
        path = self.p + f"/sources/{source['source_id']}"
        self.request('POST', path + '/handoff-preview', {'task_id': task['task_id'],
            'expected_task_revision': task['revision'] - 1}, version=source['version'], status=409)
        other = self.request('POST', self.p + '/sources', draft(), status=201)
        self.request('POST', self.p + f"/sources/{other['source_id']}/start", body, version=other['version'], status=409)
        imported = self.import_inputs()
        imported_source = self.request('GET', self.p + f"/sources/{imported['source_id']}")
        self.request('POST', self.p + f"/sources/{imported['source_id']}/handoff-preview", {
            'task_id': task['task_id'], 'expected_task_revision': task['revision']}, version=imported_source['version'], status=422)
        self.assertEqual(self.request('GET', path + '/items')['items'], [])

    def test_creation_commit_reply_loss_and_cancel_draft(self):
        def after(stage):
            if stage == 'after_commit':
                self.runtime.context.handle._fault_hook = None
                raise OSError('lost creation reply')
        self.runtime.context.handle._fault_hook = after
        key = str(uuid4())
        source = self.request('POST', self.p + '/sources', draft(), status=201, key=key)
        self.assertEqual(source, self.request('POST', self.p + '/sources', draft(), status=201, key=key))
        self.assertEqual(len(self.request('GET', self.p + '/sources')['items']), 1)
        path = self.p + f"/sources/{source['source_id']}"
        canceled = self.request('POST', path + '/control', {'action': 'cancel'}, version=source['version'])
        self.assertEqual(canceled['status'], 'canceled')
        self.assertFalse(canceled['locked'])
        self.request('POST', path + '/control', {'action': 'resume'}, version=canceled['version'], status=409)

    def test_capability_schema_negative_locks_and_no_remote_references(self):
        config, driver = draft()['config'], FakeDriver()
        good = driver.capabilities()
        for key, value in [('contract_digest', 'z' * 64), ('negative_locked_by_effort', None),
                           ('config_schema', {'type': 'object', 'additionalProperties': False, '$dynamicRef': 'https://invalid/schema'})]:
            with self.subTest(key=key), self.assertRaises(ServiceError) as error:
                validate_capabilities(config, '', {**good, key: value})
            self.assertEqual(error.exception.code, 'generation:invalid_capabilities')
        with self.assertRaises(ServiceError) as error:
            validate_capabilities(config, 'negative', {**good, 'negative_locked_by_effort': ['high']})
        self.assertEqual(error.exception.code, 'generation:negative_locked')
        with self.assertRaises(ServiceError) as error:
            validate_capabilities({**config, 'default_aspect': 'square'}, '', good)
        self.assertEqual(error.exception.code, 'generation:unsupported_configuration')
