import asyncio
from dataclasses import replace
import json
import threading
import time
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.errors import Busy, CommitUnknown, ValidationError
from stylelora.project_db.repositories import annotations, images
from stylelora.project_db.validation import primitive
from stylelora.vision.policy import PolicyStore
from stylelora.project_db import commands
from stylelora.project_db.repositories import jobs
from stylelora.project_db.types import AnnotationInput, JobRequest, JobSnapshot, JobOutcome, Scope, Tag
from .api_support import APITest
from .test_llm import reply


class VisionAPIBase(APITest):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.response = {'vision_tags': ['red cup', 'table'], 'vision_description': 'A red cup sits on a table.'}
        self.rating_response = {'rating': 'general', 'reason': 'ordinary content'}
        self.handler = None
        ep = self.request('POST', '/api/llm/endpoints', {'base_url': 'http://127.0.0.1:19283/v1', 'protocol': 'openai_chat',
            'models': [{'name': name, 'supports_images': name != 'rating-model'} for name in ('vision-model', 'nsfw-model', 'rating-model')]}, status=201)
        self.eid = ep['endpoint_id']
        self.request('POST', '/api/llm/endpoints/'+self.eid+'/tokens', {'value': 'vision-test-secret'}, status=201)
        self.bind()
        async def transport(request):
            value = json.loads(request.content)
            self.calls.append(value)
            if self.handler:
                return await self.handler(request, value)
            result = self.rating_response if value['model'] == 'rating-model' else self.response
            return httpx.Response(200, json=reply(text=json.dumps(result), model=value['model']))
        self.runtime.llm.transport = httpx.MockTransport(transport)

    def bind(self, *, nsfw=True, rating=True):
        value = self.request('GET', '/api/llm/roles')
        roles = {'vision': {'endpoint_id': self.eid, 'model': 'vision-model'}}
        if nsfw:
            roles['vision_nsfw'] = {'endpoint_id': self.eid, 'model': 'nsfw-model'}
        if rating:
            roles['rating'] = {'endpoint_id': self.eid, 'model': 'rating-model'}
        self.request('PUT', '/api/llm/roles', {'roles': roles}, version=value['version'])

    def policy(self, keywords=(), blocked=None):
        value = self.request('GET', '/api/llm/vision-policy')
        return self.request('PUT', '/api/llm/vision-policy', {'keywords': list(keywords), 'blocked_terms': value['blocked_terms'] if blocked is None else blocked}, version=value['version'])

    def start(self, *, mode='uncovered', ids=None, config=None, batch=None):
        return self.request('POST', self.p+'/jobs/batches', {'batch_id': batch or str(uuid4()), 'executors': ['llm_vision'],
            'mode': mode, 'scope': {'selected_ids': ids} if ids is not None else {'filter': {}}, 'config': config or {}}, status=202)

    def wait(self, accepted, status='done'):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            row = self.request('GET', accepted['status_url'])
            if row['status'] == status:
                return row
            if row['status'] in ('paused', 'failed', 'canceled'):
                self.fail(str(row))
            time.sleep(.01)
        self.fail('Timed out waiting for vision job')

    def idle(self, job_id):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            with self.runtime.vision.lock:
                if (self.pid, job_id) not in self.runtime.vision.active:
                    return
            time.sleep(.01)
        self.fail('Runner did not stop')

    def control(self, accepted, action):
        row = self.request('GET', accepted['status_url'])
        return self.request('POST', accepted['status_url']+'/control', {'action': action}, version=row['version'])

    def current(self, image_id, kind='tags', layer='llm'):
        with read_snapshot(self.runtime.context.handle) as db:
            return annotations.get_current(db, image_id, layer, kind)

    def manual(self, image_id, rating):
        row = self.request('GET', self.p+f'/images/{image_id}')
        return self.request('PATCH', self.p+f'/images/{image_id}', {'rating': rating}, version=row['version'])

    def gate(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        async def handler(request, body):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            value = self.rating_response if body['model'] == 'rating-model' else self.response
            return httpx.Response(200, json=reply(text=json.dumps(value), model=body['model']))
        self.handler = handler
        return entered, release

class VisionAPITests(VisionAPIBase):
    def test_http_import_vision_cache_edit_export_and_paired_layers(self):
        ids = [item['image_id'] for item in self.import_inputs(2)['items']]
        original = self.current(ids[0], layer='original')
        row = self.wait(self.start())
        self.assertEqual(row['done_count'], 2)
        a, b = self.current(ids[0]), self.current(ids[0], 'nl')
        self.assertEqual(a.model_name, 'vision-model')
        self.assertEqual(a.params_json, b.params_json)
        self.assertIsNone(a.content[0].conf)
        self.assertEqual(self.current(ids[0], layer='original'), original)
        body = json.loads(self.calls[0]['messages'][1]['content'][0]['text'])
        self.assertNotIn('natural_language', body)
        self.assertEqual(body['tags'], ['blue hair', 'smile'])
        cached = self.wait(self.start())
        self.assertEqual(cached['skipped_count'], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.wait(self.start(mode='all'))['done_count'], 2)
        self.keep(ids)
        preview = self.export_preview(include_nl=True)
        exported = self.run_export(preview)
        report = self.request('GET', exported['report_url'])
        caption = next((self.root/report['directory']).glob('*.txt')).read_text(encoding='utf-8')
        self.assertIn('red cup', caption)
        self.assertIn('A red cup sits on a table.', caption)
        raw = json.dumps(row['config_snapshot_json'])
        for forbidden in ('http://', 'vision-test-secret', 'base_url', 'endpoint_id'):
            self.assertNotIn(forbidden, raw)

    def test_keywords_import_highest_match_and_no_model_rating(self):
        self.policy([{'tag': 'blue_hair', 'rating': 'sensitive'}, {'tag': 'smile', 'rating': 'questionable'}])
        image_id = self.import_inputs()['items'][0]['image_id']
        image = self.request('GET', self.p+f'/images/{image_id}')
        self.assertEqual((image['rating'], image['rating_source'], image['rating_verified']), ('questionable', 'keyword', True))
        self.assertEqual(self.wait(self.start(config={'use_rating_model': True}))['done_count'], 1)
        self.assertEqual([c['model'] for c in self.calls], ['nsfw-model'])

    def test_text_rating_original_nl_only_to_r5_and_cached(self):
        self.import_inputs()
        self.wait(self.start(config={'use_rating_model': True}))
        self.assertEqual([c['model'] for c in self.calls], ['rating-model', 'vision-model'])
        parts = self.calls[0]['messages'][1]['content']
        self.assertEqual(len(parts), 1)
        self.assertIn('原始描述', json.loads(parts[0]['text'])['natural_language'])
        self.wait(self.start(mode='all', config={'use_rating_model': True}))
        self.assertEqual([c['model'] for c in self.calls].count('rating-model'), 1)

    def test_refusal_reroutes_once_and_commits_frozen_alternate_model(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        async def handler(request, body):
            value = {'status': 'refused_nsfw', 'reason': 'route'} if body['model'] == 'vision-model' else self.response
            return httpx.Response(200, json=reply(text=json.dumps(value), model=body['model']))
        self.handler = handler
        row = self.wait(self.start())
        self.assertEqual(row['done_count'], 1)
        self.assertEqual([c['model'] for c in self.calls], ['vision-model', 'nsfw-model'])
        image = self.request('GET', self.p+f'/images/{image_id}')
        self.assertEqual((image['rating_source'], image['rating_verified']), ('refusal', True))
        self.assertEqual(self.current(image_id).model_name, 'nsfw-model')

    def test_missing_nsfw_skips_preserves_old_layers_and_repeated_refusal_stops(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        self.wait(self.start())
        old = self.current(image_id)
        self.bind(nsfw=False)
        self.manual(image_id, 'explicit')
        skipped = self.wait(self.start(mode='all'))
        self.assertEqual(skipped['skipped_count'], 1)
        self.assertEqual(self.current(image_id), old)
        self.bind(nsfw=True)
        self.response = {'status': 'refused_nsfw', 'reason': 'still refused'}
        skipped = self.wait(self.start(mode='all'))
        result = self.request('GET', skipped['outcomes_url'])['items'][0]
        self.assertEqual(result['reason'], 'refusal_repeated')
        self.assertEqual(len(self.calls), 2)

    def test_uncertain_fallback_unverified_and_normal_does_not_write_rating(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        self.rating_response = {'rating': 'uncertain', 'reason': 'unknown'}
        self.wait(self.start(config={'use_rating_model': True}))
        self.assertIsNone(self.request('GET', self.p+f'/images/{image_id}')['rating'])
        self.wait(self.start(config={'use_rating_model': True, 'fallback': 'nsfwfallback'}))
        value = self.request('GET', self.p+f'/images/{image_id}')
        self.assertEqual((value['rating_source'], value['rating_verified']), ('fallback', False))
        body = json.loads(self.calls[-1]['messages'][1]['content'][0]['text'])
        self.assertFalse(body['rating_verified'])
        self.assertEqual(body['rating_policy'], 'nsfw')

    def test_empty_results_valid_style_violation_and_invalid_json_preserve_previous(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        self.response = {'vision_tags': [], 'vision_description': ''}
        self.assertEqual(self.wait(self.start())['done_count'], 1)
        old = self.current(image_id)
        self.response = {'vision_tags': ['watercolor'], 'vision_description': ''}
        job = self.wait(self.start(mode='all'))
        self.assertEqual(job['failed_count'], 1)
        self.assertEqual(self.current(image_id), old)
        self.response = {'vision_tags': []}
        job = self.wait(self.start(mode='all'))
        self.assertEqual(job['failed_count'], 1)
        self.assertEqual(len(self.calls), 4)

    def test_pause_finishes_two_inflight_then_resume_and_cancel_discards(self):
        self.import_inputs(3)
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'pause')
        release.set()
        row = self.wait(accepted, 'paused')
        self.idle(row['job_id'])
        row = self.request('GET', accepted['status_url'])
        self.assertEqual(row['done_count'], 2)
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 3)
        entered, release = self.gate()
        accepted = self.start(mode='all')
        self.assertTrue(entered.wait(5))
        self.control(accepted, 'cancel')
        release.set()
        row = self.wait(accepted, 'canceled')
        self.idle(row['job_id'])
        self.assertEqual(self.request('GET', accepted['status_url'])['done_count'], 0)

    def test_manual_rating_change_during_r2_drops_result(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        self.manual(image_id, 'general')
        release.set()
        row = self.wait(accepted)
        self.assertEqual(row['failed_count'], 1)
        self.assertIsNone(self.current(image_id))
        self.assertEqual(self.request('GET', row['outcomes_url'])['items'][0]['reason'], 'vision:rating_conflict')

    def test_auth_failure_pauses_and_resume_uses_same_snapshot(self):
        self.import_inputs()
        async def denied(request, body):
            return httpx.Response(401, text='secret-provider-body')
        self.handler = denied
        accepted = self.start()
        row = self.wait(accepted, 'paused')
        self.idle(row['job_id'])
        self.assertEqual(row['error'], 'llm:unauthorized')
        self.handler = None
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 1)

    def test_busy_outcome_resume_reuses_response(self):
        self.import_inputs()
        from stylelora.services import vision_jobs
        original = vision_jobs.commands.commit_job_outcome
        with patch.object(vision_jobs.commands, 'commit_job_outcome', side_effect=Busy('busy')):
            accepted = self.start()
            row = self.wait(accepted, 'paused')
            self.idle(row['job_id'])
        self.assertEqual(len(self.calls), 1)
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_policy_history_reclassify_preserves_manual_and_refusal(self):
        self.runtime.vision.policy = PolicyStore(self.runtime, self.base/'product')
        first = self.policy([{'tag': 'smile', 'rating': 'sensitive'}])
        ids = [v['image_id'] for v in self.import_inputs(2)['items']]
        self.manual(ids[0], 'general')
        self.policy([{'tag': 'smile', 'rating': 'questionable'}])
        restored = PolicyStore(self.runtime, self.base/'product')
        self.assertEqual(restored.load(first['hash'])['keywords'][0]['rating'], 'sensitive')
        task = self.request('POST', self.p+'/ratings/reclassify', {'scope': {'filter': {}}}, status=202)
        self.wait_operation(task)
        self.assertEqual(self.request('GET', self.p+f'/images/{ids[0]}')['rating_source'], 'manual')
        self.assertEqual(self.request('GET', self.p+f'/images/{ids[1]}')['rating'], 'questionable')
        self.assertEqual(self.calls, [])

    def test_batch_replay_and_configuration_mismatch(self):
        self.import_inputs()
        batch = str(uuid4())
        accepted = self.start(batch=batch)
        row = self.wait(accepted)
        self.assertEqual(self.start(batch=batch)['task'], accepted['task'])
        self.request('POST', self.p+'/jobs/batches', {'batch_id': batch, 'executors': ['llm_vision'], 'mode': 'all', 'scope': {'filter': {}}, 'config': {}}, status=409)
        self.request('POST', self.p+'/jobs/batches', {'batch_id': str(uuid4()), 'executors': ['wd'], 'mode': 'all', 'scope': {'filter': {}}, 'config': {}}, status=422)
        self.assertTrue(self.request('GET', '/api/executors/capabilities')['llm_vision']['available'])

    def test_restart_pauses_no_autorun_and_model_change_blocks_resume(self):
        self.import_inputs(3)
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        project = self.request('GET', self.p)
        closed = self.request('POST', self.p+'/close', {}, version=project['version'], status=202)
        release.set()
        self.wait_operation(closed)
        grant = self.runtime.grant(self.root, 'project_open')
        opened = self.wait_operation(self.request('POST', '/api/projects/open', {'grant_id': grant['grant_id']}, status=202))['result']
        self.pid, self.p = opened['project_id'], '/api/projects/'+opened['project_id']
        accepted['status_url'] = self.p+'/jobs/'+str(accepted['task']['id'])
        row = self.request('GET', accepted['status_url'])
        self.assertEqual(row['status'], 'paused')
        count = len(self.calls)
        roles = self.request('GET', '/api/llm/roles')
        changed = {**roles['roles'], 'vision': {'endpoint_id': self.eid, 'model': 'nsfw-model'}}
        self.request('PUT', '/api/llm/roles', {'roles': changed}, version=roles['version'])
        self.request('POST', accepted['status_url']+'/control', {'action': 'resume'}, version=row['version'], status=503)
        self.assertEqual(self.request('GET', accepted['status_url'])['status'], 'paused')
        self.assertEqual(len(self.calls), count)
        self.bind()
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 3)

    def test_missing_template_prevents_dispatch_and_policy_cas(self):
        self.import_inputs()
        from stylelora.llm.types import LlmFailure
        with patch.object(self.runtime.llm.templates, 'snapshot', side_effect=LlmFailure('skill_unavailable')):
            self.request('POST', self.p+'/jobs/batches', {'batch_id': str(uuid4()), 'executors': ['llm_vision'], 'mode': 'all', 'scope': {'filter': {}}, 'config': {}}, status=503)
        self.assertEqual(self.calls, [])
        old = self.request('GET', '/api/llm/vision-policy')
        self.policy([{'tag': 'smile', 'rating': 'general'}])
        self.request('PUT', '/api/llm/vision-policy', {'keywords': []}, version=old['version'], status=409)
        self.request('PUT', '/api/llm/vision-policy', {'keywords': [{'tag':'blue_hair','rating':'general'}, {'tag':'blue hair','rating':'explicit'}]}, version=self.request('GET','/api/llm/vision-policy')['version'], status=422)

    def test_r5_refusal_routes_once_and_manual_change_before_r2_uses_new_rating(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        self.rating_response = {'status': 'refused_nsfw', 'reason': 'route'}
        self.assertEqual(self.wait(self.start(config={'use_rating_model': True}))['done_count'], 1)
        self.assertEqual([c['model'] for c in self.calls], ['rating-model', 'nsfw-model'])
        # A fresh image has no cached rating; hold only R5.
        new = self.import_inputs(2)['items'][-1]['image_id']
        entered, release = self.gate()
        accepted = self.start(mode='selected', ids=[new], config={'use_rating_model': True})
        self.assertTrue(entered.wait(5))
        self.manual(new, 'general')
        release.set()
        self.assertEqual(self.wait(accepted)['done_count'], 1)
        self.assertEqual([c['model'] for c in self.calls][-2:], ['rating-model', 'vision-model'])
        self.assertEqual(self.request('GET', self.p+f'/images/{new}')['rating_source'], 'manual')
        self.assertEqual(self.request('GET', self.p+f'/images/{image_id}')['rating_source'], 'refusal')

    def test_busy_rating_write_reuses_r5_and_cancel_releases_transient_state(self):
        self.import_inputs()
        with patch.object(images, 'set_rating', side_effect=Busy('fixture busy')):
            accepted = self.start(config={'use_rating_model': True})
            row = self.wait(accepted, 'paused')
            self.idle(row['job_id'])
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 1)
        self.assertEqual([c['model'] for c in self.calls], ['rating-model', 'vision-model'])
        with patch.object(commands, 'commit_job_outcome', side_effect=Busy('fixture busy')):
            second = self.start(mode='all')
            row = self.wait(second, 'paused')
            self.idle(row['job_id'])
        self.control(second, 'cancel')
        self.assertEqual(self.runtime.vision.pending, {})
        self.assertEqual(self.runtime.vision.rating_pending, {})
        self.assertEqual(self.runtime.vision.steps, {})

    def test_partial_annotation_failure_rolls_back_pair_then_reuses_reply(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        def fault(stage):
            if stage == 'job_after_annotation':
                raise Busy('fixture between pair')
        self.runtime.context.handle._fault_hook = fault
        accepted = self.start()
        row = self.wait(accepted, 'paused')
        self.idle(row['job_id'])
        self.assertIsNone(self.current(image_id))
        self.assertIsNone(self.current(image_id, 'nl'))
        self.assertEqual(row['done_count'], 0)
        self.runtime.context.handle._fault_hook = None
        self.control(accepted, 'resume')
        self.assertEqual(self.wait(accepted)['done_count'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_outcome_commit_reconciles_pair_and_counter_once(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        original = commands.commit_job_outcome
        def commit(db, job_id, outcome):
            result = original(db, job_id, outcome)
            def unknown():
                raise CommitUnknown('fixture committed')
            db.after_commit.append(unknown)
            return result
        with patch.object(commands, 'commit_job_outcome', side_effect=commit):
            row = self.wait(self.start())
        self.assertEqual(row['done_count'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(self.current(image_id))
        self.assertIsNotNone(self.current(image_id, 'nl'))

    def test_cache_invalidates_policy_rating_and_requires_both_current_targets(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        self.wait(self.start())
        self.assertEqual(self.wait(self.start())['skipped_count'], 1)
        self.policy(blocked=['watercolor'])
        self.assertEqual(self.wait(self.start())['done_count'], 1)
        self.manual(image_id, 'general')
        self.assertEqual(self.wait(self.start())['done_count'], 1)
        old = self.current(image_id, 'nl')
        # Simulate loss of one current pointer: a matching historical row is not coverage.
        submit_write(self.runtime.context.handle, lambda db: db.execute('UPDATE annotations SET is_current=0 WHERE annotation_id=?', (old.annotation_id,))).result()
        self.assertEqual(self.wait(self.start())['done_count'], 1)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.current(image_id).job_id, self.current(image_id,'nl').job_id)
        self.assertEqual(self.wait(self.start())['skipped_count'], 1)
        self.assertEqual(len(self.calls), 4)

    def test_manual_annotation_is_not_evidence_but_concurrent_wd_change_is(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        current = self.request('GET', self.p+f'/annotations/{image_id}')
        self.request('PUT', self.p+f'/annotations/{image_id}/manual', {'tags': [{'tag':'manual keep'}], 'read_set':current['refs']}, version=current['version'])
        release.set()
        self.assertEqual(self.wait(accepted)['done_count'], 1)
        self.assertNotIn('manual keep', json.dumps(self.calls))
        entered, release = self.gate()
        accepted = self.start(mode='all')
        self.assertTrue(entered.wait(5))
        def wd(db):
            other = jobs.create_batch(db, str(uuid4()), (JobRequest('wd','selected',Scope((image_id,))),))[0]
            jobs.freeze_job(db, other.job_id, JobSnapshot('wd-fixture'), image_ids=(image_id,))
            return other
        other = submit_write(self.runtime.context.handle, wd).result()
        def finish_wd(db):
            value = AnnotationInput(image_id,'wd','tags',(Tag('new evidence',.8),),model_name='wd-fixture',params={'schema_version':1,'requested':{},'actual':{}},job_id=other.job_id,cache_key='changed')
            commands.commit_job_outcome(db,other.job_id,JobOutcome(image_id,'done',(value,)))
            commands.transition_job(db,other.job_id,expected='running',target='done')
        submit_write(self.runtime.context.handle, finish_wd).result()
        release.set()
        row = self.wait(accepted)
        self.assertEqual(row['failed_count'], 1)
        self.assertEqual(self.request('GET', row['outcomes_url'])['items'][0]['reason'], 'vision:evidence_conflict')
        self.assertEqual(self.current(image_id,layer='manual').content[0].tag,'manual keep')

    def test_original_changed_or_missing_never_commits_pair(self):
        ids = [i['image_id'] for i in self.import_inputs(2)['items']]
        entered, release = self.gate()
        accepted = self.start()
        self.assertTrue(entered.wait(5))
        for i, image_id in enumerate(ids):
            row = self.request('GET',self.p+f'/images/{image_id}')
            path = self.root/'images'/row['filename']
            if i: path.unlink()
            else: path.write_bytes(b'changed fixture')
        release.set()
        row = self.wait(accepted)
        self.assertEqual((row['done_count'],row['failed_count'],row['skipped_count']), (0,1,1))
        self.assertTrue(all(self.current(i) is None and self.current(i,'nl') is None for i in ids))

    def test_dal_rejects_forged_or_mismatched_paired_model_metadata(self):
        self.import_inputs()
        with patch.object(commands, 'commit_job_outcome', side_effect=Busy('fixture hold result')):
            accepted = self.start()
            row = self.wait(accepted,'paused')
            self.idle(row['job_id'])
        outcome = self.runtime.vision.pending[(self.pid,row['job_id'])][0][1]
        first, second = outcome.annotations
        for pair in ((replace(first, model_name='forged'),second),
                     (replace(first, params={**first.params,'actual':{**first.params['actual'],'role':'rating'}}),replace(second, params={**second.params,'actual':{**second.params['actual'],'role':'rating'}}))):
            with self.assertRaises(ValidationError):
                submit_write(self.runtime.context.handle,lambda db: commands.commit_job_outcome(db,row['job_id'],replace(outcome,annotations=pair))).result()
        self.assertIsNone(self.current(first.image_id))
        self.control(accepted,'resume')
        self.assertEqual(self.wait(accepted)['done_count'],1)
