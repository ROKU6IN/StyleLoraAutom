import json
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.errors import Busy, CommitUnknown
from stylelora.project_db.repositories import jobs
from stylelora.runtime.errors import ServiceError
from stylelora.services.tagger import TaggerService
from stylelora.tagging.model_runtime import runtime_status
from stylelora.tagging.models import ModelStore
from stylelora.tagging.predictor import OrtTagger
from . import test_api_vision as vision_support
from .tagger_support import inference_fixture, MemoryDownload, DEVICE


@unittest.skipUnless(runtime_status()['available'], 'optional wd-cpu runtime not installed')
class BatchAPIBase(vision_support.VisionAPIBase):
    def setUp(self):
        super().setUp()
        self.manifest, content = inference_fixture()
        store = ModelStore(self.base/'models', download=MemoryDownload(content))
        store.install(self.manifest, threading.Event())
        self.runtime.tagger = TaggerService(self.runtime, manifests=(self.manifest,), store=store, device_probe=lambda: DEVICE)
        self.wd_config = {'manifest_id': self.manifest.digest, 'general_threshold': 0., 'character_threshold': 0.}
        self.wd_calls = []
        calls = self.wd_calls
        class Counted(OrtTagger):
            def predict_one(inner, content, cancel):
                calls.append(content)
                return super().predict_one(content, cancel)
        self.counted = Counted
        self.runtime.jobs.engine_factory = Counted

    def body(self, *, mode='uncovered', ids=None, config=None, executors=None):
        return {'executors': executors or ['wd','llm_vision'], 'mode': mode,
            'scope': {'selected_ids': ids} if ids is not None else {'filter': {}},
            'config': {'wd': self.wd_config, 'llm_vision': config or {}}}

    def start_batch(self, *, batch=None, **options):
        return self.request('POST',self.p+'/jobs/batches',{**self.body(**options),'batch_id':batch or str(uuid4())},status=202)

    def preview(self, **options):
        return self.request('POST',self.p+'/jobs/preview',self.body(**options))

    def batch_control(self, accepted, action, status=200):
        current = self.request('GET',accepted['batch_url'])
        return self.request('POST',accepted['batch_url']+'/control',{'action':action},version=current['version'],status=status)

    def batch_wait(self, accepted, status='done'):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            row = self.request('GET',accepted['batch_url'])
            if row['status'] == status:
                return row
            time.sleep(.01)
        self.fail(str(row))

    def wd_idle(self, job_id):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            with self.runtime.jobs.lock:
                if (self.pid,job_id) not in self.runtime.jobs.active:
                    return
            time.sleep(.01)
        self.fail('WD runner still active')

    def wd_gate(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        parent = self.counted
        class Gated(parent):
            def predict_one(inner, content, cancel):
                result = super().predict_one(content,cancel)
                entered.set()
                release.wait(10)
                return result
        self.runtime.jobs.engine_factory = Gated
        return entered, release

    def reopen(self, accepted):
        project = self.request('GET',self.p)
        self.wait_operation(self.request('POST',self.p+'/close',{},version=project['version'],status=202))
        grant = self.runtime.grant(self.root,'project_open')
        opened = self.wait_operation(self.request('POST','/api/projects/open',{'grant_id':grant['grant_id']},status=202))['result']
        self.pid,self.p = opened['project_id'],'/api/projects/'+opened['project_id']
        accepted['batch_url'] = self.p+'/jobs/batches/'+accepted['batch_id']

class BatchAPITests(BatchAPIBase):
    def test_serial_order_latest_wd_evidence_and_complete_http_export(self):
        ids = [v['image_id'] for v in self.import_inputs(2)['items']]
        entered, release = self.wd_gate()
        accepted = self.start_batch(executors=['llm_vision','wd'])
        self.assertTrue(entered.wait(5))
        row = self.request('GET',accepted['batch_url'])
        self.assertEqual([r['executor'] for r in row['items']],['wd','llm_vision'])
        self.assertEqual([r['status'] for r in row['items']],['running','pending'])
        self.assertEqual([r['total'] for r in row['items']],[2,2])
        self.assertEqual(self.calls,[])
        release.set()
        row = self.batch_wait(accepted)
        self.assertEqual((row['total'],row['done_count']),(4,4))
        tags = {tag.tag for tag in self.current(ids[0],layer='wd').content}
        for call in self.calls:
            payload = json.loads(call['messages'][1]['content'][0]['text'])
            self.assertTrue(tags.issubset(set(payload['tags'])))
        self.keep(ids)
        exported = self.run_export(self.export_preview(include_nl=True))
        report = self.request('GET',exported['report_url'])
        self.assertIn('A red cup',next((self.root/report['directory']).glob('*.txt')).read_text(encoding='utf-8'))

    def test_preview_no_side_effects_then_both_stages_reuse_current_cache(self):
        self.import_inputs(2)
        before = self.request('GET',self.p+'/images?limit=20')
        preview = self.preview(config={'use_rating_model': True})
        self.assertEqual(preview['items'][0]['needs_execution'],2)
        self.assertEqual(preview['items'][1]['waiting_for_wd'],2)
        self.assertEqual(self.request('GET',self.p+'/jobs')['items'],[])
        self.assertEqual(self.request('GET',self.p+'/images?limit=20'),before)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.wd_calls,[])
        self.batch_wait(self.start_batch())
        preview = self.preview()
        self.assertEqual([v['cache_hit'] for v in preview['items']],[2,2])
        row = self.batch_wait(self.start_batch())
        self.assertEqual((row['done_count'],row['skipped_count']),(0,4))
        self.assertEqual((len(self.wd_calls),len(self.calls)),(2,2))
        forced = self.preview(mode='all')
        self.assertEqual((forced['items'][0]['needs_execution'],forced['items'][1]['waiting_for_wd']),(2,2))

    def test_frozen_both_ranges_survive_import_and_review_change(self):
        ids = [v['image_id'] for v in self.import_inputs(2)['items']]
        entered,release = self.wd_gate()
        accepted = self.start_batch(mode='all')
        self.assertTrue(entered.wait(5))
        self.import_inputs(3)
        value = self.request('GET',self.p+f'/images/{ids[0]}')
        self.request('POST',self.p+'/images/review',{'ids':[ids[0]],'target':'reject','expected_versions':{str(ids[0]):value['version']}})
        release.set()
        row = self.batch_wait(accepted)
        self.assertEqual([r['total'] for r in row['items']],[2,2])
        self.assertEqual(len(self.calls),2)

    def test_batch_pause_tail_completion_holds_pending_until_explicit_resume(self):
        self.import_inputs()
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        self.batch_control(accepted,'pause')
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        row = self.batch_wait(accepted,'paused')
        self.assertEqual([r['status'] for r in row['items']],['done','pending'])
        self.assertEqual(self.calls,[])
        self.batch_control(accepted,'resume')
        self.assertEqual(self.batch_wait(accepted)['done_count'],2)

    def test_pause_partial_resume_and_pending_start_cannot_bypass_predecessor(self):
        self.import_inputs(3)
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        second = self.request('GET',accepted['items'][1]['status_url'])
        self.request('POST',second['status_url']+'/control',{'action':'resume'},version=second['version'],status=409)
        overlap = self.preview()['overlapping_jobs']
        self.assertEqual({v['executor'] for v in overlap},{'wd','llm_vision'})
        self.batch_control(accepted,'pause')
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        row = self.batch_wait(accepted,'paused')
        self.assertEqual(row['items'][0]['done_count'],1)
        self.assertEqual(self.calls,[])
        self.batch_control(accepted,'resume')
        self.assertEqual(self.batch_wait(accepted)['done_count'],6)

    def test_cancel_during_wd_cancels_pending_and_discards_late_result(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        row = self.batch_control(accepted,'cancel')
        self.assertEqual([r['status'] for r in row['items']],['canceled','canceled'])
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        self.assertIsNone(self.current(image_id,layer='wd'))
        self.assertEqual(self.calls,[])
        self.batch_control(accepted,'resume',status=409)

    def test_cancel_during_vision_preserves_committed_wd(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        entered,release = self.gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        self.batch_control(accepted,'cancel')
        release.set()
        self.idle(accepted['items'][1]['job_id'])
        self.assertIsNotNone(self.current(image_id,layer='wd'))
        self.assertIsNone(self.current(image_id))
        self.assertEqual(self.batch_wait(accepted,'canceled')['done_count'],1)

    def test_canceled_predecessor_blocks_pending_without_skipping_dependency(self):
        self.import_inputs()
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        self.control({'status_url':accepted['items'][0]['status_url']},'cancel')
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        self.assertEqual(self.batch_wait(accepted,'blocked')['items'][1]['status'],'pending')
        self.assertEqual(self.batch_control(accepted,'resume',status=409)['error']['code'],'job:dependency_blocked')
        self.assertEqual(self.calls,[])
        self.batch_control(accepted,'cancel')

    def test_reopen_pending_snapshot_holds_without_model_calls_and_validates_roles(self):
        self.import_inputs()
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        self.batch_control(accepted,'pause')
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        original = self.request('GET',accepted['batch_url'])['items'][1]['config_snapshot_json']
        self.reopen(accepted)
        row = self.batch_wait(accepted,'paused')
        self.assertEqual(row['items'][1]['config_snapshot_json'],original)
        self.assertEqual(self.calls,[])
        roles = self.request('GET','/api/llm/roles')
        self.request('PUT','/api/llm/roles',{'roles':{**roles['roles'],'vision':{'endpoint_id':self.eid,'model':'nsfw-model'}}},version=roles['version'])
        self.batch_control(accepted,'resume',status=503)
        self.assertEqual(self.request('GET',accepted['batch_url'])['items'][1]['status'],'pending')
        self.bind()
        self.batch_control(accepted,'resume')
        self.assertEqual(self.batch_wait(accepted)['done_count'],2)

    def test_auto_handoff_changed_config_stays_pending_with_diagnosis(self):
        self.import_inputs()
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        roles = self.request('GET','/api/llm/roles')
        self.request('PUT','/api/llm/roles',{'roles':{}},version=roles['version'])
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        row = self.batch_wait(accepted,'pending')
        self.assertEqual(row['items'][1]['error'],'vision:role_unconfigured')
        self.assertEqual(self.calls,[])
        self.bind()
        self.batch_control(accepted,'resume')
        self.assertEqual(self.batch_wait(accepted)['done_count'],2)

    def test_create_second_snapshot_failure_rolls_back_entire_batch(self):
        self.import_inputs()
        original = jobs.freeze_job
        def broken(db, job_id, frozen, **kwargs):
            if jobs.get_job(db,job_id).executor == 'llm_vision':
                raise Busy('fixture')
            return original(db,job_id,frozen,**kwargs)
        with patch.object(jobs,'freeze_job',side_effect=broken):
            self.request('POST',self.p+'/jobs/batches',{**self.body(),'batch_id':str(uuid4())},status=503)
        self.assertEqual(self.request('GET',self.p+'/jobs')['items'],[])
        self.assertEqual(self.wd_calls,[])

    def test_unknown_create_reconciles_and_replay_is_read_only_even_without_roles(self):
        self.import_inputs()
        original, seen = commands.transition_job, []
        def unknown(db,*args,**kwargs):
            result = original(db,*args,**kwargs)
            if not seen:
                seen.append(True)
                def fault():
                    raise CommitUnknown('fixture committed')
                db.after_commit.append(fault)
            return result
        batch = str(uuid4())
        with patch.object(commands,'transition_job',side_effect=unknown):
            accepted = self.start_batch(batch=batch)
            self.batch_wait(accepted)
        roles = self.request('GET','/api/llm/roles')
        self.request('PUT','/api/llm/roles',{'roles':{}},version=roles['version'])
        repeated = self.start_batch(batch=batch)
        self.assertEqual([r['job_id'] for r in repeated['items']],[r['job_id'] for r in accepted['items']])
        self.assertEqual((len(self.wd_calls),len(self.calls)),(1,1))
        self.request('POST',self.p+'/jobs/batches',{**self.body(mode='all'),'batch_id':batch},status=409)

    def test_atomic_pause_failure_and_stale_batch_version(self):
        self.import_inputs(2)
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        old = self.request('GET',accepted['batch_url'])
        with patch.object(jobs,'wait_reason',side_effect=Busy('fixture')):
            self.batch_control(accepted,'pause',status=503)
        current = self.request('GET',accepted['batch_url'])
        self.assertEqual([r['status'] for r in current['items']],['running','pending'])
        self.batch_control(accepted,'pause')
        self.request('POST',accepted['batch_url']+'/control',{'action':'resume'},version=old['version'],status=409)
        self.batch_control(accepted,'cancel')
        release.set()

    def test_preview_single_vision_keyword_routing_and_invalid_contracts(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        self.policy([{'tag':'smile','rating':'questionable'}])
        self.bind(nsfw=False)
        body = {**self.body(), 'executors':['llm_vision'], 'config':{}}
        row = self.request('POST',self.p+'/jobs/preview',body)
        self.assertEqual(row['items'][0]['role_empty'],1)
        self.assertIsNone(self.request('GET',self.p+f'/images/{image_id}')['rating'])
        self.assertEqual(self.calls,[])
        for changes in ({'executors':['wd','wd']},{'config':self.wd_config},{'executors':['llm_vision']},
                        {'mode':'selected','scope':{'selected_ids':[999]}}):
            self.request('POST',self.p+'/jobs/batches',{**self.body(),**changes,'batch_id':str(uuid4())},status=422)
        self.assertEqual(self.request('GET',self.p+'/jobs')['items'],[])

    def test_batch_pagination_and_per_image_wd_failure_allows_downstream(self):
        self.import_inputs()
        class Failed(self.counted):
            def predict_one(inner,*args):
                raise ServiceError(503,'tagger:inference_failed')
        self.runtime.jobs.engine_factory = Failed
        first = self.start_batch()
        row = self.batch_wait(first)
        self.assertEqual((row['failed_count'],row['done_count']),(1,1))
        self.assertEqual(row['items'][0]['status'],'done')
        self.runtime.jobs.engine_factory = self.counted
        second = self.start_batch()
        self.batch_wait(second)
        page = self.request('GET',self.p+'/jobs/batches?limit=1')
        self.assertTrue(page['has_more'])
        next_page = self.request('GET',self.p+'/jobs/batches?limit=1&cursor='+page['next_cursor'])
        self.assertNotEqual(page['items'][0]['batch_id'],next_page['items'][0]['batch_id'])

    def test_preview_scope_digest_rejects_membership_change_without_creating_jobs(self):
        self.import_inputs()
        preview = self.preview()
        self.import_inputs(2)
        body = {**self.body(),'batch_id':str(uuid4()),'preview_scope_digest':preview['scope_digest']}
        error = self.request('POST',self.p+'/jobs/batches',body,status=409)
        self.assertEqual(error['error']['code'],'job:preview_stale')
        self.assertEqual(self.request('GET',self.p+'/jobs')['items'],[])
        body['preview_scope_digest'] = self.preview()['scope_digest']
        accepted = self.request('POST',self.p+'/jobs/batches',body,status=202)
        self.assertEqual(self.batch_wait(accepted)['done_count'],4)

    def test_empty_scope_finishes_both_stages_without_calls(self):
        preview = self.preview()
        self.assertEqual(preview['image_count'],0)
        row = self.batch_wait(self.start_batch())
        self.assertEqual((row['total'],row['done_count']),(0,0))
        self.assertEqual([r['status'] for r in row['items']],['done','done'])
        self.assertEqual(self.calls,[])

    def test_unknown_batch_pause_commit_reconciles_without_handoff(self):
        self.import_inputs()
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        original = jobs.wait_reason
        def unknown(db,*args):
            value = original(db,*args)
            def fault():
                raise CommitUnknown('fixture committed')
            db.after_commit.append(fault)
            return value
        with patch.object(jobs,'wait_reason',side_effect=unknown):
            self.batch_control(accepted,'pause')
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        self.assertEqual(self.batch_wait(accepted,'paused')['items'][1]['status'],'pending')
        self.assertEqual(self.calls,[])

    def test_close_while_wd_running_leaves_later_stage_pending(self):
        self.import_inputs(3)
        entered,release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        project = self.request('GET',self.p)
        closing = self.request('POST',self.p+'/close',{},version=project['version'],status=202)
        release.set()
        self.wait_operation(closing)
        grant = self.runtime.grant(self.root,'project_open')
        reopened = self.wait_operation(self.request('POST','/api/projects/open',{'grant_id':grant['grant_id']},status=202))['result']
        self.pid,self.p = reopened['project_id'],'/api/projects/'+reopened['project_id']
        accepted['batch_url'] = self.p+'/jobs/batches/'+accepted['batch_id']
        row = self.request('GET',accepted['batch_url'])
        self.assertEqual([r['status'] for r in row['items']],['paused','pending'])
        self.assertEqual(self.calls,[])
        self.batch_control(accepted,'resume')
        self.assertEqual(self.batch_wait(accepted)['done_count'],6)
