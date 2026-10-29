import threading
from unittest.mock import patch
from uuid import uuid4

from .api_support import APITest


class WorkbenchAPITests(APITest):
    def review_preview(self, query=None):
        return self.request('POST', self.p+'/images/review/preview', {'query': query or {}})

    def apply_preview(self, preview, target='keep', **options):
        return self.request('POST', self.p+'/images/review', {
            'query': preview['query'], 'expected_total': preview['total'],
            'preview_id': preview['preview_id'], 'digest': preview['digest'], 'target': target,
        }, **options)

    def edit_tags(self, image_id, tags):
        view = self.request('GET', self.p+f'/annotations/{image_id}')
        self.request('PUT', self.p+f'/annotations/{image_id}/manual',
                     {'tags': [{'tag': tag} for tag in tags], 'read_set': view['refs']}, version=view['version'])

    def test_full_query_review_ignores_browser_page_size_and_is_idempotent(self):
        self.import_inputs(3)
        page = self.request('GET', self.p+'/images?limit=1')
        self.assertEqual(len(page['items']), 1)
        preview = self.review_preview({'review_statuses': ['unreviewed']})
        self.assertEqual(preview['total'], 3)
        key = str(uuid4())
        result = self.apply_preview(preview, key=key)
        self.assertEqual(len(result['items']), 3)
        self.assertTrue(all(item['image']['review_status'] == 'keep' for item in result['items']))
        self.assertEqual(self.apply_preview(preview, key=key), result)
        self.apply_preview(preview, status=409)

    def test_query_review_detects_membership_swap_with_unchanged_total(self):
        self.import_inputs(2)
        self.edit_tags(1, ['one'])
        self.edit_tags(2, ['two'])
        preview = self.review_preview({'q': 'one'})
        self.assertEqual(preview['total'], 1)
        self.edit_tags(1, ['two'])
        self.edit_tags(2, ['one'])
        self.apply_preview(preview, status=409)
        self.assertTrue(all(row['review_status'] == 'unreviewed' for row in self.request('GET', self.p+'/images')['items']))

    def test_query_review_checks_every_frozen_version_and_rolls_back(self):
        self.import_inputs(3)
        preview = self.review_preview()
        self.keep([2])
        self.apply_preview(preview, 'reject', status=409)
        self.assertEqual([i['review_status'] for i in self.request('GET', self.p+'/images')['items']], ['unreviewed', 'keep', 'unreviewed'])

    def test_review_returns_before_version_for_contiguous_cas_undo(self):
        self.import_inputs()
        original = self.request('GET', self.p+'/images/1')
        result = self.keep([1])['items'][0]
        self.assertEqual(result['old_version'], original['version'])
        self.assertNotEqual(result['image']['version'], original['version'])
        undone = self.request('POST', self.p+'/images/review', {
            'ids': [1], 'targets': {'1': result['old_review_status']},
            'expected_versions': {'1': result['image']['version']},
        })['items'][0]
        self.assertEqual(undone['old_version'], result['image']['version'])
        self.assertEqual(undone['image']['review_status'], 'unreviewed')
        self.assertNotEqual(undone['image']['version'], original['version'])

    def test_empty_query_review_and_scope_validation(self):
        preview = self.review_preview()
        self.assertEqual(self.apply_preview(preview)['items'], [])
        self.request('POST', self.p+'/images/review', {'query': {}, 'target': 'keep'}, status=428)
        self.request('POST', self.p+'/images/review', {'query': {}, 'ids': [1], 'target': 'keep'}, status=422)
        self.request('POST', self.p+'/images/review', {'target': 'keep'}, status=422)

    def test_preview_total_query_and_digest_are_bound(self):
        self.import_inputs()
        preview = self.review_preview()
        self.apply_preview({**preview, 'total': 2}, status=409)
        self.apply_preview({**preview, 'query': {'review_statuses': ['keep']}}, status=409)
        self.apply_preview({**preview, 'digest': '0'*64}, status=409)

    def test_stats_obey_same_filters_as_image_list(self):
        self.import_inputs(3)
        self.keep([1, 2])
        stats = self.request('GET', self.p+'/images/stats?review_status=keep&q=blue')
        self.assertEqual(stats['total'], 2)
        group = next(g for g in stats['groups'] if g['by'] == 'review_status')
        self.assertEqual(group['buckets'], [{'key': 'keep', 'count': 2}])
        self.assertEqual(self.request('GET', self.p+'/images/stats?file_missing=true')['total'], 0)
        self.request('GET', self.p+'/images/stats?unsupported=true', status=422)

    def test_operations_list_is_paginated_stable_and_omits_results(self):
        self.import_inputs()
        first = self.request('GET', '/api/operations?limit=1&project_id='+self.pid)
        self.assertEqual(len(first['items']), 1)
        self.assertIsNone(first['items'][0]['result'])
        ids = [first['items'][0]['operation_id']]
        page = first
        while page['next_cursor']:
            page = self.request('GET', '/api/operations?limit=1&project_id='+self.pid+'&cursor='+page['next_cursor'])
            ids.extend(item['operation_id'] for item in page['items'])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 3)
        self.request('GET', '/api/operations?project_id='+str(uuid4()), status=410)
        self.request('GET', '/api/operations?limit=0', status=422)

    def test_native_picker_grants_are_scoped_and_single_dialog_is_enforced(self):
        picker = self.runtime.picker
        picker.available = True
        entered, released = threading.Event(), threading.Event()
        def choose(purpose, cancel):
            entered.set()
            released.wait(5)
            return self.inputs
        with patch.object(picker, 'choose', choose):
            try:
                accepted = self.request('POST', '/api/filesystem/grants', {'purpose': 'import_root', 'project_id': self.pid}, status=202)
                self.assertTrue(entered.wait(2))
                self.request('POST', '/api/filesystem/grants', {'purpose': 'project_open'}, status=409)
                self.assertGreater(self.runtime.context.users, 0)
            finally:
                released.set()
            grant = self.wait_operation(accepted)['result']
        self.assertEqual(grant['project_id'], self.pid)
        self.assertEqual(grant['purpose'], 'import_root')
        self.assertNotIn(str(self.inputs), str(grant))
        self.assertEqual(self.runtime.grants.get(grant['grant_id'], 'import_root', self.pid), self.inputs)

    def test_native_picker_user_cancel_does_not_create_grant_or_hold_gate(self):
        picker = self.runtime.picker
        picker.available = True
        with patch.object(picker, 'choose', return_value=None):
            for _ in range(2):
                accepted = self.request('POST', '/api/filesystem/grants', {'purpose': 'project_open'}, status=202)
                task = self.wait_operation(accepted, expected='canceled')
                self.assertIsNone(task['result'])

    def test_native_picker_task_cancel_releases_project_lease(self):
        picker = self.runtime.picker
        picker.available = True
        entered = threading.Event()
        def choose(purpose, cancel):
            entered.set()
            cancel.wait(5)
            return None
        with patch.object(picker, 'choose', choose):
            accepted = self.request('POST', '/api/filesystem/grants', {'purpose': 'import_root', 'project_id': self.pid}, status=202)
            self.assertTrue(entered.wait(2))
            task = self.request('GET', accepted['status_url'])
            self.request('POST', accepted['status_url']+'/cancel', {}, version=task['version'])
            self.wait_operation(accepted, expected='canceled')
        self.assertEqual(self.runtime.context.users, 0)

    def test_compiled_frontend_is_served_with_assets_and_api_404_stays_json(self):
        from pathlib import Path
        root = Path(__file__).parents[1]/'src/stylelora/api/web_dist'
        if not root.exists():
            self.skipTest('Build the web bundle before packaging integration checks')
        response = self.client.get('/')
        self.assertIn('id="root"', response.text)
        self.assertIn("script-src 'self'", response.headers['content-security-policy'])
        asset = next((root/'assets').glob('index-*.js'))
        response = self.client.get('/assets/'+asset.name)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, asset.read_bytes())
        self.assertIn('immutable', response.headers['cache-control'])
        self.request('GET', '/api/not-a-route', status=404)
        self.assertEqual(self.client.get('/assets/%2E%2E/project.json').status_code, 404)
