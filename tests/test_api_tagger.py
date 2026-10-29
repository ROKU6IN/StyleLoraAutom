import threading
from uuid import uuid4

from stylelora.project_db.connection import read_snapshot
from stylelora.services.tagger import TaggerService
from stylelora.tagging.models import ModelStore
from .api_support import APITest
from .tagger_support import fixture, MemoryDownload, RUNTIME, DEVICE


class TaggerAPITest(APITest):
    def setUp(self):
        super().setUp()
        self.manifest, self.content = fixture()
        self.download = MemoryDownload(self.content)
        self.store = ModelStore(self.base/'models', download=self.download, validator=lambda *_: None)
        self.configure()

    def configure(self, **kwargs):
        self.runtime.tagger = TaggerService(self.runtime, manifests=(self.manifest,), store=self.store,
            device_probe=lambda: dict(DEVICE), status=lambda: dict(RUNTIME), **kwargs)

    def start(self, **kwargs):
        return self.request('POST', '/api/tagger/models/install', {'manifest_id': self.manifest.digest}, status=202, **kwargs)

    def models(self):
        return self.request('GET', '/api/tagger/models')['items']

    def test_default_registry_requires_explicit_install_and_empty_registry_still_supported(self):
        self.runtime.tagger = TaggerService(self.runtime, store=self.store, device_probe=lambda: DEVICE, status=lambda: RUNTIME)
        caps = self.request('GET', '/api/tagger/capabilities')
        self.assertFalse(caps['available'])
        self.assertEqual(caps['reason'], 'tagger:model_missing')
        self.assertEqual(caps['trusted_manifest_count'], 1)
        self.assertFalse(caps['launcher']['available'])
        self.assertEqual(self.models()[0]['provenance']['csv_git_blob'], 'a2a653a5c89c8e387765eb1de9cfd2aedc92ee8d')
        self.assertEqual(self.models()[0]['state'], 'missing')
        self.request('POST', '/api/tagger/models/install', {'manifest_id': self.manifest.digest}, status=422)
        self.assertEqual(self.download.calls, [])
        self.assertFalse(self.store.root.exists())
        self.assertFalse(self.runtime.session()['capabilities']['wd'])
        self.runtime.tagger = TaggerService(self.runtime, manifests=(), store=self.store, device_probe=lambda: DEVICE, status=lambda: RUNTIME)
        self.assertEqual(self.request('GET', '/api/tagger/capabilities')['manifest_reason'], 'tagger:manifest_unconfigured')
        self.assertEqual(self.models(), [])

    def test_install_poll_and_reverify_are_product_tasks_and_do_not_change_project(self):
        self.import_inputs(1)
        with read_snapshot(self.runtime.context.handle) as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM images')]
            job_count = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        result = self.wait_operation(self.start())
        self.assertEqual(result['kind'], 'tagger.install')
        self.assertIsNone(result['project_id'])
        self.assertEqual(result['result']['state'], 'ready')
        self.assertEqual(self.models()[0]['state'], 'ready')
        self.assertEqual(len(self.download.calls), 2)
        again = self.wait_operation(self.start())
        self.assertTrue(again['result']['reused'])
        self.assertEqual(len(self.download.calls), 2)
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM images')])
            self.assertEqual(job_count, db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        self.assertNotIn(str(self.base), str(result))

    def test_idempotency_and_singleflight_and_cancel_with_cas(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(manifest, file, cancel):
            entered.set()
            while not release.wait(.01):
                if cancel.is_set():
                    return
            yield self.content[file.name]
        self.store.download = slow
        key = str(uuid4())
        accepted = self.start(key=key)
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.start(key=key)['installation_id'], accepted['installation_id'])
        self.assertEqual(self.start()['installation_id'], accepted['installation_id'])
        url = accepted['status_url']
        self.request('DELETE', url, status=428)
        self.request('DELETE', url, version='stale', status=409)
        current = self.request('GET', url)
        self.request('DELETE', url, version=current['version'], status=204)
        result = self.wait_operation(accepted, expected='canceled')
        self.assertEqual(result['phase'], 'canceled')
        self.assertFalse(self.store.directory(self.manifest).exists())
        self.assertEqual(list(self.store.root.rglob('*.partial')), [])
        release.set()
        self.wait_operation(self.start())
        self.assertEqual(self.models()[0]['state'], 'ready')

    def test_pending_cancel_skips_download_and_can_retry(self):
        blockers = [threading.Event(), threading.Event()]
        started = [threading.Event(), threading.Event()]
        for index in range(2):
            self.addCleanup(blockers[index].set)
            def block(task, i=index):
                started[i].set()
                blockers[i].wait(5)
            self.runtime.supervisor.submit('fixture.block', block)
            self.assertTrue(started[index].wait(2))
        accepted = self.start()
        current = self.request('GET', accepted['status_url'])
        self.assertEqual(current['status'], 'pending')
        self.request('DELETE', accepted['status_url'], version=current['version'], status=204)
        for blocker in blockers:
            blocker.set()
        self.wait_operation(accepted, expected='canceled')
        self.assertEqual(self.download.calls, [])
        self.wait_operation(self.start())

    def test_cancel_after_publish_preserves_ready_model(self):
        accepted = self.start()
        result = self.wait_operation(accepted)
        marker = self.store.directory(self.manifest)/'ready.json'
        before = marker.read_bytes()
        self.request('DELETE', accepted['status_url'], version=result['version'], status=204)
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(self.models()[0]['state'], 'ready')

    def test_failure_is_safe_and_no_ready_state(self):
        self.download.content = {**self.content, 'model.onnx': b'bad model'}
        result = self.wait_operation(self.start(), expected='failed')
        self.assertEqual(result['error']['code'], 'tagger:checksum_mismatch')
        self.assertNotIn(str(self.base), str(result))
        self.assertEqual(self.models()[0]['state'], 'missing')
        self.assertFalse(self.store.directory(self.manifest).exists())

    def test_restart_requires_revalidation_and_file_change_invalidates_cached_ready(self):
        self.wait_operation(self.start())
        self.configure()
        self.assertEqual(self.models()[0]['state'], 'unverified')
        self.wait_operation(self.start())
        path = self.store.directory(self.manifest)/'model.onnx'
        path.write_bytes(b'changed')
        model = self.models()[0]
        self.assertEqual(model['state'], 'invalid')
        self.assertEqual(model['reason'], 'tagger:model_changed')
        self.assertEqual(self.models()[0]['reason'], 'tagger:model_changed')
        result = self.wait_operation(self.start(), expected='failed')
        self.assertEqual(result['error']['code'], 'tagger:checksum_mismatch')
        self.assertEqual(path.read_bytes(), b'changed')

    def test_unavailable_runtime_rejects_before_download(self):
        self.runtime.tagger.status = lambda: {'available': False, 'reason': 'tagger:runtime_missing'}
        result = self.request('POST', '/api/tagger/models/install', {'manifest_id': self.manifest.digest}, status=503)
        self.assertEqual(result['error']['code'], 'tagger:runtime_missing')
        self.assertEqual(self.download.calls, [])

    def test_project_close_does_not_cancel_product_installation(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def download(manifest, file, cancel):
            entered.set()
            while not release.wait(.01):
                if cancel.is_set():
                    return
            yield self.content[file.name]
        self.store.download = download
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        project = self.request('GET', self.p)
        self.wait_operation(self.request('POST', self.p+'/close', {}, version=project['version'], status=202))
        self.assertIsNone(self.runtime.context)
        self.assertEqual(self.request('GET', accepted['status_url'])['status'], 'running')
        release.set()
        self.wait_operation(accepted)
        self.assertEqual(self.models()[0]['state'], 'ready')

    def test_paths_urls_unknown_fields_queries_and_unknown_sessions_rejected(self):
        for body in [{'manifest_id': '../escape'}, {'manifest_id': self.manifest.digest, 'url': 'https://example.com'},
                     {'manifest_id': self.manifest.digest, 'directory': str(self.base)}]:
            self.request('POST', '/api/tagger/models/install', body, status=422)
        self.request('GET', '/api/tagger/capabilities?url=x', status=422)
        self.request('GET', '/api/tagger/models?limit=1001', status=422)
        self.request('GET', '/api/tagger/installations/'+str(uuid4()), status=410)
        result = self.client.get('/api/tagger/models', headers={'Authorization': 'Bearer invalid'})
        self.assertEqual(result.status_code, 401)

    def test_catalog_pagination_and_cursor_scope(self):
        second, _ = fixture(revision='b'*40)
        self.runtime.tagger.manifests[second.digest] = second
        first = self.request('GET', '/api/tagger/models?limit=1')
        self.assertEqual(len(first['items']), 1)
        self.assertTrue(first['has_more'])
        end = self.request('GET', '/api/tagger/models?limit=1&cursor='+first['next_cursor'])
        self.assertFalse(end['has_more'])
        self.assertNotEqual(first['items'][0]['manifest_id'], end['items'][0]['manifest_id'])
        self.request('GET', '/api/operations?cursor='+first['next_cursor'], status=422)
