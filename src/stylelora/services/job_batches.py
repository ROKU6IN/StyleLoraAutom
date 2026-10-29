"""Persistent WD -> vision ordering, batch control and side-effect-free estimates."""
from dataclasses import replace
from types import SimpleNamespace

from ..project_db import commands
from ..project_db.cache import make_cache_key
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.errors import CommitUnknown
from ..project_db.repositories import annotations, images, jobs
from ..project_db.types import JobRequest, JobSnapshot, PageRequest, RatingValue, Scope
from ..project_db.validation import digest, primitive, relative_path, utc_now
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError, safe_error
from ..tagging.predictor import TaggerConfig, cache_parameters, model_name
from ..vision.evidence import read_evidence
from ..vision.executor import VisionEngine, snapshot
from ..vision.policy import VisionConfig, keyword_rating

PIPELINE = 'wd-vision-batch-v1'
HOLD = 'job:batch_paused'
TERMINAL = ('done', 'failed', 'canceled')


class BatchService:
    def __init__(self, runtime):
        self.runtime, self.lock = runtime, runtime.jobs.lock

    def service(self, executor):
        return self.runtime.jobs if executor == 'wd' else self.runtime.vision

    @staticmethod
    def rows(db, batch_id):
        return jobs.list_jobs(db, batch_id=batch_id, page=PageRequest(limit=3)).items

    def read(self, context, batch_id):
        with read_snapshot(context.handle) as db:
            values = self.rows(db, batch_id)
        if not values:
            raise ServiceError(404, 'job:batch_not_found')
        return values

    def version(self, context, values):
        return self.runtime.versions.get(f'{context.project_id}:batch:{values[0].batch_id}',
            [(row.job_id, row.status, row.updated_at, row.error, row.done_count, row.failed_count, row.skipped_count) for row in values])

    def view(self, context, values):
        first = next((row for row in values if row.status != 'done'), None)
        if first is None:
            state, reason = 'done', None
        elif first.status in ('failed', 'canceled'):
            state = 'blocked' if any(row.status not in TERMINAL for row in values) else first.status
            reason = 'job:dependency_blocked' if state == 'blocked' else first.error
        elif any(row.status == 'paused' or row.error == HOLD for row in values):
            state, reason = 'paused', HOLD if any(row.error == HOLD for row in values) else first.error
        else:
            state, reason = first.status, first.error
        return {'batch_id': values[0].batch_id, 'version': self.version(context, values), 'status': state,
            'reason': reason, 'image_count': max(row.total for row in values),
            'total': sum(row.total for row in values), 'done_count': sum(row.done_count for row in values),
            'failed_count': sum(row.failed_count for row in values), 'skipped_count': sum(row.skipped_count for row in values),
            'active_job_id': first.job_id if first and first.status not in TERMINAL else None,
            'items': [self.runtime.jobs.view(context, row) for row in values]}

    def get(self, context, batch_id):
        return self.view(context, self.read(context, batch_id))

    def retry_state(self, context, db, job_id):
        source = jobs.get_job(db, job_id)
        parents = self.rows(db, source.batch_id)
        ids = tuple(sorted(int(key) for key, value in source.progress_json.get('outcomes', {}).items() if value['kind'] == 'failed'))
        reason = 'job:retry_not_ready' if any(row.status not in TERMINAL for row in parents) else 'job:no_failed_images' if not ids else None
        fingerprint = digest([context.project_id, source.job_id, source.config_snapshot_json, ids,
            [(row.job_id, row.status, row.updated_at) for row in parents]])
        return source, ids, reason, fingerprint

    def retry_preview(self, context, job_id):
        with read_snapshot(context.handle) as db:
            source, ids, reason, fingerprint = self.retry_state(context, db, job_id)
        if not reason:
            try:
                self.service(source.executor)._validate_resume(context, source)
            except Exception as error:
                reason = safe_error(error).code
        return {'source_job_id': job_id, 'executor': source.executor, 'image_count': len(ids),
            'model': source.config_snapshot_json.get('model'), 'mode': 'all', 'preview_digest': fingerprint,
            'available': reason is None, 'reason': reason}

    def retry(self, context, job_id, body):
        """A new single-stage job; never reopen or clear the source job's ledger."""
        origin = {'job_id': job_id, 'preview_digest': body['preview_digest']}
        with self.lock:
            with read_snapshot(context.handle) as db:
                previous = self.rows(db, body['batch_id'])
                if previous:
                    if len(previous) != 1 or previous[0].config_snapshot_json.get('params', {}).get('retry_origin') != origin:
                        raise ServiceError(409, 'job:batch_conflict')
                    return [self.runtime.jobs.view(context, previous[0])]
                source, ids, reason, fingerprint = self.retry_state(context, db, job_id)
            if reason:
                raise ServiceError(409, reason)
            if fingerprint != body['preview_digest']:
                raise ServiceError(409, 'job:retry_stale')
            self.service(source.executor)._validate_resume(context, source)
            raw = source.config_snapshot_json
            frozen = JobSnapshot(**{**raw, 'target_types': tuple(raw['target_types']),
                'params': {**raw['params'], 'retry_origin': origin}})
            def write(db):
                _, current_ids, current_reason, current_digest = self.retry_state(context, db, job_id)
                if current_reason or current_digest != fingerprint or current_ids != ids:
                    raise ServiceError(409, 'job:retry_stale')
                member = jobs.create_batch(db, body['batch_id'], (JobRequest(source.executor, 'all', Scope(selected_ids=ids)),))[0]
                return jobs.freeze_job(db, member.job_id, frozen, image_ids=ids).value
            try:
                row = submit_write(context.handle, write).result()
            except CommitUnknown:
                rows = self.read(context, body['batch_id'])
                if len(rows) != 1 or rows[0].config_snapshot_json != primitive(frozen):
                    raise
                row = rows[0]
            self.runtime.jobs._emit(context, row)
            self.service(row.executor)._schedule(context, row.job_id)
            return [self.runtime.jobs.get(context, row.job_id)]

    def list(self, context, *, limit=20, cursor=None):
        identity = digest([context.project_id, 'job-batches'])
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        page = PageRequest(state['last'] if state else 0, limit, state['upper'] if state else None)
        with read_snapshot(context.handle) as db:
            heads = jobs.list_batch_heads(db, page=page)
            values = [self.view(context, self.rows(db, row.batch_id)) for row in heads.items]
        return {'items': values, 'limit': limit, 'upper_id': heads.upper_id, 'has_more': heads.next_after_id is not None,
            'next_cursor': self.runtime.cursors.encode({'identity': identity, 'upper': heads.upper_id, 'last': heads.next_after_id}) if heads.next_after_id is not None else None}

    def snapshots(self, context, body):
        combined = len(body['executors']) == 2
        values, wd = {}, None
        if 'wd' in body['executors']:
            config = TaggerConfig.model_validate(body['config']['wd'] if combined else body['config'])
            manifest, version = self.runtime.jobs.require_model(config)
            wd = config, manifest, version
            values['wd'] = JobSnapshot(model_name(manifest), role='wd', protocol='onnx', params={
                'config': config.model_dump(), 'runtime_version': version, 'pipeline_version': 'wd-runner-v2'})
        if 'llm_vision' in body['executors']:
            config = VisionConfig.model_validate(body['config']['llm_vision'] if combined else body['config'])
            values['llm_vision'] = snapshot(self.runtime, context, config)
        if combined:
            values = {name: replace(value, params={**value.params, 'batch_pipeline': PIPELINE}) for name, value in values.items()}
        return values, wd

    def create(self, context, body):
        scope = self.runtime.jobs._scope(body['scope'], body['mode'])
        with self.lock:
            with read_snapshot(context.handle) as db:
                previous = self.rows(db, body['batch_id'])
            if previous:
                if (len(previous) != 2 or [row.executor for row in previous] != ['wd', 'llm_vision']
                        or any(row.mode != body['mode'] or row.scope_json != jobs.scope_json(scope)
                               or row.config_snapshot_json.get('params', {}).get('config') != body['config'][row.executor]
                               for row in previous)):
                    raise ServiceError(409, 'job:batch_conflict')
                return [self.runtime.jobs.view(context, row) for row in previous]
            frozen, _ = self.snapshots(context, body)
            def write(db):
                ids = self.runtime.jobs._resolve(db, scope)
                self.runtime.jobs.check_preview_scope(context, body, scope, ids)
                members = jobs.create_batch(db, body['batch_id'], tuple(JobRequest(name, body['mode'], scope) for name in ('wd', 'llm_vision')))
                for member in members:
                    jobs.freeze_job(db, member.job_id, frozen[member.executor], image_ids=ids, start=False)
                commands.transition_job(db, members[0].job_id, expected='pending', target='running')
                return self.rows(db, body['batch_id'])
            try:
                values = submit_write(context.handle, write).result()
            except CommitUnknown:
                values = self.read(context, body['batch_id'])
                if len(values) != 2 or any(row.config_snapshot_json != primitive(frozen[row.executor]) for row in values):
                    raise
            for row in values:
                self.runtime.jobs._emit(context, row)
            self.runtime.jobs._schedule(context, values[0].job_id)
            return [self.runtime.jobs.get(context, row.job_id) for row in values]

    def advance(self, context, batch_id):
        """Only an accepted, live predecessor completion may auto-start stage two."""
        with self.lock:
            if context.closing or self.runtime.draining:
                return
            values = self.read(context, batch_id)
            if len(values) != 2 or values[0].status != 'done':
                return
            following = values[1]
            if (following.status != 'pending' or following.error
                    or following.config_snapshot_json.get('params', {}).get('batch_pipeline') != PIPELINE):
                return
            try:
                self.service(following.executor)._validate_resume(context, following)
                def write(db):
                    current = jobs.get_job(db, following.job_id)
                    if current.status != 'pending' or current.error:
                        return current
                    return commands.transition_job(db, current.job_id, expected='pending', target='running').value
                try:
                    started = submit_write(context.handle, write).result()
                except CommitUnknown:
                    started = self.runtime.jobs.read(context, following.job_id)
                    if started.status != 'running':
                        raise
                self.runtime.jobs._emit(context, started)
                if started.status == 'running':
                    self.service(started.executor)._schedule(context, started.job_id)
            except Exception as error:
                code = safe_error(error).code
                self.runtime.jobs.errors[(context.project_id, following.job_id)] = code
                try:
                    with read_snapshot(context.handle) as db:
                        current = jobs.get_job(db, following.job_id)
                    if current.status == 'pending':
                        current = submit_write(context.handle, lambda db: jobs.wait_reason(db, following.job_id, code).value).result()
                    self.runtime.jobs._emit(context, current)
                except Exception:
                    pass  # The next GET still exposes the in-memory diagnosis.

    def control(self, context, batch_id, action, expected):
        with self.lock:
            values = self.read(context, batch_id)
            Versions.require(expected, self.version(context, values))
            unfinished = [row for row in values if row.status not in TERMINAL]
            if not unfinished:
                if action == 'cancel' and any(row.status == 'canceled' for row in values):
                    return self.view(context, values)
                raise ServiceError(409, 'job:state_conflict')
            chosen = unfinished[0]
            if action == 'resume':
                if any(row.status != 'done' for row in values if row.job_id < chosen.job_id):
                    raise ServiceError(409, 'job:dependency_blocked')
                self.service(chosen.executor)._validate_resume(context, chosen)
            intended = []
            def write(db):
                current = self.rows(db, batch_id)
                Versions.require(expected, self.version(context, current))
                for row in current:
                    if row.status in TERMINAL:
                        continue
                    if action == 'cancel':
                        commands.transition_job(db, row.job_id, expected=row.status, target='canceled')
                    elif action == 'pause':
                        if row.status == 'running':
                            commands.transition_job(db, row.job_id, expected='running', target='paused')
                        elif row.status == 'pending':
                            jobs.wait_reason(db, row.job_id, HOLD)
                    elif row.job_id == chosen.job_id and row.status in ('paused', 'pending'):
                        commands.transition_job(db, row.job_id, expected=row.status, target='running')
                    elif row.status == 'pending' and row.error == HOLD:
                        jobs.wait_reason(db, row.job_id, None)
                result = self.rows(db, batch_id)
                intended.extend((row.job_id, row.status, row.error) for row in result)
                return result
            try:
                values = submit_write(context.handle, write).result()
            except CommitUnknown:
                values = self.read(context, batch_id)
                if not intended or [(row.job_id, row.status, row.error) for row in values] != intended:
                    raise
            for row in values:
                service = self.service(row.executor)
                key = context.project_id, row.job_id
                if action == 'cancel':
                    run = service.active.get(key)
                    if run:
                        run.canceled.set()
                    service.pending.pop(key, None)
                    if row.executor == 'llm_vision':
                        service._clear_transient(*key)
                self.runtime.jobs._emit(context, row)
            if action == 'resume':
                self.service(chosen.executor)._schedule(context, chosen.job_id)
            return self.get(context, batch_id)

    def preview(self, context, body):
        """Count metadata/current caches only; never run R5, infer, or set ratings."""
        scope = self.runtime.jobs._scope(body['scope'], body['mode'])
        frozen, wd = self.snapshots(context, body)
        engine = VisionEngine(self.runtime, context, SimpleNamespace(job_id=0, mode=body['mode'],
            config_snapshot_json=primitive(frozen['llm_vision']))) if 'llm_vision' in frozen else None
        counts = {name: {'executor': name, 'total': 0, 'cache_hit': 0, 'needs_execution': 0,
            'file_missing': 0, 'waiting_for_wd': 0, 'role_empty': 0, 'rating_unresolved': 0} for name in frozen}
        overlaps = []
        with read_snapshot(context.handle) as db:
            ids = self.runtime.jobs._resolve(db, scope)
            for image_id in ids:
                evidence = read_evidence(db, image_id)
                row = evidence.image
                missing = row.file_missing or not relative_path(context.handle.root, 'images/'+row.filename).is_file()
                wd_changes = False
                for name, count in counts.items():
                    count['total'] += 1
                    if missing:
                        count['file_missing'] += 1
                        continue
                    if name == 'wd':
                        config, manifest, version = wd
                        current = annotations.get_current(db, image_id, 'wd', 'tags')
                        providers = ('cpu', 'cuda') if config.provider == 'auto' else ('cpu',)
                        keys = {make_cache_key(source='wd', type='tags', image_sha256=row.sha256, model=model_name(manifest),
                            protocol='onnx', skill_hash='none:wd-v1', actual_params=cache_parameters(manifest, config, version, provider),
                            route='wd', keyword_hash='none:wd-v1') for provider in providers}
                        cached = body['mode'] != 'all' and current and current.cache_key in keys
                        count['cache_hit' if cached else 'needs_execution'] += 1
                        wd_changes = not cached
                    elif wd_changes:
                        count['waiting_for_wd'] += 1
                    else:
                        rating = images.rating_value(row)
                        found = keyword_rating(evidence.tags, engine.policy) if not rating or not rating.verified else None
                        if found:
                            rating = RatingValue(found, 'keyword', True, utc_now())
                        decision = engine.decision(rating, engine.config.fallback)
                        if engine.covered(evidence, decision):
                            count['cache_hit'] += 1
                        elif engine.config.use_rating_model and (not rating or not rating.verified):
                            count['needs_execution'] += 1
                            count['rating_unresolved'] += 1
                        elif not engine.bindings.get('vision_nsfw' if decision['route'] == 'nsfw' else 'vision'):
                            count['role_empty'] += 1
                        else:
                            count['needs_execution'] += 1
            # Explicit warning, never a silent queue behind an overlapping job.
            selected = set(ids)
            after, upper = 0, None
            while True:
                page = jobs.list_jobs(db, page=PageRequest(after, 1000, upper))
                for row in page.items:
                    if row.status not in TERMINAL and row.executor in frozen and selected.intersection(row.progress_json.get('image_ids', [])):
                        overlaps.append({'job_id': row.job_id, 'batch_id': row.batch_id, 'executor': row.executor, 'status': row.status})
                if page.next_after_id is None:
                    break
                after, upper = page.next_after_id, page.upper_id
        return {'items': list(counts.values()), 'image_count': len(ids), 'overlapping_jobs': overlaps,
            'scope_digest': self.runtime.jobs.scope_digest(context, body['mode'], scope, ids),
            'estimate_only': True, 'revalidates_on_start': True,
            'notes': ['metadata_cache_only', 'downstream_rechecked_after_wd', 'no_paid_requests']}
