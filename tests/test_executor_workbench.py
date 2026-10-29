from unittest.mock import patch
from uuid import uuid4

from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import images, jobs
from stylelora.project_db.types import RatingValue
from stylelora.project_db.errors import Busy, CommitUnknown
from stylelora.project_db.validation import utc_now
from stylelora.runtime.errors import ServiceError
from . import test_api_job_batches as support


class ExecutorWorkbenchTests(support.BatchAPIBase):
    def failed_wd(self, count=2):
        self.import_inputs(count)
        parent = self.counted
        failed = [False]
        class Failing(parent):
            def predict_one(inner, content, cancel):
                if not failed[0]:
                    failed[0] = True
                    self.wd_calls.append(content)
                    raise ServiceError(503, 'tagger:inference_failed')
                return super().predict_one(content, cancel)
        self.runtime.jobs.engine_factory = Failing
        accepted = self.start_batch(mode='all')
        batch = self.batch_wait(accepted)
        self.runtime.jobs.engine_factory = parent
        return accepted, batch['items'][0]

    def retry_info(self, row):
        return self.request('GET', row['status_url']+'/retry-preview')

    def retry_job(self, row, preview=None, batch=None, status=202):
        info = preview or self.retry_info(row)
        return self.request('POST', row['status_url']+'/retry',
            {'batch_id': batch or str(uuid4()), 'preview_digest': info['preview_digest']}, status=status)

    def test_workbench_filter_matches_rating_search_review_source_and_missing(self):
        ids = [v['image_id'] for v in self.import_inputs(3)['items']]
        self.keep(ids[:2])
        def prepare(db):
            for image_id, rating in zip(ids, ['sensitive', 'general', 'sensitive']):
                images.set_rating(db, image_id, expected=None, value=RatingValue(rating, 'manual', True, utc_now()))
            return images.get_image(db, ids[0]).source_id
        source = submit_write(self.runtime.context.handle, prepare).result()
        body = self.body(mode='all')
        body['scope'] = {'filter': {'source_ids': [source], 'review_statuses': ['keep'], 'rating': ['sensitive'], 'q': 'BLUE', 'file_missing': False}}
        preview = self.request('POST', self.p+'/jobs/preview', body)
        listed = self.request('GET', self.p+f'/images?source_id={source}&review_status=keep&rating=sensitive&q=BLUE&file_missing=false')
        self.assertEqual(preview['image_count'], len(listed['items']))
        self.assertEqual(preview['image_count'], 1)
        accepted = self.request('POST', self.p+'/jobs/batches', {**body, 'batch_id': str(uuid4()), 'preview_scope_digest': preview['scope_digest']}, status=202)
        batch = self.batch_wait(accepted)
        for row in batch['items']:
            with read_snapshot(self.runtime.context.handle) as db:
                frozen = jobs.get_job(db, row['job_id'])
            self.assertEqual(frozen.progress_json['image_ids'], ids[:1])
            self.assertEqual(frozen.scope_json['filter']['q'], 'BLUE')

    def test_selected_filter_full_membership_and_stale_preview(self):
        self.import_inputs(3)
        body = self.body(mode='selected')
        body['scope'] = {'filter': {'q': 'smile'}}
        first = self.request('POST', self.p+'/jobs/preview', body)
        self.assertEqual(first['image_count'], 3)
        self.import_inputs(4)
        self.request('POST', self.p+'/jobs/batches', {**body, 'batch_id': str(uuid4()), 'preview_scope_digest': first['scope_digest']}, status=409)
        latest = self.request('POST', self.p+'/jobs/preview', body)
        accepted = self.request('POST', self.p+'/jobs/batches', {**body, 'batch_id': str(uuid4()), 'preview_scope_digest': latest['scope_digest']}, status=202)
        self.assertEqual(self.batch_wait(accepted)['image_count'], 4)

    def test_advanced_filter_replay_and_frozen_scope_survive_changes(self):
        ids = [v['image_id'] for v in self.import_inputs(2)['items']]
        body = {**self.body(), 'batch_id': str(uuid4())}
        body['scope'] = {'filter': {'q': 'blue', 'file_missing': False}}
        entered, release = self.wd_gate()
        accepted = self.request('POST', self.p+'/jobs/batches', body, status=202)
        self.assertTrue(entered.wait(5))
        self.batch_control(accepted, 'pause')
        release.set()
        self.wd_idle(accepted['items'][0]['job_id'])
        submit_write(self.runtime.context.handle, lambda db: images.set_file_missing(db, ids[1], missing=True)).result()
        replay = self.request('POST', self.p+'/jobs/batches', body, status=202)
        self.assertEqual(replay['items'][1]['job_id'], accepted['items'][1]['job_id'])
        self.batch_control(accepted, 'resume')
        batch = self.batch_wait(accepted)
        self.assertEqual(batch['image_count'], 2)
        self.assertEqual(batch['items'][1]['skipped_count'], 1)

    def test_invalid_filter_contracts_and_legacy_scope_serialization(self):
        self.import_inputs()
        for query in ({'q': 'a'*4097}, {'rating':['unknown']}, {'file_missing':'false'}, {'unknown':True}):
            body = self.body(); body['scope'] = {'filter': query}
            self.request('POST', self.p+'/jobs/preview', body, status=422)
        accepted = self.start_batch()
        batch = self.batch_wait(accepted)
        with read_snapshot(self.runtime.context.handle) as db:
            scope = jobs.get_job(db, batch['items'][0]['job_id']).scope_json
        self.assertEqual(set(scope['filter']), {'source_ids', 'review_statuses', 'include_missing'})

    def test_retry_only_failed_ids_forces_execution_keeps_parent_and_frozen_config(self):
        accepted, row = self.failed_wd()
        before = self.request('GET', row['outcomes_url'])
        info = self.retry_info(row)
        self.assertEqual(info['image_count'], 1)
        self.assertTrue(info['available'])
        calls = len(self.wd_calls)
        retried = self.retry_job(row, info)
        finished = self.batch_wait(retried)
        self.assertEqual(finished['done_count'], 1)
        self.assertEqual(len(finished['items']), 1)
        self.assertEqual(len(self.wd_calls), calls+1)
        self.assertEqual(self.request('GET', row['outcomes_url']), before)
        self.assertEqual(self.request('GET', accepted['batch_url'])['failed_count'], 1)
        frozen = finished['items'][0]['config_snapshot_json']
        self.assertEqual(frozen['params']['config'], row['config_snapshot_json']['params']['config'])
        self.assertEqual(frozen['params']['retry_origin']['job_id'], row['job_id'])
        self.assertEqual(finished['items'][0]['mode'], 'all')

    def test_retry_unknown_commit_replays_after_dependency_removed(self):
        _, row = self.failed_wd()
        info, batch = self.retry_info(row), str(uuid4())
        original = jobs.freeze_job
        def unknown(db, *args, **kwargs):
            result = original(db, *args, **kwargs)
            def fault(): raise CommitUnknown('fixture committed')
            db.after_commit.append(fault)
            return result
        with patch.object(jobs, 'freeze_job', side_effect=unknown):
            accepted = self.retry_job(row, info, batch)
        self.batch_wait(accepted)
        with patch.object(self.runtime.jobs, 'require_model', side_effect=ServiceError(503, 'tagger:model_missing')):
            replay = self.retry_job(row, info, batch)
        self.assertEqual(replay['items'][0]['job_id'], accepted['items'][0]['job_id'])
        self.retry_job(row, {**info, 'preview_digest':'a'*64}, batch, status=409)

    def test_retry_stale_digest_and_transaction_failure_do_not_create_jobs(self):
        _, row = self.failed_wd()
        count = len(self.request('GET', self.p+'/jobs')['items'])
        self.retry_job(row, {'preview_digest':'a'*64}, status=409)
        with patch.object(jobs, 'freeze_job', side_effect=Busy('fixture busy')):
            self.retry_job(row, status=503)
        self.assertEqual(len(self.request('GET', self.p+'/jobs')['items']), count)

    def test_retry_requires_finished_parent_batch_and_actual_failures(self):
        self.import_inputs()
        entered, release = self.wd_gate()
        accepted = self.start_batch()
        self.assertTrue(entered.wait(5))
        info = self.retry_info(accepted['items'][0])
        self.assertEqual(info['reason'], 'job:retry_not_ready')
        self.retry_job(accepted['items'][0], info, status=409)
        release.set()
        row = self.batch_wait(accepted)['items'][0]
        info = self.retry_info(row)
        self.assertEqual(info['reason'], 'job:no_failed_images')
        self.retry_job(row, info, status=409)

    def test_vision_retry_validates_old_roles_preserves_paired_results(self):
        self.import_inputs()
        correct = self.response
        self.response = {'vision_tags': []}  # Incomplete pair fails strict JSON validation.
        accepted = self.start_batch()
        row = self.batch_wait(accepted)['items'][1]
        self.response = correct
        self.assertEqual(row['failed_count'], 1)
        roles = self.request('GET', '/api/llm/roles')
        info = self.retry_info(row)
        self.request('PUT', '/api/llm/roles', {'roles': {}}, version=roles['version'])
        self.assertFalse(self.retry_info(row)['available'])
        self.retry_job(row, info, status=503)
        self.bind()
        retried = self.retry_job(row)
        finished = self.batch_wait(retried)['items'][0]
        outcomes = self.request('GET', finished['outcomes_url'])
        self.assertEqual(len(outcomes['items'][0]['annotation_ids']), 2)

    def test_missing_filter_reports_skipped_without_inference(self):
        image_id = self.import_inputs()['items'][0]['image_id']
        submit_write(self.runtime.context.handle, lambda db: images.set_file_missing(db, image_id, missing=True)).result()
        body = self.body(); body['scope'] = {'filter': {'file_missing': True}}
        preview = self.request('POST', self.p+'/jobs/preview', body)
        self.assertEqual([v['file_missing'] for v in preview['items']], [1,1])
        accepted = self.request('POST', self.p+'/jobs/batches', {**body, 'batch_id':str(uuid4())}, status=202)
        self.assertEqual(self.batch_wait(accepted)['skipped_count'], 2)
        self.assertEqual(self.wd_calls, [])
        self.assertEqual(self.calls, [])

    def test_filter_selection_materializes_beyond_loaded_page_without_inference(self):
        for index in range(205):
            self.make_input(f'page-{index}.png', color=(index, 25, 80, 200))
        preview = self.import_preview()
        self.wait_operation(self.request('POST', self.p+'/imports', {'preview_id':preview['preview_id'], 'digest':preview['digest']}, status=202))
        page = self.request('GET', self.p+'/images?limit=200&q=blue')
        self.assertEqual(len(page['items']), 200)
        self.assertTrue(page['has_more'])
        body = self.body(mode='selected'); body['scope'] = {'filter': {'q':'blue'}}
        self.assertEqual(self.request('POST', self.p+'/jobs/preview', body)['image_count'], 205)
        self.assertEqual(self.request('GET', self.p+'/jobs')['items'], [])
        self.assertEqual(self.wd_calls, [])
        self.assertEqual(self.calls, [])

    def test_failed_retry_does_not_skip_using_an_older_successful_cache(self):
        self.import_inputs(2)
        self.batch_wait(self.start_batch())
        _, row = self.failed_wd()
        calls = len(self.wd_calls)
        retried = self.batch_wait(self.retry_job(row))
        self.assertEqual(retried['done_count'], 1)
        self.assertEqual(retried['skipped_count'], 0)
        self.assertEqual(len(self.wd_calls), calls+1)
