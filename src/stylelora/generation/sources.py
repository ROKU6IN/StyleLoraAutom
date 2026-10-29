from copy import deepcopy
from collections import Counter
import threading

from ..project_db import commands
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import sources
from ..project_db.types import ItemSeed, SourceDraft, SourcePatch
from ..project_db.validation import digest, primitive
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError
from ..tagging.trigger_validation import validate_trigger
from .profile import SIZES, build_request, locked_projection, validate_capabilities
from .types import Configuration, Draft


class SourceService:
    def __init__(self, runtime, *, driver=None):
        self.runtime, self.driver = runtime, driver
        self.lock = threading.RLock()
        self.reasons = {}

    def capabilities(self):
        from .presets import BUILTIN_IDS
        available = self.driver is not None or self.runtime.nai_direct.available()
        return {'drafts': True, 'handoff_preview': True, 'generation': available,
                'reason': None if available else 'generation:adapter_unavailable', 'size_presets': deepcopy(SIZES),
                'default_backend': 'nai_direct', 'default_model': 'nai-diffusion-5-full', 'profile_schema': Configuration.model_json_schema(),
                'presets_endpoint': '/api/presets', 'default_preset_id': BUILTIN_IDS['small'],
                'draft_creation_idempotency': 'session',
                'default_cost_policy': self.runtime.product_config.get()['cost_policy']}

    def driver_for(self, source):
        if source.config_json.get('backend') == 'nai_direct':
            return self.runtime.nai_direct.driver
        return self.driver

    def _source(self, db, identifier):
        source = sources.get_source(db, identifier)
        if source.type != 'nai':
            raise ServiceError(422, 'source:not_generation')
        return source

    def _view(self, context, identifier):
        return self.runtime.dataset.get_source(context, identifier)

    def _emit(self, context, identifier):
        value = self._view(context, identifier)
        self.runtime.emit('source.changed', {'source_id': identifier, 'status': value['status'], 'counts': value['counts']},
            project_id=context.project_id, resource={'kind': 'source', 'id': identifier}, version=value['version'])
        return value

    def create(self, context, body):
        if 'cost_policy' not in body:
            body = {**body, 'cost_policy': self.runtime.product_config.get()['cost_policy']}
        body = Draft.model_validate(body).model_dump()
        self._validate_draft(body)
        # Session request keys are handled by dispatch. A committed creation whose
        # callback fails can still be identified from this in-memory allocated ID.
        created = []
        def write(db):
            row = sources.create_source(db, SourceDraft(type='nai', **body))
            created.append(row.source_id)
            return row.source_id
        try:
            identifier = submit_write(context.handle, write).result()
        except Exception:
            if not created:
                raise
            with read_snapshot(context.handle) as db:
                try:
                    source = self._source(db, created[0])
                except Exception:
                    raise ServiceError(503, 'generation:create_unknown', outcome='unknown') from None
                actual = {key: primitive(source.config_json) if key == 'config' else getattr(source, key) for key in body}
                if actual != body:
                    raise ServiceError(503, 'generation:create_unknown', outcome='unknown') from None
            identifier = created[0]
        return self._emit(context, identifier)

    def update(self, context, identifier, body, expected):
        body = Draft.model_validate(body).model_dump()
        committed = []
        def write(db):
            source = self._source(db, identifier)
            Versions.require(expected, self.runtime.dataset.source_view(context, db, identifier)['version'])
            if source.locked or source.status != 'draft':
                raise ServiceError(409, 'generation:source_locked')
            self._validate_draft(body)
            result = sources.update_source(db, identifier, SourcePatch(**body))
            db.after_commit.append(lambda: committed.append(True))
            if result.changed:
                db.after_commit.append(lambda: self.runtime.versions.bump(self.runtime.dataset.key(context, 'source', identifier)))
        with self.lock:
            try:
                submit_write(context.handle, write).result()
                return self._emit(context, identifier)
            except Exception:
                if committed:
                    raise ServiceError(503, 'generation:update_outcome_unknown', outcome='unknown') from None
                raise

    @staticmethod
    def _validate_draft(body):
        if body['config']['backend'] == 'nai_direct':
            from ..backends.nai_direct.profile import capabilities
            validate_capabilities(body['config'], body['negative_prompt'], capabilities())

    def _gate(self, source):
        if source.config_json.get('backend') != 'nai_direct' and self.runtime.settings_leases.status()['blocked']:
            raise ServiceError(409, 'profile:restore_pending')
        driver = self.driver_for(source)
        if driver is None:
            raise ServiceError(503, 'generation:adapter_unavailable')
        return validate_capabilities(source.config_json, source.negative_prompt, driver.capabilities())

    def _trigger(self, context, source):
        from ..tagging.catalog import TagCatalog
        snapshot = self.runtime.catalog.snapshot()
        catalog = TagCatalog(snapshot.revision, dict(snapshot.entries), dict(snapshot.categories), snapshot.complete)
        return validate_trigger(context.handle.metadata.trigger_word, (source.style_prompt,), catalog=catalog)

    def preview(self, context, identifier, body, expected):
        frozen = self.runtime.prompts.seeds(body['task_id'], body['expected_task_revision'])
        items = tuple(ItemSeed(**row) for row in frozen['items'])
        with read_snapshot(context.handle) as db:
            source = self._source(db, identifier)
            version = self.runtime.dataset.source_view(context, db, identifier)['version']
        Versions.require(expected, version)
        if source.locked or source.status != 'draft':
            raise ServiceError(409, 'generation:source_locked')
        Configuration.model_validate(source.config_json)
        if source.target_count != len(items):
            raise ServiceError(422, 'generation:count_mismatch', details={'target_count': source.target_count, 'count': len(items)})
        trigger = self._trigger(context, source)
        blockers = []
        if trigger['status'] != 'valid':
            blockers.append('generation:trigger_invalid')
        capability_digest = None
        try:
            capability_digest = self._gate(source)
        except ServiceError as error:
            blockers.append(error.code)
        projection = {'source_digest': digest(locked_projection(source)), 'seeds_digest': digest(primitive(items)), 'count': len(items)}
        report = {**projection, 'source_id': identifier, 'source_version': version,
                  'can_start': not blockers, 'blockers': blockers, 'trigger_validation': trigger,
                  'cost_policy': source.cost_policy, 'estimated_anlas': None, 'capability_digest': capability_digest,
                  'aspect_counts': dict(Counter(item.aspect or source.config_json['default_aspect'] for item in items)),
                  'examples': [build_request(source, item) for item in items[:100]], 'examples_truncated': len(items) > 100}
        return self.runtime.previews.add(context.project_id, 'generation-handoff', report,
            {'source_id': identifier, 'source_version': version, 'items': items, 'projection': projection,
             'trigger_digest': trigger['digest'], 'capability_digest': capability_digest})

    def _reconcile(self, context, identifier, projection):
        with read_snapshot(context.handle) as db:
            source = self._source(db, identifier)
            items = [dict(row) for row in db.execute('SELECT seq,prompt,aspect FROM source_items WHERE source_id=? ORDER BY seq', (identifier,))]
            if not source.locked:
                if items:
                    raise ServiceError(409, 'generation:seed_conflict')
                return None
            actual = {'source_digest': digest(locked_projection(source)), 'seeds_digest': digest(items), 'count': len(items)}
            if actual != {key: projection[key] for key in actual}:
                raise ServiceError(409, 'generation:seed_conflict')
        return {**self._view(context, identifier), 'reconciled': True, 'dispatched': False}

    def reconcile(self, context, identifier, body):
        value = self._reconcile(context, identifier, body)
        if value is None:
            return {**self._view(context, identifier), 'reconciled': False, 'dispatched': False}
        return value

    def _pause(self, context, identifier, reason):
        def write(db):
            source = self._source(db, identifier)
            if source.status == 'running':
                commands.transition_source(db, identifier, expected='running', target='paused', reason=reason)
        submit_write(context.handle, write).result()
        self.reasons[(context.project_id, identifier)] = reason

    def start(self, context, identifier, body, expected):
        with self.lock:
            previous = self._reconcile(context, identifier, body)
            if previous is not None:
                return previous  # No task, preview, dictionaries, models or driver needed.
            report, frozen = self.runtime.previews.get(context.project_id, 'generation-handoff', body['preview_id'], body['digest'])
            if frozen['source_id'] != identifier or frozen['projection'] != {k: body[k] for k in ('source_digest', 'seeds_digest', 'count')}:
                raise ServiceError(409, 'preview:stale')
            with read_snapshot(context.handle) as db:
                source = self._source(db, identifier)
                version = self.runtime.dataset.source_view(context, db, identifier)['version']
            Versions.require(expected, version)
            Versions.require(frozen['source_version'], version)
            if self._gate(source) != frozen['capability_digest']:
                raise ServiceError(409, 'generation:capabilities_changed')
            trigger = self._trigger(context, source)
            if trigger['status'] != 'valid' or trigger['digest'] != frozen['trigger_digest']:
                raise ServiceError(409, 'generation:trigger_changed')
            driver = self.driver_for(source)
            if driver.is_active(context, identifier):
                raise ServiceError(409, 'source:in_flight')
            def write(db):
                Versions.require(frozen['source_version'], self.runtime.dataset.source_view(context, db, identifier)['version'])
                current = self._source(db, identifier)
                if digest(locked_projection(current)) != body['source_digest']:
                    raise ServiceError(409, 'generation:seed_conflict')
                current_trigger = self._trigger(context, current)
                if current_trigger['status'] != 'valid' or current_trigger['digest'] != frozen['trigger_digest']:
                    raise ServiceError(409, 'generation:trigger_changed')
                commands.start_source(db, identifier, frozen['items'])
            try:
                submit_write(context.handle, write).result()
            except Exception:
                # Read the complete persisted projection before deciding anything.
                reconciled = self._reconcile(context, identifier, body)
                if reconciled is None:
                    raise
                self._pause(context, identifier, 'generation:start_commit_reconciled')
                return {**self._emit(context, identifier), 'reconciled': True, 'dispatched': False}
            try:
                driver.dispatch(context, identifier)
            except Exception:
                self._pause(context, identifier, 'generation:dispatch_unknown')
                return {**self._emit(context, identifier), 'reconciled': False, 'dispatched': False}
            return {**self._emit(context, identifier), 'reconciled': False, 'dispatched': True}

    def control(self, context, identifier, action, expected):
        with self.lock:
            with read_snapshot(context.handle) as db:
                source = self._source(db, identifier)
                view = self.runtime.dataset.source_view(context, db, identifier)
            target = {'resume': 'running', 'pause': 'paused', 'cancel': 'canceled'}[action]
            driver = self.driver_for(source)
            if source.status == target:
                return view  # Never redispatch a repeated resume.
            Versions.require(expected, view['version'])
            if action == 'resume':
                if not source.locked or source.status != 'paused':
                    raise ServiceError(409, 'generation:state_conflict')
                self._gate(source)
                if driver.is_active(context, identifier):
                    raise ServiceError(409, 'source:in_flight')
            def write(db):
                Versions.require(expected, self.runtime.dataset.source_view(context, db, identifier)['version'])
                commands.transition_source(db, identifier, expected=source.status, target=target, reason='http:' + action)
            try:
                submit_write(context.handle, write).result()
            except Exception:
                if action == 'resume':
                    self._pause(context, identifier, 'generation:control_commit_unknown')
                raise
            if action == 'resume':
                try:
                    driver.dispatch(context, identifier)
                except Exception:
                    self._pause(context, identifier, 'generation:dispatch_unknown')
                else:
                    self.reasons.pop((context.project_id, identifier), None)
            elif driver:
                try:
                    driver.stop(context, identifier)
                except Exception:
                    self.reasons[(context.project_id, identifier)] = 'generation:stop_unknown'
            return self._emit(context, identifier)
