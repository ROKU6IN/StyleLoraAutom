import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.errors import Busy, CommitUnknown
from stylelora.project_db.repositories import annotations, images
from stylelora.project_db.repositories import jobs as job_repository
from stylelora.project_db.types import AnnotationInput, JobRequest, JobSnapshot, JobOutcome, Scope, Tag
from stylelora.services.tagger import TaggerService
from stylelora.tagging.model_runtime import runtime_status
from stylelora.tagging.models import ModelStore
from stylelora.tagging.predictor import OrtTagger
from .api_support import APITest
from .tagger_support import inference_fixture, MemoryDownload, DEVICE


@unittest.skipUnless(runtime_status()['available'], 'optional wd-cpu runtime not installed')
class JobsAPITest(APITest):
    def setUp(self):
        super().setUp()
        self.manifest, content = inference_fixture()
        store = ModelStore(self.base/'models', download=MemoryDownload(content))
        store.install(self.manifest, threading.Event())
        self.runtime.tagger = TaggerService(self.runtime, manifests=(self.manifest,), store=store, device_probe=lambda: DEVICE)
        self.config = {'manifest_id': self.manifest.digest, 'general_threshold': 0., 'character_threshold': 0.}
        self.calls = []
        calls = self.calls
        class Counted(OrtTagger):
            def predict_one(self, content, cancel):
                calls.append(content)
                return super().predict_one(content, cancel)
        self.counted = Counted
        self.runtime.jobs.engine_factory = Counted

    def start(self, *, mode='uncovered', ids=None, batch=None, config=None, query=None):
        body = {'batch_id': batch or str(uuid4()), 'executors': ['wd'], 'mode': mode,
                'scope': {'selected_ids': ids} if ids is not None else {'filter': query or {}},
                'config': config or self.config}
        return self.request('POST', self.p+'/jobs/batches', body, status=202)

    def wait(self, accepted, status='done'):
        deadline = time.monotonic()+12
        while time.monotonic() < deadline:
            row = self.request('GET', accepted['status_url'])
            if row['status'] == status:
                return row
            if row['status'] in ('failed', 'canceled', 'paused') and row['status'] != status:
                self.fail(str(row))
            time.sleep(.01)
        self.fail('Job did not reach '+status)

    def idle(self, job_id):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            with self.runtime.jobs.lock:
                if (self.pid, job_id) not in self.runtime.jobs.active:
                    return
            time.sleep(.01)
        self.fail('Runner did not stop')

    def control(self, accepted, action):
        row = self.request('GET', accepted['status_url'])
        return self.request('POST', accepted['status_url']+'/control', {'action': action}, version=row['version'])

    def current(self, image_id):
        with read_snapshot(self.runtime.context.handle) as db:
            return annotations.get_current(db, image_id, 'wd', 'tags')

    def gate(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        base = self.counted
        class Gated(base):
            def predict_one(inner, content, cancel):
                result = super().predict_one(content, cancel)
                if len(self.calls) == 1:
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('Gate timed out')
                return result
        self.runtime.jobs.engine_factory = Gated
        return entered, release

    def test_http_import_infer_current_cache_force_all_and_export(self):
        ids = [i['image_id'] for i in self.import_inputs(2)['items']]
        event_cursor = f'{self.runtime.events.epoch}:{self.runtime.events.sequence}'
        first = self.wait(self.start())
        self.idle(first['job_id'])
        sub, frames = self.runtime.events.subscribe(after=event_cursor, topics=('job',), project_id=self.pid, resource_id=str(first['job_id']))
        self.runtime.events.unsubscribe(sub)
        self.assertEqual([frame['type'] for frame in frames], ['job.started', 'job.image_completed', 'job.image_completed', 'job.done', 'stream.ready'])
        self.assertEqual(frames[1]['data']['payload']['kind'], 'done')
        self.assertIsNone(frames[1]['data']['payload']['reason'])
        self.assertEqual((first['done_count'], first['total']), (2, 2))
        self.assertNotIn('progress_json', first)
        for image_id in ids:
            value = self.current(image_id)
            self.assertEqual([t.tag for t in value.content], ['blue hair', 'fixture character'])
            self.assertEqual(value.params_json['actual']['provider'], 'cpu')
            self.assertIsNone(self.request('GET', self.p+f'/images/{image_id}')['rating'])
        old = self.current(ids[0]).annotation_id
        cached = self.wait(self.start())
        self.assertEqual((cached['done_count'], cached['skipped_count']), (0, 2))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.current(ids[0]).annotation_id, old)
        all_job = self.wait(self.start(mode='all'))
        self.assertEqual(all_job['done_count'], 2)
        self.assertNotEqual(self.current(ids[0]).annotation_id, old)
        self.assertEqual(len(self.calls), 4)
        self.keep(ids)
        preview = self.export_preview()
        exported = self.run_export(preview)
        report = self.request('GET', exported['report_url'])
        caption = next((self.root/report['directory']).glob('*.txt')).read_text(encoding='utf-8')
        self.assertIn('fixture character', caption)
        self.assertNotIn('general', caption)

    def test_batch_replay_conflict_and_current_only_coverage(self):
        self.import_inputs()
        batch = str(uuid4())
        first = self.start(batch=batch)
        self.wait(first)
        self.assertEqual(self.start(batch=batch)['task'], first['task'])
        changed = {**self.config, 'general_threshold': .8}
        self.request('POST', self.p+'/jobs/batches', {'batch_id': batch, 'executors': ['wd'], 'mode': 'uncovered',
                     'scope': {'filter': {}}, 'config': changed}, status=409)
        self.wait(self.start(config=changed))
        self.wait(self.start())
        self.assertEqual(len(self.calls), 3)
        rows = self.request('GET', self.p+'/jobs?executor=wd&limit=1')
        self.assertTrue(rows['has_more'])
        self.assertEqual(len(self.request('GET', self.p+'/jobs?executor=wd&limit=1&cursor='+rows['next_cursor'])['items']), 1)
        self.request('GET', self.p+'/jobs?executor=llm_vision&cursor='+rows['next_cursor'], status=422)

    def test_missing_changed_and_rejected_scope_and_selected_dedup(self):
        ids = [i['image_id'] for i in self.import_inputs(3)['items']]
        values = self.request('GET', self.p+'/images')['items']
        self.request('POST', self.p+'/images/review', {'ids': [ids[0]], 'target': 'reject', 'expected_versions': {str(ids[0]): values[0]['version']}})
        (self.root/'images'/values[1]['filename']).unlink()
        (self.root/'images'/values[2]['filename']).write_bytes(b'changed')
        row = self.wait(self.start())
        self.assertEqual((row['total'], row['done_count'], row['failed_count'], row['skipped_count']), (2, 0, 1, 1))
        self.assertEqual(len(self.calls), 0)
        selected = self.wait(self.start(mode='selected', ids=[ids[0], ids[0]]))
        self.assertEqual((selected['total'], selected['done_count']), (1, 1))

    def test_pause_allows_inflight_commit_and_resume_does_not_repeat(self):
        self.import_inputs(3)
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'pause')
        release.set()
        self.idle(accepted['task']['id'])
        paused = self.request('GET', accepted['status_url'])
        self.assertEqual((paused['status'], paused['done_count']), ('paused', 1))
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 3)
        self.assertEqual(len(self.calls), 3)

    def test_cancel_discards_late_result_and_terminal_resume_rejected(self):
        ids = [i['image_id'] for i in self.import_inputs(2)['items']]
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'cancel')
        release.set()
        self.idle(accepted['task']['id'])
        row = self.wait(accepted, 'canceled')
        self.assertEqual(row['done_count'], 0)
        self.assertIsNone(self.current(ids[0]))
        self.request('POST', accepted['status_url']+'/control', {'action': 'resume'}, version=row['version'], status=409)
        self.assertEqual(self.control(accepted, 'cancel')['status'], 'canceled')

    def test_paused_last_inflight_completes_job(self):
        self.import_inputs()
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'pause')
        release.set()
        self.idle(accepted['task']['id'])
        self.assertEqual(self.request('GET', accepted['status_url'])['status'], 'done')

    def test_file_mutated_during_inference_does_not_write_success(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        row = self.request('GET', self.p+f'/images/{image_id}')
        (self.root/'images'/row['filename']).write_bytes(b'changed during inference')
        release.set()
        job = self.wait(accepted)
        self.assertEqual(job['failed_count'], 1)
        self.assertIsNone(self.current(image_id))

    def test_frozen_scope_survives_import_and_review_changes(self):
        ids = [i['image_id'] for i in self.import_inputs(2)['items']]
        entered, release = self.gate()
        accepted = self.start(mode='all')
        self.assertTrue(entered.wait(5))
        self.import_inputs(3)
        row = self.request('GET', self.p+f'/images/{ids[1]}')
        self.request('POST', self.p+'/images/review', {'ids': [ids[1]], 'target': 'reject', 'expected_versions': {str(ids[1]): row['version']}})
        release.set()
        job = self.wait(accepted)
        self.assertEqual((job['total'], job['done_count']), (2, 2))
        self.assertEqual(len(self.calls), 2)

    def test_commit_unknown_reconciles_without_another_inference(self):
        self.import_inputs(2)
        original = commands.commit_job_outcome
        injected = []
        def commit(db, job_id, outcome):
            value = original(db, job_id, outcome)
            if not injected:
                injected.append(True)
                def unknown():
                    raise CommitUnknown('fixture after commit')
                db.after_commit.append(unknown)
            return value
        with patch('stylelora.services.jobs.commands.commit_job_outcome', side_effect=commit):
            job = self.wait(self.start())
        self.assertEqual(job['done_count'], 2)
        self.assertEqual(len(self.calls), 2)

    def test_busy_pause_retains_result_for_resume_without_inference_replay(self):
        self.import_inputs(2)
        with patch('stylelora.services.jobs.commands.commit_job_outcome', side_effect=Busy('fixture busy')):
            accepted = self.start()
            self.wait(accepted, 'paused')
            self.idle(accepted['task']['id'])
        self.assertEqual(len(self.calls), 1)
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 2)
        self.assertEqual(len(self.calls), 2)

    def test_close_reopen_pauses_and_does_not_autorun_then_resume_frozen_job(self):
        self.import_inputs(2)
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        project = self.request('GET', self.p)
        close = self.request('POST', self.p+'/close', {}, version=project['version'], status=202)
        release.set()
        self.wait_operation(close)
        grant = self.runtime.grant(self.root, 'project_open')
        opened = self.wait_operation(self.request('POST', '/api/projects/open', {'grant_id': grant['grant_id']}, status=202))['result']
        self.pid, self.p = opened['project_id'], '/api/projects/'+opened['project_id']
        accepted['status_url'] = self.p+'/jobs/'+str(accepted['task']['id'])
        paused = self.request('GET', accepted['status_url'])
        self.assertEqual(paused['status'], 'paused')
        calls = len(self.calls)
        time.sleep(.05)
        self.assertEqual(len(self.calls), calls)
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 2)

    def test_resume_missing_manifest_or_changed_runtime_preserves_paused(self):
        self.import_inputs(2)
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'pause')
        release.set()
        self.idle(accepted['task']['id'])
        old = self.runtime.tagger.manifests.pop(self.manifest.digest)
        row = self.request('GET', accepted['status_url'])
        self.request('POST', accepted['status_url']+'/control', {'action': 'resume'}, version=row['version'], status=503)
        self.assertEqual(self.request('GET', accepted['status_url'])['status'], 'paused')
        self.runtime.tagger.manifests[self.manifest.digest] = old
        self.runtime.tagger.status = lambda: {'available': True, 'reason': None, 'version': 'changed'}
        self.request('POST', accepted['status_url']+'/control', {'action': 'resume'}, version=row['version'], status=503)

    def test_outcomes_paging_control_cas_and_bad_inputs(self):
        self.import_inputs(3)
        accepted = self.start()
        self.wait(accepted)
        page = self.request('GET', accepted['status_url']+'/outcomes?limit=1&kind=done')
        self.assertEqual(len(page['items']), 1)
        self.assertTrue(page['has_more'])
        second = self.request('GET', accepted['status_url']+'/outcomes?limit=1&kind=done&cursor='+page['next_cursor'])
        self.assertNotEqual(second['items'][0]['image_id'], page['items'][0]['image_id'])
        self.request('GET', accepted['status_url']+'/outcomes?kind=failed&cursor='+page['next_cursor'], status=422)
        self.request('POST', accepted['status_url']+'/control', {'action': 'cancel'}, status=428)
        self.request('POST', accepted['status_url']+'/control', {'action': 'cancel'}, version='stale', status=409)
        body = {'batch_id': str(uuid4()), 'executors': ['wd'], 'mode': 'selected', 'scope': {'selected_ids': [999]}, 'config': self.config}
        self.request('POST', self.p+'/jobs/batches', body, status=422)
        for change in [{'executors': ['llm_vision']}, {'config': {**self.config, 'general_threshold': True}},
                       {'config': {**self.config, 'character_threshold': 2.}}, {'scope': {'filter': {}, 'selected_ids': [1]}},
                       {'config': {**self.config, 'path': str(self.base)}}]:
            self.request('POST', self.p+'/jobs/batches', {**body, **change}, status=422)
        self.assertNotIn(str(self.base), str(self.request('GET', self.p+'/jobs')))

    def test_scope_capacity_rejected_before_persistent_job_creation(self):
        self.import_inputs()
        def fill(db):
            row = dict(db.execute('SELECT * FROM images LIMIT 1').fetchone())
            db.executemany('INSERT INTO images(filename,width,height,phash,sha256,file_size,source_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                [(f'{i}.png', row['width'], row['height'], row['phash'], row['sha256'], row['file_size'], row['source_id'], row['created_at']) for i in range(2, 5002)])
        submit_write(self.runtime.context.handle, fill).result()
        self.request('POST', self.p+'/jobs/batches', {'batch_id': str(uuid4()), 'executors': ['wd'], 'mode': 'all',
            'scope': {'filter': {}}, 'config': self.config}, status=413)
        self.assertEqual(self.request('GET', self.p+'/jobs')['items'], [])
        self.assertEqual(self.calls, [])

    def test_manual_edit_survives_inflight_wd_and_older_wd_is_cas_protected(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        current = self.request('GET', self.p+f'/annotations/{image_id}')
        self.request('PUT', self.p+f'/annotations/{image_id}/manual', {'tags': [{'tag': 'manual keep'}], 'read_set': current['refs']}, version=current['version'])
        def external_result(db):
            other = job_repository.create_batch(db, str(uuid4()), (JobRequest('wd', 'selected', Scope((image_id,))),))[0]
            job_repository.freeze_job(db, other.job_id, JobSnapshot('other'), image_ids=(image_id,))
            return other
        other = submit_write(self.runtime.context.handle, external_result).result()
        def finish_external(db):
            value = AnnotationInput(image_id, 'wd', 'tags', (Tag('other result', .8),), model_name='other',
                params={'schema_version': 1, 'requested': {}, 'actual': {}}, job_id=other.job_id, cache_key='external')
            commands.commit_job_outcome(db, other.job_id, JobOutcome(image_id, 'done', (value,)))
            commands.transition_job(db, other.job_id, expected='running', target='done')
        submit_write(self.runtime.context.handle, finish_external).result()
        release.set()
        self.assertEqual(self.wait(accepted)['failed_count'], 1)
        self.assertEqual(self.current(image_id).content[0].tag, 'other result')
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertEqual(annotations.get_current(db, image_id, 'manual', 'tags').content[0].tag, 'manual keep')

    def test_resume_during_previous_worker_cleanup_is_not_lost(self):
        self.import_inputs(2)
        entered, release = self.gate()
        base = self.runtime.jobs.engine_factory
        closing, release_close = threading.Event(), threading.Event()
        self.addCleanup(release_close.set)
        class Closing(base):
            def close(inner):
                super().close()
                if not closing.is_set():
                    closing.set()
                    release_close.wait(5)
        self.runtime.jobs.engine_factory = Closing
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'pause')
        release.set()
        self.assertTrue(closing.wait(5))
        self.control(accepted, 'resume')
        release_close.set()
        self.assertEqual(self.wait(accepted)['done_count'], 2)
        self.assertEqual(len(self.calls), 2)

    def test_cleanup_failure_releases_worker_for_next_job(self):
        self.import_inputs()
        base = self.counted
        class BrokenClose(base):
            def close(inner):
                super().close()
                raise RuntimeError('fixture cleanup failure')
        self.runtime.jobs.engine_factory = BrokenClose
        first = self.start()
        self.assertEqual(self.wait(first)['done_count'], 1)
        self.idle(first['task']['id'])
        self.runtime.jobs.engine_factory = base
        self.assertEqual(self.wait(self.start(mode='all'))['done_count'], 1)
        self.assertEqual(len(self.calls), 2)


    def batch_engine(self, *, gated=False):
        from .tagger_support import dynamic_fixture, GPU_DEVICE, gpu_profile
        from stylelora.tagging.model_runtime import create_session
        self.manifest, content = dynamic_fixture()
        store = ModelStore(self.base/'batch-models', download=MemoryDownload(content))
        store.install(self.manifest, threading.Event())
        self.runtime.tagger = TaggerService(self.runtime, manifests=(self.manifest,), store=store, device_probe=lambda: GPU_DEVICE)
        self.config = {'manifest_id': self.manifest.digest, 'general_threshold': 0., 'character_threshold': 0.,
                       'provider': 'auto', 'batch_size': 4}
        entered, release, calls = threading.Event(), threading.Event(), []
        self.addCleanup(release.set)
        def factory(manifest, path, provider):
            return create_session(manifest, path, 'cpu')  # Controlled CUDA scheduler, actual dynamic ONNX graph.
        class Batched(OrtTagger):
            def __init__(inner, store, manifest, config, cancel, **kwargs):
                super().__init__(store, manifest, config, cancel, **kwargs,
                                 session_factory=factory, profile=gpu_profile(manifest))
            def predict_many(inner, contents, cancel):
                calls.append(len(contents))
                result = super().predict_many(contents, cancel)
                if gated and len(calls) == 1:
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('Batch gate timed out')
                return result
        self.runtime.jobs.engine_factory = Batched
        return calls, entered, release

    def test_dynamic_batch_pause_commits_four_resume_keeps_order_and_tail_two(self):
        self.import_inputs(6)
        calls, entered, release = self.batch_engine(gated=True)
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'pause')
        release.set()
        self.idle(accepted['task']['id'])
        self.assertEqual(self.wait(accepted, 'paused')['done_count'], 4)
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 6)
        self.assertEqual(calls, [4, 2])
        rows = self.request('GET', self.p+'/images?limit=100')['items']
        self.assertEqual([self.current(r['image_id']).params_json['actual']['batch_size'] for r in rows], [4]*4+[2]*2)

    def test_dynamic_batch_cancel_discards_all_four_and_leaves_tail_unstarted(self):
        ids = [r['image_id'] for r in self.import_inputs(6)['items']]
        calls, entered, release = self.batch_engine(gated=True)
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'cancel')
        release.set()
        self.idle(accepted['task']['id'])
        self.assertEqual(self.wait(accepted, 'canceled')['done_count'], 0)
        self.assertEqual(calls, [4])
        self.assertTrue(all(self.current(i) is None for i in ids))

    def test_dynamic_batch_busy_retains_every_uncommitted_result(self):
        self.import_inputs(6)
        calls, _, _ = self.batch_engine()
        original = commands.commit_job_outcome
        def commit(db, job_id, outcome):
            if outcome.image_id > 1:
                raise Busy('fixture second image busy')
            return original(db, job_id, outcome)
        with patch('stylelora.services.jobs.commands.commit_job_outcome', side_effect=commit):
            accepted = self.start()
            self.wait(accepted, 'paused')
            self.idle(accepted['task']['id'])
        self.assertEqual(calls, [4])
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 6)
        self.assertEqual(calls, [4, 2])

    def test_dynamic_batch_changed_image_fails_independently_and_current_cache_skips(self):
        rows = self.import_inputs(4)['items']
        calls, entered, release = self.batch_engine(gated=True)
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        with read_snapshot(self.runtime.context.handle) as db:
            row = images.get_image(db, rows[1]['image_id'])
        (self.runtime.context.handle.root/'images'/row.filename).write_bytes(b'changed')
        release.set()
        complete = self.wait(accepted)
        self.assertEqual((complete['done_count'], complete['failed_count']), (3, 1))
        cached = self.wait(self.start())
        self.assertEqual((cached['skipped_count'], cached['failed_count']), (3, 1))
        self.assertEqual(calls, [4])
