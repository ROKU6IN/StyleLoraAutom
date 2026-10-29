"""Persistent WD jobs coordinated through F01; inference never holds a DB session."""
from dataclasses import dataclass, field, replace
import threading
import time

from ..project_db import commands
from ..project_db.cache import make_cache_key
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.errors import Busy, CommitUnknown, NotFound, SnapshotMismatch, OutcomeConflict
from ..project_db.repositories import annotations, images, jobs
from ..project_db.types import AnnotationInput, ImageFilter, JobOutcome, JobRequest, JobSnapshot, PageRequest, Scope
from ..project_db.validation import digest, primitive, relative_path
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError, safe_error
from ..tagging.models import check_cancel
from ..tagging.predictor import OrtTagger, TaggerConfig, cache_parameters, model_name, read_original

TERMINAL = {'done', 'failed', 'canceled'}


@dataclass
class Run:
    canceled: threading.Event = field(default_factory=threading.Event)
    operation_id: str | None = None
    stopping: bool = False


class Token:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)


class JobService:
    executor = 'wd'
    def __init__(self, runtime, *, engine_factory=OrtTagger):
        self.runtime, self.engine_factory = runtime, engine_factory
        self.lock, self.worker = threading.RLock(), threading.Lock()
        self.active, self.pending, self.errors = {}, {}, {}

    def read(self, context, job_id):
        with read_snapshot(context.handle) as db:
            return jobs.get_job(db, job_id)

    def version(self, context, job):
        return self.runtime.versions.get(f'{context.project_id}:job:{job.job_id}',
            [job.updated_at, job.status, job.done_count, job.failed_count, job.skipped_count])

    def view(self, context, job):
        result = primitive(job)
        result.pop('progress_json')
        result['version'] = self.version(context, job)
        result['status_url'] = f'/api/projects/{context.project_id}/jobs/{job.job_id}'
        result['outcomes_url'] = result['status_url'] + '/outcomes'
        result['task'] = {'kind': 'job', 'id': job.job_id, 'project_id': context.project_id}
        result['runtime_error'] = self.errors.get((context.project_id, job.job_id))
        return result

    def get(self, context, job_id):
        return self.view(context, self.read(context, job_id))

    def list(self, context, *, status=None, batch_id=None, executor=None, limit=50, cursor=None):
        identity = digest([context.project_id, 'jobs', status, batch_id, executor])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        page = PageRequest(state['last'] if state else 0, limit, state['upper'] if state else None)
        with read_snapshot(context.handle) as db:
            result = (jobs.list_jobs_for_executor(db, executor, status=status, batch_id=batch_id, page=page) if executor
                      else jobs.list_jobs(db, status=status, batch_id=batch_id, page=page))
        return {'items': [self.view(context, j) for j in result.items], 'limit': limit, 'upper_id': result.upper_id,
                'has_more': result.next_after_id is not None,
                'next_cursor': self.runtime.cursors.encode({'identity': identity, 'upper': result.upper_id, 'last': result.next_after_id}) if result.next_after_id is not None else None}

    def outcomes(self, context, job_id, *, kind=None, limit=50, cursor=None):
        job = self.read(context, job_id)
        identity = digest([context.project_id, 'job-outcomes', job_id, kind])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        values = sorted((int(i), v) for i, v in job.progress_json.get('outcomes', {}).items() if kind is None or v['kind'] == kind)
        upper = state['upper'] if state else max((i for i, _ in values), default=0)
        last = state['last'] if state else 0
        remaining = [(i, v) for i, v in values if last < i <= upper]
        page = remaining[:limit]
        more = len(remaining) > limit
        return {'items': [{'image_id': i, **{k: v[k] for k in ('kind', 'reason', 'annotation_ids', 'cache_keys')}} for i, v in page],
                'limit': limit, 'upper_id': upper, 'has_more': more,
                'next_cursor': self.runtime.cursors.encode({'identity': identity, 'upper': upper, 'last': page[-1][0]}) if more else None}

    def require_model(self, config, runtime_version=None):
        tagger = self.runtime.tagger
        status = tagger.status()
        if not status['available']:
            raise ServiceError(503, status['reason'])
        if runtime_version is not None and status['version'] != runtime_version:
            raise ServiceError(503, 'job:runtime_changed')
        manifest = tagger.manifests.get(config.manifest_id)
        if manifest is None:
            raise ServiceError(503, 'tagger:manifest_invalid')
        try:
            tagger.store.footprint(manifest)
        except FileNotFoundError:
            raise ServiceError(503, 'tagger:model_missing') from None
        return manifest, status['version']

    def _scope(self, value, mode):
        selected, query = value.get('selected_ids'), value.get('filter')
        if (selected is None) == (query is None):
            raise ServiceError(422, 'input:invalid')
        if selected is not None:
            return Scope(tuple(sorted(set(selected))))
        return Scope(filter=ImageFilter(tuple(query['source_ids']) if query.get('source_ids') is not None else None,
            tuple(query['review_statuses']) if query.get('review_statuses') is not None else None,
            query.get('include_missing', True),
            tuple(query['rating']) if query.get('rating') else None,
            query.get('file_missing'), query.get('q') or None))

    @staticmethod
    def _resolve(db, scope):
        if scope.selected_ids is not None:
            try:
                for image_id in scope.selected_ids:
                    images.get_image(db, image_id)
            except NotFound:
                raise ServiceError(422, 'job:invalid_scope') from None
            return scope.selected_ids
        query = scope.filter
        query = replace(query, review_statuses=query.review_statuses if query.review_statuses is not None else ('keep', 'unreviewed'))
        ids, after, upper = [], 0, None
        while True:
            page = images.list_images(db, query, PageRequest(after, 1000, upper))
            ids.extend(i.image_id for i in page.items)
            if len(ids) > 5000:
                raise ServiceError(413, 'batch:too_large')
            if page.next_after_id is None:
                return tuple(ids)
            after, upper = page.next_after_id, page.upper_id

    def create(self, context, body):
        if len(body['executors']) == 2:
            return self.runtime.batches.create(context, body)
        if body['executors'] == ['llm_vision']:
            return self.runtime.vision.create(context, body)
        config = TaggerConfig.model_validate(body['config'])
        scope = self._scope(body['scope'], body['mode'])
        request = JobRequest('wd', body['mode'], scope)
        # A repeated batch is a read, including after process restart. Never
        # silently resume a paused job or require newly installed configuration.
        with self.lock:
            with read_snapshot(context.handle) as db:
                previous = jobs.list_jobs(db, batch_id=body['batch_id'], page=PageRequest(limit=2)).items
            if previous:
                job = previous[0]
                if (len(previous) != 1 or job.executor != 'wd' or job.mode != body['mode']
                        or job.scope_json != jobs.scope_json(scope)
                        or job.config_snapshot_json.get('params', {}).get('config') != config.model_dump()):
                    raise ServiceError(409, 'job:batch_conflict')
                return [self.view(context, job)]
            manifest, runtime_version = self.require_model(config)
            snapshot = JobSnapshot(model_name(manifest), role='wd', protocol='onnx',
                params={'config': config.model_dump(), 'runtime_version': runtime_version, 'pipeline_version': 'wd-runner-v2'})
            def write(db):
                ids = self._resolve(db, scope)
                self.check_preview_scope(context, body, scope, ids)
                job = jobs.create_batch(db, body['batch_id'], (request,))[0]
                return jobs.freeze_job(db, job.job_id, snapshot, image_ids=ids).value
            try:
                job = submit_write(context.handle, write).result()
            except SnapshotMismatch:
                raise ServiceError(409, 'job:batch_conflict') from None
            except CommitUnknown:
                # Reconcile the batch rather than create another persistent job.
                with read_snapshot(context.handle) as db:
                    found = jobs.list_jobs(db, batch_id=body['batch_id'], page=PageRequest(limit=2)).items
                if len(found) != 1:
                    raise
                job = found[0]
            self._emit(context, job)
            self._schedule(context, job.job_id)
            return [self.get(context, job.job_id)]

    @staticmethod
    def scope_digest(context, mode, scope, ids):
        return digest([context.project_id, mode, jobs.scope_json(scope), ids])

    def check_preview_scope(self, context, body, scope, ids):
        expected = body.get('preview_scope_digest')
        if expected is not None and expected != self.scope_digest(context, body['mode'], scope, ids):
            raise ServiceError(409, 'job:preview_stale')

    def _schedule(self, context, job_id):
        key = (context.project_id, job_id)
        if key in self.active and not self.active[key].stopping:
            return
        run = Run()
        self.active[key] = run
        self.errors.pop(key, None)
        try:
            operation = self.runtime.supervisor.submit('job.' + self.executor, lambda task: self._run(context, job_id, run, task),
                context=context, finalizer=lambda: self._finalize(context, job_id, run))
            run.operation_id = operation['operation_id']
        except Exception as error:
            self.active.pop(key, None)
            self._pause(context, job_id, safe_error(error).code)

    def control(self, context, job_id, action, expected):
        with self.lock:
            job = self.read(context, job_id)
            if self.executor == 'wd' and job.executor == 'llm_vision':
                return self.runtime.vision.control(context, job_id, action, expected)
            Versions.require(expected, self.version(context, job))
            if job.executor != self.executor:
                raise ServiceError(503, 'job:executor_unavailable')
            target = {'pause': 'paused', 'resume': 'running', 'cancel': 'canceled'}[action]
            if job.status == target:
                return self.view(context, job)
            if job.status in TERMINAL or action == 'pause' and job.status != 'running':
                raise ServiceError(409, 'job:state_conflict')
            if action == 'resume':
                self._validate_resume(context, job)
            def write(db):
                current = jobs.get_job(db, job_id)
                Versions.require(expected, self.version(context, current))
                return commands.transition_job(db, job_id, expected=current.status, target=target).value
            job = submit_write(context.handle, write).result()
            run = self.active.get((context.project_id, job_id))
            if action == 'cancel':
                if run:
                    run.canceled.set()
                self.pending.pop((context.project_id, job_id), None)
            elif action == 'resume':
                self._schedule(context, job_id)
            self._emit(context, job)
            return self.view(context, job)

    def _validate_resume(self, context, job):
        config = self._config(job)
        self.require_model(config, job.config_snapshot_json['params']['runtime_version'])

    @staticmethod
    def _config(job):
        try:
            if job.config_snapshot_json['params']['pipeline_version'] not in ('wd-runner-v1', 'wd-runner-v2'):
                raise ValueError()
            return TaggerConfig.model_validate(job.config_snapshot_json['params']['config'])
        except (KeyError, ValueError):
            raise ServiceError(503, 'job:snapshot_unavailable') from None

    def _emit(self, context, job, image_id=None):
        event = {'running': 'started', 'paused': 'paused', 'done': 'done', 'failed': 'failed', 'canceled': 'canceled'}.get(job.status, 'changed')
        outcome = job.progress_json.get('outcomes', {}).get(str(image_id), {})
        self.runtime.emit('job.image_completed' if image_id is not None else 'job.'+event,
            {'job_id': job.job_id, 'batch_id': job.batch_id, 'executor': job.executor, 'image_id': image_id, 'status': job.status, 'total': job.total,
             'done_count': job.done_count, 'failed_count': job.failed_count, 'skipped_count': job.skipped_count,
             'kind': outcome.get('kind'), 'reason': outcome.get('reason') if image_id is not None else job.error},
            project_id=context.project_id, resource={'kind': 'job', 'id': job.job_id}, version=self.version(context, job))
        if image_id is not None:
            self.runtime.emit('annotations.changed', {'image_id': image_id}, project_id=context.project_id,
                              resource={'kind': 'image', 'id': image_id})

    def _pause(self, context, job_id, reason):
        def write(db):
            job = jobs.get_job(db, job_id)
            return commands.transition_job(db, job_id, expected='running', target='paused', error=reason).value if job.status == 'running' else None
        try:
            job = submit_write(context.handle, write).result()
            if job is not None:
                self._emit(context, job)
        except Exception as error:
            # A full disk can also prevent persisting paused. Keep the diagnosis
            # and prepared result in memory; never leak the project lease.
            self.errors[(context.project_id, job_id)] = safe_error(error).code

    def _finalize(self, context, job_id, run):
        with self.lock:
            key = (context.project_id, job_id)
            if self.active.get(key) is run:
                self.active.pop(key)
                self._pause(context, job_id, 'job:interrupted')
                if run.canceled.is_set():
                    self.pending.pop(key, None)

    def _finish(self, context, job_id):
        def write(db):
            job = jobs.get_job(db, job_id)
            if job.status in ('running', 'paused') and job.done_count+job.failed_count+job.skipped_count == job.total:
                return commands.transition_job(db, job_id, expected=job.status, target='done').value
            return job
        job = submit_write(context.handle, write).result()
        self._emit(context, job)
        if job.status == 'done':
            self.runtime.batches.advance(context, job.batch_id)

    def _run(self, context, job_id, run, task):
        token, acquired, engine = Token(run.canceled, task.cancel), False, None
        try:
            while not acquired:
                check_cancel(token)
                acquired = self.worker.acquire(timeout=.1)
            with self.lock:
                job = self.read(context, job_id)
                if job.status != 'running':
                    run.stopping = True
                    return {'job_id': job_id}
                config = self._config(job)
                version = job.config_snapshot_json['params']['runtime_version']
                manifest, _ = self.require_model(config, version)
            tagger = self.runtime.tagger
            engine = self.engine_factory(tagger.store, manifest, config, token, device_probe=tagger.device_probe, status=tagger.status)
            if hasattr(engine, 'stamp'):
                from ..project_db.validation import utc_now
                with tagger.lock:
                    tagger.verified[manifest.digest] = (engine.stamp, utc_now())
                    tagger.errors.pop(manifest.digest, None)
            key = (context.project_id, job_id)
            while True:
                check_cancel(token)
                with self.lock:
                    current = self.read(context, job_id)
                    if current.status != 'running':
                        run.stopping = True
                        break
                    remaining = [i for i in current.progress_json['image_ids']
                                 if str(i) not in current.progress_json['outcomes']]
                if not remaining:
                    break
                queue = self.pending.get(key)
                if queue:
                    while queue and str(queue[0][0].image_id) in current.progress_json['outcomes']:
                        queue.pop(0)
                if not queue:
                    capacity = engine.batch_capacity() if hasattr(engine, 'batch_capacity') else 1
                    if current.config_snapshot_json['params']['pipeline_version'] == 'wd-runner-v1':
                        capacity = 1  # Preserve the in-flight unit of historical jobs.
                    queue = self._prepare_many(context, current, remaining[:capacity], manifest,
                                               config, version, engine, token)
                    check_cancel(token)
                    self.pending[key] = queue
                # Finish the accepted in-flight batch on pause; cancel discards
                # uncommitted results. Retain the whole queue if a write is busy.
                while queue:
                    check_cancel(token)
                    self._persist(context, job_id, queue[0])
                    queue.pop(0)
                    row = self.read(context, job_id)
                    self.runtime.supervisor.progress(task, completed=row.done_count+row.failed_count+row.skipped_count, total=row.total)
                self.pending.pop(key, None)
            with self.lock:
                self._finish(context, job_id)
            return {'job_id': job_id}
        except Exception as error:
            with self.lock:
                run.stopping = True
                if self.active.get((context.project_id, job_id)) is run:
                    self._pause(context, job_id, 'job:interrupted' if task.cancel.is_set() else safe_error(error).code)
            return {'job_id': job_id}
        finally:
            try:
                if engine:
                    engine.close()
            finally:
                if acquired:
                    self.worker.release()
                self._finalize(context, job_id, run)

    @staticmethod
    def _failure(image_id, error):
        if error.code in ('operation:canceled', 'tagger:model_changed', 'job:snapshot_unavailable'):
            raise error
        return JobOutcome(image_id, 'skipped' if error.code == 'image:file_missing' else 'failed',
            reason='file_missing' if error.code == 'image:file_missing' else error.code)

    def _prepare_many(self, context, job, image_ids, manifest, config, runtime_version, engine, token):
        prepared, inputs = [], []
        for image_id in image_ids:
            check_cancel(token)
            with read_snapshot(context.handle) as db:
                row = images.get_image(db, image_id)
                previous = annotations.get_current(db, image_id, 'wd', 'tags')
            ref = previous.annotation_id if previous else None
            def cache(provider, row=row):
                return make_cache_key(source='wd', type='tags', image_sha256=row.sha256, model=model_name(manifest),
                    protocol='onnx', skill_hash='none:wd-v1', actual_params=cache_parameters(manifest, config, runtime_version, provider),
                    route='wd', keyword_hash='none:wd-v1')
            index = len(prepared)
            prepared.append((row, ref, None))
            try:
                content = read_original(context.handle, row)
                providers = ('cpu', 'cuda') if config.provider == 'auto' else ('cpu',)
                if job.mode != 'all' and previous and previous.cache_key in {cache(p) for p in providers}:
                    prepared[index] = (row, ref, JobOutcome(image_id, 'skipped', reason='cache_hit', annotation_ids=(ref,)))
                else:
                    inputs.append((index, content, cache))
            except ServiceError as error:
                prepared[index] = (row, ref, self._failure(image_id, error))
        if inputs:
            if len(inputs) == 1:
                try:
                    results = [engine.predict_one(inputs[0][1], token)]
                except ServiceError as error:
                    results = [error]
            else:
                results = engine.predict_many([content for _, content, _ in inputs], token)
            check_cancel(token)
            if len(results) != len(inputs):
                raise ServiceError(503, 'tagger:invalid_output')
            for (index, _, cache), result in zip(inputs, results):
                row, ref, _ = prepared[index]
                try:
                    if isinstance(result, ServiceError):
                        raise result
                    if result.actual_model != model_name(manifest) or result.params['actual']['runtime_version'] != runtime_version:
                        raise ServiceError(503, 'job:snapshot_unavailable')
                    value = AnnotationInput(row.image_id, 'wd', 'tags', result.tags, model_name=result.actual_model,
                        params=result.params, job_id=job.job_id, cache_key=cache(result.params['actual']['provider']))
                    outcome = JobOutcome(row.image_id, 'done', (value,))
                except ServiceError as error:
                    outcome = self._failure(row.image_id, error)
                prepared[index] = (row, ref, outcome)
        # _persist rechecks bytes and the current WD annotation independently for
        # every image, including retained results after a pause/busy write.
        return prepared

    def _persist(self, context, job_id, pending):
        row, ref, outcome = pending
        stamp = None
        if outcome.kind == 'done' or outcome.reason == 'cache_hit':
            try:
                read_original(context.handle, row)
                stat = relative_path(context.handle.root, 'images/'+row.filename).stat()
                stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            except (FileNotFoundError, ServiceError) as error:
                missing = isinstance(error, FileNotFoundError) or error.code == 'image:file_missing'
                outcome = JobOutcome(row.image_id, 'skipped' if missing else 'failed', reason='file_missing' if missing else 'image:image_changed')
        submitted_digest = digest(outcome)
        def write(db):
            nonlocal submitted_digest
            job = jobs.get_job(db, job_id)
            existing = job.progress_json['outcomes'].get(str(row.image_id))
            if existing:
                if existing['digest'] != digest(outcome):
                    raise OutcomeConflict('Conflicting result')
                return job
            if job.status == 'canceled':
                return job
            fresh = images.get_image(db, row.image_id)
            value = outcome
            if fresh.file_missing:
                value = JobOutcome(row.image_id, 'skipped', reason='file_missing')
            elif value.kind == 'done' or value.reason == 'cache_hit':
                current = annotations.current_ref(db, row.image_id, 'wd', 'tags').annotation_id
                path = relative_path(context.handle.root, 'images/'+fresh.filename)
                try:
                    stat = path.stat()
                    present = (stat.st_size, stat.st_mtime_ns, stat.st_ino) == stamp
                except FileNotFoundError:
                    present = False
                if not path.is_file():
                    value = JobOutcome(row.image_id, 'skipped', reason='file_missing')
                elif current != ref or (fresh.sha256, fresh.file_size, fresh.filename) != (row.sha256, row.file_size, row.filename) or not present:
                    value = JobOutcome(row.image_id, 'failed', reason='job:input_changed')
            submitted_digest = digest(value)
            return commands.commit_job_outcome(db, job_id, value).value
        with self.lock:
            for attempt in range(3):
                try:
                    job = submit_write(context.handle, write).result()
                    break
                except CommitUnknown:
                    job = self.read(context, job_id)
                    receipt = job.progress_json['outcomes'].get(str(row.image_id))
                    if not receipt or receipt['digest'] != submitted_digest:
                        raise
                    break
                except Busy:
                    if attempt == 2:
                        raise
                    time.sleep(.05*(attempt+1))
            if str(row.image_id) in job.progress_json['outcomes']:
                self._emit(context, job, row.image_id)

    def invalidate(self, project_id):
        with self.lock:
            for key in list(self.pending):
                if key[0] == project_id:
                    self.pending.pop(key)
            for key in list(self.errors):
                if key[0] == project_id:
                    self.errors.pop(key)
