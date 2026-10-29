from hashlib import sha256
import io
import json
import unittest
from uuid import uuid4
from zipfile import ZipFile

from stylelora.prompt_production import exchange
from stylelora.tagging.catalog import TagCatalog
from tests import test_prompt_production as fixtures
from tests.test_prompt_production import entry


class DictionaryExchangeTests(unittest.TestCase):
    setUp = fixtures.PromptTests.setUp
    tearDown = fixtures.PromptTests.tearDown
    request = fixtures.PromptTests.request
    dictionary = fixtures.PromptTests.dictionary

    def preview(self, text, format='csv', kind='features', dimension=None):
        response = self.client.post(self.p + '/imports/preview',
            headers={**self.headers, 'Idempotency-Key': str(uuid4())},
            files={'file': ('ignored.' + format, text.encode('utf-8'), 'application/octet-stream')},
            data={'kind': kind, 'format': format, **({'dimension': dimension} if dimension else {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def select(self, preview, rows, *, status=200, key=None):
        return self.request('POST', '/imports/select', {'preview_id': preview['preview_id'], 'digest': preview['digest'], 'rows': rows}, status=status, key=key)

    def records(self, preview, suffix=''):
        return self.request('GET', f"/imports/{preview['preview_id']}/rows?digest={preview['digest']}" + suffix)

    def download(self, dictionary, format='txt', ack='true', version=True):
        return self.client.get(self.p + '/dictionaries/' + dictionary['dictionary_id'] + '/export?format=' + format + '&acknowledge_lossy=' + ack,
                               headers={**self.headers, **({'If-Match': '"' + (dictionary['version'] if version is True else version) + '"'} if version else {})})

    def test_feature_archive_safe_paths_manifest_ratings_determinism_and_reimport(self):
        dimensions = {name: [{'tag': 'forest', 'rating': None}, {'tag': 'beach', 'rating': 'explicit'}]
                      for name in ('../outside', 'CON', 'Scene', 'scene', '中文维度')}
        d = self.dictionary({'kind': 'features', 'name': '特征交换', 'dimensions': dimensions})
        response = self.download(d)
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertEqual(response.headers['content-type'], 'application/zip')
        self.assertIn('.zip', response.headers['content-disposition'])
        self.assertEqual(response.headers['x-prompt-lossy-fields'], 'dictionary_envelope')
        self.assertEqual(response.content, self.download(d).content)
        with ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), [f'{i:03d}.txt' for i in range(1, 6)] + ['dimensions.json'])
            manifest = json.loads(archive.read('dimensions.json'))
            self.assertEqual({v['dimension'] for v in manifest['files']}, set(dimensions))
            self.assertNotIn(d['dictionary_id'], archive.read('dimensions.json').decode())
            for item in manifest['files']:
                data = archive.read(item['filename'])
                self.assertEqual(data, b'forest\nbeach\texplicit\n')
                self.assertEqual(sha256(data).hexdigest(), item['sha256'])
                self.assertEqual(item['count'], 2)
                payload, report = exchange.parse(data, 'txt', 'features', item['dimension'], self.runtime.catalog.snapshot())
                self.assertTrue(report['valid'])
                self.assertEqual(payload['dimensions'][item['dimension']], dimensions[item['dimension']])
        self.assertEqual(self.request('GET', '/dictionaries/' + d['dictionary_id']), d)

    def test_txt_requires_explicit_ack_and_current_dictionary_version(self):
        for body in ({'kind': 'features', 'name': 'f', 'dimensions': {'x': [{'tag': 'forest'}]}},
                     {'kind': 'prompts', 'name': 'p', 'entries': [entry(['forest'], natural_language='A river.\nA tree.', aspect='landscape', rating='general')]}):
            d = self.dictionary(body)
            self.assertEqual(self.download(d, ack='false').status_code, 422)
            self.assertEqual(self.download(d, version=False).status_code, 428)
            self.assertEqual(self.download(d, version='stale').status_code, 409)
            self.assertEqual(self.download(d, ack='1').status_code, 422)
            response = self.download(d)
            self.assertEqual(response.status_code, 200)
            if d['kind'] == 'prompts':
                self.assertEqual(response.content, b'forest, A river. A tree.\n')
                self.assertEqual(response.headers['x-prompt-lossy-fields'], 'aspect,rating,structure')
            self.request('PUT', '/dictionaries/' + d['dictionary_id'], {**body, 'name': 'changed'}, version=d['version'])
            self.assertEqual(self.download(d).status_code, 409)

    def test_json_csv_still_preserve_entries_and_private_receipts_are_excluded(self):
        d = self.dictionary({'kind': 'features', 'name': '导出', 'dimensions': {'场景': [{'tag': 'forest', 'rating': None}]}})
        for format in ('json', 'csv'):
            response = self.download(d, format=format, version=False)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'_operations', response.content)
            payload, report = exchange.parse(response.content, format, 'features', None, self.runtime.catalog.snapshot())
            self.assertTrue(report['valid'], report)
            self.assertEqual(payload['dimensions'], d['dimensions'])

    def test_select_valid_rows_beyond_examples_and_commit_replays_after_preview_expiry(self):
        text = 'dimension,tag,rating\n' + '\n'.join(f'scene,item {n},general' for n in range(205)) + '\nscene,,general\n'
        p = self.preview(text)
        self.assertFalse(p['valid'])
        self.assertEqual(p['row_count'], 206)
        self.assertEqual(p['selectable_count'], 205)
        first = self.records(p, '&limit=200')
        second = self.records(p, '&limit=200&cursor=' + first['next_cursor'])
        self.assertEqual(second['items'][-1]['row'], 207)
        self.assertIsNone(second['items'][-1]['value'])
        chosen = self.select(p, [206, 2])
        self.assertTrue(chosen['valid'])
        self.assertNotEqual(chosen['digest'], p['digest'])
        self.assertEqual(chosen['selection']['excluded_count'], 204)
        self.assertEqual([v['tag'] for v in chosen['examples']], ['item 0', 'item 204'])
        narrowed = self.select(chosen, [206])
        self.assertEqual(narrowed['selection']['excluded_count'], 205)
        self.assertEqual(narrowed['selection']['rows'], [206])
        d = self.dictionary({'kind': 'features', 'name': 'target', 'dimensions': {'scene': [{'tag': 'original'}]}})
        body, key = {'preview_id': chosen['preview_id'], 'digest': chosen['digest']}, str(uuid4())
        self.request('POST', '/dictionaries/' + d['dictionary_id'] + '/imports', {'preview_id': p['preview_id'], 'digest': p['digest']}, version=d['version'], status=422)
        saved = self.request('POST', '/dictionaries/' + d['dictionary_id'] + '/imports', body, version=d['version'], key=key)
        self.runtime.previews.values.clear()
        self.assertEqual(saved, self.request('POST', '/dictionaries/' + d['dictionary_id'] + '/imports', body, version=d['version'], key=key))
        current = self.request('GET', '/dictionaries/' + d['dictionary_id'])
        self.assertEqual([v['tag'] for v in current['dimensions']['scene']], ['original', 'item 0', 'item 204'])

    def test_selection_validates_rows_and_idempotency(self):
        p = self.preview('forest\n\nbeach\tsafe\nsunlight\tgeneral', 'txt', dimension='scene')
        for rows in ([], [1, 1], [True], ['1'], [0], [10002], [2], [3], [99]):
            self.select(p, rows, status=422)
        key = str(uuid4())
        chosen = self.select(p, [4, 1], key=key)
        self.assertEqual(chosen, self.select(p, [1, 4], key=key))
        self.select(p, [1], key=key, status=409)
        self.assertEqual(chosen['selection']['rows'], [1, 4])

    def test_conflicting_ratings_require_selecting_a_nonconflicting_subset(self):
        p = self.preview('tags,natural_language,aspect,rating\n"[""forest""]",,,general\n"[""forest""]",,,explicit\n', kind='prompts')
        self.assertFalse(p['valid'])
        self.assertTrue(p['selection_available'])
        self.assertFalse(self.select(p, [2, 3])['valid'])
        chosen = self.select(p, [3])
        self.assertTrue(chosen['valid'])
        self.assertEqual(chosen['examples'][0]['rating'], 'explicit')

    def test_feature_duplicates_recheck_rating_merge_in_selected_preview(self):
        p = self.preview('forest\tgeneral\nforest\texplicit\nbeach\tgeneral', 'txt', dimension='scene')
        selected = self.select(p, [1, 2])
        self.assertEqual(selected['duplicates'], 1)
        self.assertEqual(selected['examples'][0]['rating'], 'explicit')
        self.assertEqual(selected['issues'][0]['code'], 'dictionary:rating_merged')

    def test_invalid_txt_segment_cannot_be_laundered_through_valid_remainder(self):
        catalog = TagCatalog('fixture', {'forest': 'forest'}, {})
        private, report = exchange.parse(b'forest, (bad:1.2)\nforest', 'txt', 'prompts', None, catalog, with_rows=True)
        self.assertFalse(report['valid'])
        self.assertIsNone(private['rows'][0]['value'])
        self.assertEqual(report['selectable_count'], 1)

    def test_json_rows_keep_global_ordinals_and_strict_envelope(self):
        raw = {'schema_version': 1, 'kind': 'features', 'dictionary_id': str(uuid4()), 'revision': 1, 'name': '导入',
               'dimensions': {'one': [{'tag': 'forest'}, {'tag': ''}], 'two': [{'tag': 'beach'}, {'tag': 'moonlight', 'dimension': 'override'}]}}
        p = self.preview(json.dumps(raw), 'json')
        self.assertFalse(p['valid'])
        self.assertEqual([row['row'] for row in self.records(p)['items']], [1, 2, 3, 4])
        chosen = self.select(p, [3, 1])
        self.assertTrue(chosen['valid'])
        self.assertEqual([row['dimension'] for row in chosen['examples']], ['one', 'two'])
        raw['unexpected'] = True
        bad = self.preview(json.dumps(raw), 'json')
        self.assertFalse(bad['selection_available'])
        self.select(bad, [1], status=422)

    def test_malformed_csv_does_not_allow_truncated_partial_selection(self):
        p = self.preview('dimension,tag,rating\nscene,forest,general\n"unterminated')
        self.assertFalse(p['selection_available'])
        self.assertEqual(p['count'], 1)
        self.select(p, [2], status=422)

    def test_row_cursor_digest_scope_expiry_auth_and_unknown_query(self):
        p = self.preview('forest\nbeach', 'txt', dimension='scene')
        other = self.preview('forest\nbeach', 'txt', dimension='scene')
        cursor = self.records(p, '&limit=1')['next_cursor']
        path = f"/imports/{other['preview_id']}/rows?digest={other['digest']}&cursor={cursor}"
        self.request('GET', path, status=422)
        self.request('GET', f"/imports/{p['preview_id']}/rows?digest={'a' * 64}", status=409)
        self.request('GET', f"/imports/{p['preview_id']}/rows?digest={'中' * 64}", status=422)
        self.request('GET', f"/imports/{p['preview_id']}/rows?digest={p['digest']}&bad=1", status=422)
        self.assertEqual(self.client.get(self.p + f"/imports/{p['preview_id']}/rows?digest={p['digest']}").status_code, 401)
        self.runtime.previews.values.clear()
        self.select(p, [1], status=410)

    def test_selected_commit_keeps_version_conflict_and_target_rating_conflict_guards(self):
        d = self.dictionary({'kind': 'prompts', 'name': 'target', 'entries': [entry(['forest'], rating='general')]})
        p = self.preview('tags,natural_language,aspect,rating\n"[""forest""]",,,explicit\n', kind='prompts')
        chosen = self.select(p, [2])
        body = {'preview_id': chosen['preview_id'], 'digest': chosen['digest']}
        path = '/dictionaries/' + d['dictionary_id'] + '/imports'
        self.request('POST', path, body, version='stale', status=409)
        self.request('POST', path, body, version=d['version'], status=409)
        self.assertEqual(self.request('GET', '/dictionaries/' + d['dictionary_id']), d)

    def test_source_digest_binds_invalid_rows_outside_examples(self):
        text = 'forest\n' * 100 + 'beach\tsafe\n'
        p = self.preview(text, 'txt', dimension='scene')
        other = self.preview(text.replace('beach', 'sunlight'), 'txt', dimension='scene')
        self.assertEqual(p['examples'], other['examples'])
        self.assertNotEqual(p['digest'], other['digest'])
        self.assertNotEqual(p['source_digest'], other['source_digest'])

    def test_selection_does_not_reread_catalog_aliases(self):
        catalog = TagCatalog('old', {'forest': 'old forest'}, {})
        private, report = exchange.parse(b'forest\nbeach\tsafe', 'txt', 'features', 'scene', catalog, with_rows=True)
        frozen = self.runtime.previews.add(None, 'prompt-import', report, private)
        catalog.entries['forest'] = 'new forest'
        selected = self.select(frozen, [1])
        self.assertEqual(selected['examples'][0]['tag'], 'old forest')
        self.assertEqual(selected['catalog_revision'], 'old')
        self.assertEqual(private['rows'][0]['value']['tag'], 'old forest')
