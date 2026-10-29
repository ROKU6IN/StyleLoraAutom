import asyncio
from types import SimpleNamespace
import time
from uuid import uuid4

from ..project_db import commands
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.errors import Busy, CommitUnknown, OutcomeConflict
from ..project_db.repositories import images, jobs
from ..project_db.types import JobOutcome, JobRequest, PageRequest, RatingValue
from ..project_db.validation import digest, primitive, relative_path, utc_now
from ..runtime.errors import ServiceError, safe_error
from ..tagging.predictor import read_original
from ..vision.evidence import read_evidence, unchanged
from ..vision.executor import VisionEngine, snapshot, templates, frozen_roles
from ..vision.policy import PolicyStore, VisionConfig, keyword_rating
from .jobs import JobService, Token


class VisionJobService(JobService):
    executor = 'llm_vision'

    def __init__(self, runtime, directory=None):
        super().__init__(runtime)
        self.lock = runtime.jobs.lock
        self.errors = runtime.jobs.errors
        self.policy = PolicyStore(runtime, directory)
        self.rating_pending, self.steps = {}, {}

    def capabilities(self):
        try:
            templates(self.runtime)
            frozen_roles(self.runtime, VisionConfig())
            available, reason = True, None
        except ServiceError as error:
            available, reason = False, error.code
        return {'available': available, 'reason': reason, 'executor': self.executor, 'concurrency': 2,
                'dataset_modes': ['style'], 'combined_batch': True, 'semantic_check': 'literal_checks_only'}

    def create(self, context, body):
        config = VisionConfig.model_validate(body['config'])
        scope = self._scope(body['scope'], body['mode'])
        with self.lock:
            with read_snapshot(context.handle) as db:
                previous = jobs.list_jobs(db, batch_id=body['batch_id'], page=PageRequest(limit=2)).items
            if previous:
                job = previous[0]
                if (len(previous) != 1 or job.executor != self.executor or job.mode != body['mode']
                        or job.scope_json != jobs.scope_json(scope) or job.config_snapshot_json.get('params', {}).get('config') != config.model_dump()):
                    raise ServiceError(409, 'job:batch_conflict')
                return [self.view(context, job)]
            frozen = snapshot(self.runtime, context, config)
            def write(db):
                ids = self._resolve(db, scope)
                self.check_preview_scope(context, body, scope, ids)
                job = jobs.create_batch(db, body['batch_id'], (JobRequest(self.executor, body['mode'], scope),))[0]
                return jobs.freeze_job(db, job.job_id, frozen, image_ids=ids).value
            try:
                job = submit_write(context.handle, write).result()
            except CommitUnknown:
                with read_snapshot(context.handle) as db:
                    found = jobs.list_jobs(db, batch_id=body['batch_id'], page=PageRequest(limit=2)).items
                if len(found) != 1 or not found[0].config_snapshot_json:
                    raise
                job = found[0]
            self._emit(context, job)
            self._schedule(context, job.job_id)
            return [self.get(context, job.job_id)]

    def _validate_resume(self, context, job):
        VisionEngine(self.runtime, context, job)

    def _clear_transient(self, project_id, job_id):
        for mapping in (self.rating_pending, self.steps):
            for key in list(mapping):
                if key[:2] == (project_id, job_id):
                    mapping.pop(key, None)

    def control(self, context, job_id, action, expected):
        with self.lock:
            value = super().control(context, job_id, action, expected)
            if action == 'cancel':
                self._clear_transient(context.project_id, job_id)
            return value

    def _run(self, context, job_id, run, task):
        token = Token(run.canceled, task.cancel)
        key = context.project_id, job_id
        try:
            engine = VisionEngine(self.runtime, context, self.read(context, job_id))
            async def prepare(ids):
                return await asyncio.gather(*(engine.prepare(image_id, token) for image_id in ids), return_exceptions=True)
            while True:
                engine.canceled(token)
                with self.lock:
                    current = self.read(context, job_id)
                    if current.status != 'running':
                        run.stopping = True
                        break
                    remaining = [i for i in current.progress_json['image_ids'] if str(i) not in current.progress_json['outcomes']]
                if not remaining:
                    break
                queue = self.pending.get(key)
                failure = None
                if not queue:
                    results = asyncio.run(prepare(remaining[:2]))
                    engine.canceled(token)
                    queue = []
                    for item in results:
                        if isinstance(item, BaseException):
                            failure = item
                        else:
                            queue.append(item)
                    self.pending[key] = queue
                while queue:
                    engine.canceled(token)
                    self._persist(context, job_id, queue[0])
                    image_id = queue[0][0].image.image_id
                    queue.pop(0)
                    self.rating_pending.pop((*key, image_id), None)
                    self.steps.pop((*key, image_id), None)
                    row = self.read(context, job_id)
                    self.runtime.supervisor.progress(task, completed=row.done_count+row.failed_count+row.skipped_count, total=row.total)
                self.pending.pop(key, None)
                if failure:
                    raise failure
            with self.lock:
                self._finish(context, job_id)
            return {'job_id': job_id}
        except Exception as error:
            with self.lock:
                run.stopping = True
                self._pause(context, job_id, 'job:interrupted' if task.cancel.is_set() else safe_error(error).code)
            return {'job_id': job_id}
        finally:
            self._finalize(context, job_id, run)
            with self.lock:
                if run.canceled.is_set():
                    self._clear_transient(context.project_id, job_id)

    def _persist(self, context, job_id, pending):
        evidence, outcome = pending
        row, stamp = evidence.image, None
        if outcome.kind == 'done' or outcome.reason == 'cache_hit':
            try:
                read_original(context.handle, row)
                stat = relative_path(context.handle.root, 'images/'+row.filename).stat()
                stamp = stat.st_size, stat.st_mtime_ns, stat.st_ino
            except (FileNotFoundError, ServiceError) as error:
                missing = isinstance(error, FileNotFoundError) or error.code == 'image:file_missing'
                outcome = JobOutcome(row.image_id, 'skipped' if missing else 'failed', reason='file_missing' if missing else 'image:image_changed')
        submitted = digest(outcome)
        def write(db):
            nonlocal submitted
            job = jobs.get_job(db, job_id)
            receipt = job.progress_json['outcomes'].get(str(row.image_id))
            if receipt:
                if receipt['digest'] != submitted:
                    raise OutcomeConflict('Result differs from recorded outcome')
                return job
            if job.status == 'canceled':
                return job
            fresh, value = images.get_image(db, row.image_id), outcome
            if fresh.file_missing:
                value = JobOutcome(row.image_id, 'skipped', reason='file_missing')
            elif value.kind == 'done' or value.reason == 'cache_hit':
                path = relative_path(context.handle.root, 'images/'+fresh.filename)
                if not path.is_file():
                    value = JobOutcome(row.image_id, 'skipped', reason='file_missing')
                elif images.rating_value(fresh) != images.rating_value(row):
                    value = JobOutcome(row.image_id, 'failed', reason='vision:rating_conflict')
                elif not unchanged(db, evidence):
                    value = JobOutcome(row.image_id, 'failed', reason='vision:evidence_conflict')
                else:
                    stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != stamp or (fresh.sha256, fresh.filename) != (row.sha256, row.filename):
                        value = JobOutcome(row.image_id, 'failed', reason='image:image_changed')
            submitted = digest(value)
            return commands.commit_job_outcome(db, job_id, value).value
        with self.lock:
            for attempt in range(3):
                try:
                    job = submit_write(context.handle, write).result()
                    break
                except CommitUnknown:
                    job = self.read(context, job_id)
                    receipt = job.progress_json['outcomes'].get(str(row.image_id))
                    if not receipt or receipt['digest'] != submitted:
                        raise
                    break
                except Busy:
                    if attempt == 2:
                        raise
                    time.sleep(.05*(attempt+1))
            if str(row.image_id) in job.progress_json['outcomes']:
                self._emit(context, job, row.image_id)

    def import_keywords(self, context, image_id):
        policy = self.policy.load(self.policy.view()['hash'])
        if not policy['keywords']:
            return
        def write(db):
            evidence = read_evidence(db, image_id)
            current = images.rating_value(evidence.image)
            if current and current.verified:
                return
            value = keyword_rating(evidence.tags, policy)
            if value:
                images.set_rating(db, image_id, expected=current, value=RatingValue(value, 'keyword', True, utc_now()))
        submit_write(context.handle, write).result()

    def reclassify(self, context, body):
        config = VisionConfig.model_validate(body['config'])
        scope = self._scope(body['scope'], 'selected' if body['scope'].get('selected_ids') is not None else 'all')
        frozen = snapshot(self.runtime, context, config, rating_only=True)
        with read_snapshot(context.handle) as db:
            ids = self._resolve(db, scope)
            expected = {i: images.rating_value(images.get_image(db, i)) for i in ids}
        job = SimpleNamespace(job_id=0, cache_id=str(uuid4()), mode='all', config_snapshot_json=primitive(frozen))
        engine = VisionEngine(self.runtime, context, job)
        def work(task):
            results = []
            for image_id in ids:
                engine.canceled(task.cancel)
                def clear(db):
                    row = images.get_image(db, image_id)
                    if images.rating_value(row) != expected[image_id]:
                        raise ServiceError(409, 'vision:rating_conflict')
                    if row.rating_source in ('manual', 'refusal'):
                        return False
                    images.clear_recomputable_ratings(db, (image_id,))
                    return True
                try:
                    if not submit_write(context.handle, clear).result():
                        results.append({'image_id': image_id, 'status': 'preserved'})
                        continue
                    with read_snapshot(context.handle) as db:
                        evidence = read_evidence(db, image_id)
                    content = read_original(context.handle, evidence.image)
                    asyncio.run(engine.rating(evidence, content, task.cancel))
                    results.append({'image_id': image_id, 'status': 'done'})
                except ServiceError as error:
                    from ..vision.executor import FATAL
                    if error.code in FATAL:
                        raise
                    results.append({'image_id': image_id, 'status': 'failed', 'reason': error.code})
                finally:
                    self.rating_pending.pop((context.project_id, job.cache_id, image_id), None)
                self.runtime.supervisor.progress(task, completed=len(results), total=len(ids))
            return {'items': results}
        return self.runtime.supervisor.submit('rating.reclassify', work, context=context)

    def invalidate(self, project_id):
        super().invalidate(project_id)
        with self.lock:
            for mapping in (self.rating_pending, self.steps):
                for key in list(mapping):
                    if key[0] == project_id:
                        mapping.pop(key)
