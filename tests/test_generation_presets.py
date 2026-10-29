from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from stylelora.generation.presets import BUILTIN_IDS, PresetStore
from stylelora.generation.profile import SIZES, build_request
from stylelora.llm.types import LlmFailure
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import sources
from stylelora.project_db.storage import atomic_json
from stylelora.project_db.types import ItemSeed
from stylelora.project_db.validation import digest, primitive
from stylelora.runtime.errors import ServiceError
from .api_support import APITest
from .test_generation_handoff import draft


def body(name='自定义', size='small'):
    return {'name': name, 'sizes': deepcopy(SIZES[size])}


class PresetTests(APITest):
    def setUp(self):
        super().setUp()
        self.directory = self.base / 'product'
        self.runtime.presets.path = self.directory / 'generation_presets.json'
        self.store = self.runtime.presets

    def call(self, method, path='', payload=None, *, status=200, version=None, key=None):
        return self.request(method, '/api/presets' + path, payload, status=status, version=version, key=key,
                            headers={'Idempotency-Key': key or str(uuid4())})

    def create(self, value=None):
        return self.call('POST', payload=value or body(), status=201)

    def test_builtin_sizes_default_read_only_and_not_written_on_read(self):
        page = self.call('GET')
        self.assertEqual(page['default_preset_id'], BUILTIN_IDS['small'])
        self.assertEqual(page['total'], 2)
        for preset in page['items']:
            self.assertTrue(preset['builtin'])
            self.assertEqual(preset['sizes'], SIZES[preset['builtin_key']])
            self.assertEqual(preset, self.call('GET', '/' + preset['preset_id']))
            for method in ('PUT', 'DELETE'):
                response = self.call(method, '/' + preset['preset_id'], body() if method == 'PUT' else None,
                                     version=preset['version'], status=409)
                self.assertEqual(response['error']['code'], 'preset:builtin_locked')
        self.assertFalse(self.store.path.exists())
        caps = self.request('GET', '/api/generation/capabilities')
        self.assertEqual(caps['default_preset_id'], page['default_preset_id'])
        self.assertFalse(caps['generation'])

    def test_custom_crud_persists_across_store_restart_without_project_references(self):
        created = self.create(body('  测试  '))
        self.assertEqual(created['name'], '测试')
        path = '/' + created['preset_id']
        changed = self.call('PUT', path, body('更新', 'large'), version=created['version'])
        restored = PresetStore(self.runtime, self.directory)
        self.assertEqual(restored.get(created['preset_id']), changed)
        self.assertEqual(changed['revision'], 2)
        self.assertNotIn(self.pid, self.store.path.read_text())
        self.call('DELETE', path, version=changed['version'], status=204)
        self.assertEqual(PresetStore(self.runtime, self.directory).list()['total'], 2)
        self.call('GET', path, status=404)

    def test_invalid_pixels_types_dimensions_orientation_and_name_do_not_write(self):
        values = []
        for width in (True, 768.0, '768', 0, -64, 769, 16448):
            value = body(); value['sizes']['landscape']['width'] = width; values.append(value)
        for aspect in ('landscape', 'portrait', 'square'):
            value = body(); value['sizes'][aspect] = {'width': 640, 'height': 512}; values.append(value)
        # Landscape above is valid; use equal dimensions to exercise its invalid direction.
        values[-3]['sizes']['landscape'] = {'width': 640, 'height': 640}
        for name in ('', '  ', 'bad\nname', 'x' * 201):
            values.append(body(name))
        value = body(); del value['sizes']['square']; values.append(value)
        value = body(); value['sizes']['unknown'] = {'width': 64, 'height': 64}; values.append(value)
        values.extend([{**body(), 'builtin': False}, {**body(), 'preset_id': str(uuid4())}])
        for value in values:
            self.call('POST', payload=value, status=422)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.call('GET')['total'], 2)

    def test_version_conflict_aba_noop_and_missing_preconditions(self):
        first = self.create()
        path = '/' + first['preset_id']
        self.call('PUT', path, body('no version'), status=428)
        self.call('DELETE', path, status=428)
        same = self.call('PUT', path, body(), version=first['version'])
        self.assertEqual(same, first)
        second = self.call('PUT', path, body('B'), version=first['version'])
        third = self.call('PUT', path, body(), version=second['version'])
        self.assertNotEqual(third['version'], first['version'])
        self.call('PUT', path, body('stale'), version=first['version'], status=409)
        self.call('DELETE', path, version=first['version'], status=409)
        self.assertEqual(self.call('GET', path), third)

    def test_session_idempotency_create_update_delete_and_key_conflicts(self):
        key = str(uuid4())
        first = self.call('POST', payload=body(), key=key, status=201)
        self.assertEqual(first, self.call('POST', payload=body(), key=key, status=201))
        self.call('POST', payload=body('different'), key=key, status=409)
        path, key = '/' + first['preset_id'], str(uuid4())
        updated = self.call('PUT', path, body('updated'), version=first['version'], key=key)
        self.assertEqual(updated, self.call('PUT', path, body('updated'), version=first['version'], key=key))
        self.call('PUT', path, body('updated'), version=updated['version'], key=key, status=409)
        key = str(uuid4())
        self.call('DELETE', path, version=updated['version'], key=key, status=204)
        self.call('DELETE', path, version=updated['version'], key=key, status=204)
        self.call('DELETE', path, version=updated['version'], status=404)
        self.assertEqual(self.call('GET')['total'], 2)

    def test_concurrent_updates_have_one_winner(self):
        first = self.create()
        def change(name):
            try:
                return self.store.update(first['preset_id'], body(name), first['version'])
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(change, ('one', 'two')))
        self.assertEqual(results.count('concurrency:version_conflict'), 1)
        self.assertEqual(self.store.get(first['preset_id'])['revision'], 2)

    def test_atomic_failure_preserves_old_bytes_and_after_replace_is_reconciled(self):
        first = self.create()
        before = self.store.path.read_bytes()
        with patch('stylelora.generation.presets.atomic_json', side_effect=OSError('private-path')):
            failure = self.call('PUT', '/' + first['preset_id'], body('failed'), version=first['version'], status=503)
        self.assertNotIn('private-path', json.dumps(failure))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get(first['preset_id']), first)
        def after_replace(path, value):
            atomic_json(path, value)
            raise OSError('after replace')
        key = str(uuid4())
        with patch('stylelora.generation.presets.atomic_json', side_effect=after_replace):
            saved = self.call('PUT', '/' + first['preset_id'], body('committed'), version=first['version'], key=key)
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(saved, self.call('PUT', '/' + first['preset_id'], body('committed'), version=first['version'], key=key))

    def test_corrupt_configuration_is_not_silently_replaced(self):
        first = self.create()
        good = json.loads(self.store.path.read_bytes())
        bad = [b'{bad', b'{"content":{},"content":{}}']
        for mutate in (
            lambda value: value.update(schema_version=True),
            lambda value: value.update(revision=True),
            lambda value: value.update(extra=True),
            lambda value: value['presets'][first['preset_id']].update(revision=True),
            lambda value: value['presets'][first['preset_id']].update(preset_id=str(uuid4())),
            lambda value: value['presets'][first['preset_id']]['sizes']['square'].update(width=65),
        ):
            value = deepcopy(good['content']); mutate(value)
            bad.append(json.dumps({'content': value, 'digest': digest(value)}).encode())
        bad.append(json.dumps({**good, 'digest': '0' * 64}).encode())
        for data in bad:
            self.store.path.write_bytes(data)
            self.call('GET', status=503)
            self.call('POST', payload=body('new'), status=503)
            self.assertEqual(self.store.path.read_bytes(), data)

    def test_storage_path_safety_limits_and_returned_values_are_copies(self):
        first = self.create()
        first['sizes']['square']['width'] = 1
        self.assertEqual(self.store.get(first['preset_id'])['sizes'], SIZES['small'])
        before = self.store.path.read_bytes()
        with patch('stylelora.generation.presets.safe_path', side_effect=LlmFailure('unsafe_storage')):
            self.call('POST', payload=body(), status=503)
        with patch('stylelora.generation.presets.MAX_PRESETS', 1):
            self.call('POST', payload=body(), status=413)
        with patch('stylelora.generation.presets.MAX_BYTES', 1):
            self.call('GET', status=503)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_pagination_invalidates_after_change_and_auth_query_guards(self):
        page = self.call('GET', '?limit=1')
        second = self.call('GET', '?limit=1&cursor=' + page['next_cursor'])
        self.assertNotEqual(page['items'][0]['preset_id'], second['items'][0]['preset_id'])
        self.create()
        self.call('GET', '?limit=1&cursor=' + page['next_cursor'], status=409)
        for query in ('?limit=1001', '?limit=1&limit=2', '?bad=1', '?cursor=bad'):
            self.call('GET', query, status=422)
        self.call('GET', '/not-uuid', status=422)
        self.call('DELETE', '/' + BUILTIN_IDS['small'], payload={}, status=422)
        response = self.client.get('/api/presets', headers={'Authorization': 'Bearer invalid'})
        self.assertEqual(response.status_code, 401)

    def test_draft_and_locked_source_keep_copied_pixels_after_preset_update_and_delete(self):
        preset = self.create(body('source sizes', 'large'))
        raw = draft(2); raw['config']['sizes'] = deepcopy(preset['sizes'])
        source = self.request('POST', self.p + '/sources', raw, status=201)
        before = deepcopy(source['config_json'])
        changed = self.call('PUT', '/' + preset['preset_id'], body('changed', 'small'), version=preset['version'])
        self.assertEqual(self.request('GET', self.p + '/sources/' + str(source['source_id']))['config_json'], before)
        seeds = (ItemSeed(seq=1, prompt='forest', aspect=None), ItemSeed(seq=2, prompt='beach', aspect='landscape'))
        submit_write(self.runtime.context.handle, lambda db: commands.start_source(db, source['source_id'], seeds)).result()
        self.call('DELETE', '/' + preset['preset_id'], version=changed['version'], status=204)
        with read_snapshot(self.runtime.context.handle) as db:
            locked = sources.get_source(db, source['source_id'])
            self.assertTrue(locked.locked)
            self.assertEqual(primitive(locked.config_json), before)
            self.assertEqual(build_request(locked, seeds[0])['width'], 1024)
            self.assertEqual(build_request(locked, seeds[1])['width'], 1536)
        self.assertNotIn(preset['preset_id'], json.dumps(before))
        self.assertFalse(self.request('GET', '/api/generation/capabilities')['generation'])
