"""Compare a saved source to reviewed Launcher reads; never authorize generation.

Mappings derive from official v4.3.1 api_constants.dart. A settings comparison
does not prove prepare's effective parameters, inherited references or batch size.
"""
import asyncio
from copy import deepcopy

from ..project_db.connection import read_snapshot
from ..project_db.validation import digest
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError
from ..runtime.supervisor import TERMINAL
from .profile import locked_projection
from .types import Configuration

HIGH_MODEL = 'nai-diffusion-5-full'
MEDIUM_MODEL = 'nai-diffusion-5-full-medium'
PROFILE_FIELDS = ('model', 'effort', 'sampler', 'steps', 'scale', 'cfg_rescale',
                  'noise_schedule', 'uc_preset', 'quality_toggle', 'smea', 'smea_dyn',
                  'variety_plus', 'decrisp')
# These facts are not visible in the two reviewed tools. Never infer absence.
UNVERIFIED = ('effective_parameters', 'inherited_references', 'batch_size',
              'resolution_support', 'atomic_settings_update', 'durable_attempt_outcome')


def compare_source(source, inspection):
    config = Configuration.model_validate(source.config_json).model_dump()
    result = {'status': 'unavailable', 'settings_match': None, 'generation_enabled': False,
              'reason': inspection['reason'], 'checked_at': inspection['checked_at'],
              'launcher_version': inspection.get('version'), 'warnings': inspection['warnings'],
              'fields': [], 'issues': [], 'unverified': list(UNVERIFIED),
              'request_sizes': config['sizes'], 'default_aspect': config['default_aspect']}
    if inspection['status'] != 'ready':
        return result
    wanted = config['profile']
    settings = inspection['settings']
    expected = deepcopy(wanted)
    # Local None denotes "no preset"; Launcher requires integer 3.
    expected['uc_preset'] = 3
    selector = wanted['model']
    if selector in (HIGH_MODEL, MEDIUM_MODEL):
        selector = HIGH_MODEL
        if wanted['effort'] in ('high', 'medium'):
            expected['model'] = HIGH_MODEL if wanted['effort'] == 'high' else MEDIUM_MODEL
    models = {model['id']: model['efforts'] for model in settings['available_models']}
    issues = []
    if selector not in models:
        issues.append('launcher_check:model_unlisted')
    if wanted['effort'] not in models.get(selector, []):
        issues.append('launcher_check:effort_unsupported')
    locks = settings['locked_by_effort']
    fields = [{'key': key, 'requested': wanted[key], 'expected': expected[key],
               'observed': settings[key], 'matches': expected[key] == settings[key],
               'locked': key in locks} for key in PROFILE_FIELDS]
    matched = all(field['matches'] for field in fields)
    if not matched:
        issues.append('launcher_check:settings_mismatch')
    if source.negative_prompt.strip() and 'negative_prompt' in locks:
        issues.append('launcher_check:negative_locked')
    # Queue counts/progress are only a snapshot, never attempt settlement proof.
    activity = inspection['activity']
    if activity['generation']['status'] == 'generating' or activity['queue']['pending'] > 0:
        issues.append('launcher_check:activity_present')
    result.update(status='checked', settings_match=matched, fields=fields, issues=issues)
    return result


class LauncherCheckService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.entries = {}

    @property
    def coordinator(self):
        return self.runtime.generation_sources

    def _source(self, context, source_id):
        with read_snapshot(context.handle) as db:
            source = self.coordinator._source(db, source_id)
            if source.config_json.get('backend', 'nai_launcher') != 'nai_launcher':
                raise ServiceError(422, 'source:backend_mismatch')
            version = self.runtime.dataset.source_view(context, db, source_id)['version']
        return source, version

    def view(self, context, source_id):
        with self.coordinator.lock:
            _, version = self._source(context, source_id)
            entry = self.entries.get((context.project_id, source_id), {})
            result = deepcopy(entry.get('result'))
            return {'source_id': source_id, 'source_version': version,
                    'operation_id': entry.get('operation_id'), 'result': result,
                    'stale': bool(result and result['source_version'] != version)}

    def start(self, context, source_id, expected):
        with self.coordinator.lock:
            source, version = self._source(context, source_id)
            Versions.require(expected, version)
            Configuration.model_validate(source.config_json)
            key = (context.project_id, source_id)
            entry = self.entries.get(key, {})
            if entry.get('operation_id'):
                current = self.runtime.supervisor.get(entry['operation_id'])
                if current['status'] not in TERMINAL:
                    if entry['source_version'] != version:
                        raise ServiceError(409, 'launcher_check:busy')
                    return current
            operation = self.runtime.supervisor.submit('generation.launcher_check',
                lambda task: self._run(context, source, version, task), context=context)
            self.entries[key] = {**entry, 'operation_id': operation['operation_id'], 'source_version': version}
            return operation

    def _run(self, context, source, version, task):
        # A queued check must not contact Launcher for a source already changed.
        with self.coordinator.lock:
            self._require_current(context, source.source_id, version, task)
        inspection = asyncio.run(self.runtime.backends.read_inspection(task))
        result = compare_source(source, inspection)
        result.update(source_id=source.source_id, source_version=version,
                      source_digest=digest(locked_projection(source)))
        with self.coordinator.lock:
            self._require_current(context, source.source_id, version, task)
            self.entries[(context.project_id, source.source_id)]['result'] = result
        return deepcopy(result)

    def _require_current(self, context, source_id, version, task):
        if task.cancel.is_set():
            raise ServiceError(409, 'operation:canceled')
        if context.closing:
            raise ServiceError(409, 'project:closing')
        _, current = self._source(context, source_id)
        if version != current:
            raise ServiceError(409, 'launcher_check:source_changed')

    def invalidate(self, project_id):
        with self.coordinator.lock:
            self.entries = {key: entry for key, entry in self.entries.items() if key[0] != project_id}
