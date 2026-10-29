from copy import deepcopy
import itertools
import secrets
import sys
import threading

from ..project_db.validation import digest, encode, utc_now
from ..runtime.contracts import uuid_string
from ..runtime.errors import ServiceError
from .dictionaries import Dictionaries
from .coverage import CoveragePlans
from .sampling import SamplingStopped, cell_id, sample
from .storage import ProductFiles
from .types import DictionaryFile, Plan, SamplingRequest
from .composer import dependencies
from .execution import RefinedTasks
from .append import TaskAppend
from .management import TaskManagement
from .review import TaskReview, ReviewRecord
from .matrix import CoverageViews, TaskMatrix, matrix


class PromptProduction(RefinedTasks, TaskAppend, TaskManagement, TaskReview, TaskMatrix):
    def __init__(self, runtime, directory=None):
        self.runtime = runtime
        self.files = ProductFiles(directory)
        self.dictionaries = Dictionaries(runtime, self.files)
        self.coverage_plans = CoveragePlans(runtime, self.files)
        self.lock = self.files.lock
        self.active, self.generations, self.task_keys, self.unavailable = {}, {}, {}, {}
        self.preview_gate = threading.BoundedSemaphore(1)
        self.coverage_views = CoverageViews()
        self.pending_commits, self.refinement_busy = {}, set()
        self.cleanup_keys = {}
        for identifier in self.files.ids('prompt_tombstones'):
            try:
                marker = self.tombstone(identifier)
                if marker['request_key'] in self.task_keys and self.task_keys[marker['request_key']] != identifier:
                    raise ServiceError(503, 'prompt:corrupt_snapshot')
                if marker['cleanup_key'] in self.cleanup_keys and self.cleanup_keys[marker['cleanup_key']] != marker['cleanup_digest']:
                    raise ServiceError(503, 'prompt:corrupt_snapshot')
                self.task_keys[marker['request_key']] = identifier
                self.cleanup_keys[marker['cleanup_key']] = marker['cleanup_digest']
            except ServiceError as error:
                self.unavailable[identifier] = error.code
        # Restart never resends an in-flight remote request. Unsent work needs
        # explicit resume and uses the frozen input, never today's dictionaries.
        for identifier in self.files.ids('prompt_tasks'):
            try:
                value = self.load(identifier)
            except ServiceError as error:
                if error.code == 'prompt:task_deleted':
                    continue
                self.unavailable[identifier] = error.code
                continue  # Preserve the damaged file; do not replay or block other tasks.
            if value['request_key'] in self.task_keys:
                self.unavailable[identifier] = 'prompt:corrupt_snapshot'
                continue
            self.task_keys[value['request_key']] = identifier
            if value['status'] in ('pending', 'running'):
                value.update(status='paused', reason='prompt:interrupted', revision=value['revision'] + 1)
                self.files.put('prompt_tasks', identifier, value)
            if value.get('execution'):
                changed = False
                for row in value['execution']['outcomes']:
                    if row['status'] == 'running':
                        row.update(status='failed', reason='llm_outcome_unknown')
                        changed = True
                if changed:
                    value.update(revision=value['revision'] + 1)
                    self.files.put('prompt_tasks', identifier, value)

    def capabilities(self):
        return {'local_sampling': True, 'coverage': True, 'optimize': True, 'rating_model': True,
                'coverage_plans': True, 'coverage_template_formats': ['json', 'yaml'],
                'manual_prompt_review': True,
                'coverage_matrix_details': True,
                'coverage_editor': True,
                'library_deletion': True,
                'dictionary_export_formats': ['json', 'csv', 'txt'], 'feature_txt_archive': True,
                'import_row_selection': True,
                'task_cleanup': True, 'partial_results_export': True, 'result_formats': ['json', 'csv', 'txt'],
                'launcher': False, 'automatic_append': True, 'candidate_limit': 10000,
                'search_nodes': 100000, 'max_count': 10000, 'formats': ['json', 'csv', 'txt']}

    def freeze(self, request):
        request = SamplingRequest.model_validate(request).model_dump()
        if request['live_features'] or request['launcher_selection'] is not None:
            raise ServiceError(503, 'launcher:unsupported_schema')
        if request['plan']['random_seed'] is None:
            # A valid uint64 that also survives a browser's JSON number round trip.
            request['plan']['random_seed'] = secrets.randbits(53)
        with self.lock:
            dictionaries = []
            for key, kind in (('prompt_dictionary_ids', 'prompts'), ('feature_dictionary_ids', 'features')):
                request[key].sort()
                for identifier in request[key]:
                    value = self.dictionaries.get(identifier)
                    if value['kind'] != kind:
                        raise ServiceError(422, 'dictionary:kind_mismatch')
                    value.pop('version')
                    dictionaries.append(value)
            catalog = self.runtime.catalog.snapshot()
            plan = request['plan']
            names = plan['primary_dimensions']
            old_values = deepcopy(plan['cell_values'])
            for name, values in plan['cell_values'].items():
                plan['cell_values'][name] = [catalog.entries.get(tag, tag) for tag in values]
            weights = {}
            for values in itertools.product(*(old_values[name] for name in names)):
                old = cell_id(names, values)
                if old in plan['cell_weights']:
                    weights[cell_id(names, [catalog.entries.get(tag, tag) for tag in values])] = plan['cell_weights'][old]
            if len(weights) != len(plan['cell_weights']):
                raise ServiceError(422, 'coverage:invalid_cell_weight')
            plan['cell_weights'] = weights
            for constraint in plan['constraints']:
                constraint['tag'] = catalog.entries.get(constraint['tag'], constraint['tag'])
            for predicate in plan['exclude']:
                for key in ('all_tags', 'none_tags'):
                    predicate[key] = [catalog.entries.get(tag, tag) for tag in predicate[key]]
            request['plan'] = Plan.model_validate(plan).model_dump()
            policy = self.runtime.vision.policy.view()
            policy = {key: policy[key] for key in ('keywords', 'blocked_terms')}
            snapshot = {'schema_version': 1, 'request': request, 'dictionaries': dictionaries,
                    'catalog': {'revision': catalog.revision, 'entries': dict(catalog.entries),
                                'categories': dict(catalog.categories)}, 'policy': policy,
                    'python_version': list(sys.version_info[:3]), 'algorithm_version': 'coverage-v1', 'content_gate': 'r1-v1'}
            if request['optimize'] or request['fence']['use_rating_model'] or request['append_target_id'] is not None:
                snapshot['composer'] = dependencies(self.runtime)
                if request['append_target_id']:
                    target = self.dictionaries.get(request['append_target_id'])
                    if target['kind'] != 'prompts':
                        raise ServiceError(422, 'dictionary:kind_mismatch')
                    snapshot['append_target'] = {k: target[k] for k in ('dictionary_id', 'version')}
            if len(encode(snapshot).encode('utf-8')) > 32 * 1024 * 1024:
                raise ServiceError(413, 'prompt:storage_limit')
            return snapshot

    def preview(self, request):
        if not self.preview_gate.acquire(blocking=False):
            raise ServiceError(429, 'prompt:preview_busy')
        try:
            snapshot = self.freeze(request)
            def stop():
                if self.runtime.draining:
                    raise ServiceError(503, 'process:draining')
            report = sample(snapshot, stop=stop)
            result = self.report(report, snapshot['request'])
            result.update(request=snapshot['request'], preview_digest=digest(snapshot),
                          examples=report['outcomes'][:100], examples_truncated=len(report['outcomes']) > 100)
            result['coverage_view'] = self.coverage_views.add(report)
            result['coverage_view_reason'] = None if result['coverage_view'] else 'coverage:detail_limit'
            if 'composer' in snapshot:
                result['refinement'] = {'roles': snapshot['composer']['roles'], 'nsfw_template': 'R1-nsfw' in snapshot['composer']['templates'],
                    'max_candidates': min(10000, snapshot['request']['plan']['target_count'] * 3),
                    'max_semantic_calls_per_candidate': 3, 'max_transport_attempts_per_call': 3,
                    'rating_enabled': snapshot['request']['fence']['use_rating_model'],
                    'automatic_append': snapshot.get('append_target'), 'external_calls': 'on_task_creation_only'}
            return result
        finally:
            self.preview_gate.release()

    @staticmethod
    def report(value, request=None):
        result = {key: deepcopy(content) for key, content in value.items() if key not in ('items', 'outcomes')}
        if request is not None:
            result['matrix'] = matrix(request)
        return result

    def load(self, identifier):
        if self.tombstone(identifier) is not None:
            raise ServiceError(410, 'prompt:task_deleted')
        value = self.files.get('prompt_tasks', identifier)
        try:
            expected = {'schema_version', 'task_id', 'revision', 'status', 'ready', 'reason', 'snapshot', 'result',
                        'created_at', 'request_key', 'request_digest', 'generation'}
            if value.get('schema_version') == 2:
                expected |= {'execution', 'appends'}
            if set(value) != expected:
                raise ValueError()
            if type(value['schema_version']) is not int or value['schema_version'] not in (1, 2) or value['task_id'] != identifier:
                raise ValueError()
            if type(value['revision']) is not int or value['revision'] < 1 or type(value['generation']) is not int:
                raise ValueError()
            if value['status'] not in ('pending', 'running', 'paused', 'done', 'failed', 'canceled') or type(value['ready']) is not bool:
                raise ValueError()
            uuid_string(value['request_key'])
            snapshot = value['snapshot']
            expected_snapshot = {'schema_version', 'request', 'dictionaries', 'catalog', 'policy', 'python_version', 'algorithm_version'}
            if snapshot.get('content_gate') == 'r1-v1':
                expected_snapshot.add('content_gate')
            if 'composer' in snapshot and value['schema_version'] == 2:
                expected_snapshot |= {'composer'}
                if snapshot['request']['append_target_id']:
                    expected_snapshot |= {'append_target'}
            if set(snapshot) != expected_snapshot:
                raise ValueError()
            if value.get('execution') is not None:
                execution = value['execution']
                if set(execution) != {'round', 'outcomes', 'selected', 'deficits_reason', 'append_error'} or not 0 <= execution['round'] <= 3:
                    raise ValueError()
                indices = [r['index'] for r in execution['outcomes']]
                if len(indices) != len(set(indices)) or any(type(i) is not int or not 0 <= i < 10000 for i in indices):
                    raise ValueError()
                for row in execution['outcomes']:
                    if row['status'] not in ('pending', 'running', 'ok', 'skipped', 'failed', 'fenced', 'review_required'):
                        raise ValueError()
                    if set(row) - {'index', 'status', 'phase', 'reason', 'input', 'result', 'reply', 'rating', 'verified', 'repair_used', 'review_candidate', 'review'}:
                        raise ValueError()
                    if row['phase'] not in ('new', 'rating', 'rated', 'r1') or type(row.get('repair_used', 0)) is not int or row.get('repair_used', 0) not in (0, 1):
                        raise ValueError()
                    for candidate in (row['input'], row.get('result'), row.get('review_candidate')):
                        if candidate is None:
                            continue
                        if set(candidate) - {'entry', 'rating', 'verified', 'route', 'source', 'cell', 'pool_index'}:
                            raise ValueError()
                        from .types import Entry
                        Entry.model_validate(candidate['entry'])
                        if type(candidate['verified']) is not bool or candidate['rating'] not in (None, 'general', 'sensitive', 'questionable', 'explicit'):
                            raise ValueError()
                        if candidate['pool_index'] != row['index'] or candidate['source'] not in ('dictionary_pool', 'feature_pool'):
                            raise ValueError()
                    if row['status'] in ('ok', 'skipped') and row.get('result') is None:
                        raise ValueError()
                    if 'review' in row:
                        reviewed = ReviewRecord.model_validate(row['review'])
                        if (row['status'] != 'ok' or row['reason'] != 'manual_review' or not row['result']['verified']
                                or reviewed.result.task_id != identifier or reviewed.result.index != row['index']
                                or reviewed.result.revision > value['revision'] or reviewed.entry_digest != digest(row['result']['entry'])):
                            raise ValueError()
                review_keys = [r['review']['request_key'] for r in execution['outcomes'] if 'review' in r]
                if len(review_keys) != len(set(review_keys)):
                    raise ValueError()
            if 'composer' in snapshot:
                frozen = snapshot['composer']
                if set(frozen) != {'version', 'roles', 'templates'} or frozen['version'] not in ('composer-v1', 'composer-v2'):
                    raise ValueError()
                templates = {'R0', 'R1-sfw', 'R5'} if frozen['version'] == 'composer-v1' else {'R0-routing-v2', 'R1-sfw', 'R1-nsfw', 'R5'}
                if set(frozen['roles']) != {'prompt', 'prompt_nsfw', 'rating'} or set(frozen['templates']) != templates:
                    raise ValueError()
                for binding in frozen['roles'].values():
                    if binding is not None:
                        if set(binding) != {'endpoint_id', 'model', 'protocol', 'config_digest'}:
                            raise ValueError()
                        uuid_string(binding['endpoint_id'])
                        if binding['protocol'] not in ('openai_chat', 'openai_responses', 'anthropic_messages') or not isinstance(binding['model'], str) or len(binding['config_digest']) != 64:
                            raise ValueError()
                for name, template in frozen['templates'].items():
                    if set(template) != {'id', 'hashes', 'system_text', 'response_schema'} or template['id'] != name or set(template['hashes']) != {'prompt', 'schema'}:
                        raise ValueError()
            if value['schema_version'] == 2:
                if not isinstance(value['appends'], list) or len(value['appends']) > 4096:
                    raise ValueError()
                for marker in value['appends']:
                    if set(marker) != {'request_key', 'target_id', 'result'}:
                        raise ValueError()
                    uuid_string(marker['request_key'])
                    uuid_string(marker['target_id'])
            SamplingRequest.model_validate(snapshot['request'])
            if type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1 or snapshot['algorithm_version'] != 'coverage-v1':
                raise ValueError()
            for dictionary in snapshot['dictionaries']:
                DictionaryFile.model_validate(dictionary)
            if set(snapshot['catalog']) != {'revision', 'entries', 'categories'}:
                raise ValueError()
            from ..vision.policy import PolicyInput
            PolicyInput.model_validate(snapshot['policy'])
            if value['ready'] and (value['status'] != 'done' or not value['result']['ready']):
                raise ValueError()
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(503, 'prompt:corrupt_snapshot') from None
        return value

    def view(self, value):
        return {key: deepcopy(value[key]) for key in ('task_id', 'revision', 'status', 'ready', 'reason', 'created_at')} | {
            'random_seed': value['snapshot']['request']['plan']['random_seed'],
            'requested': value['snapshot']['request']['plan']['target_count'],
            'coverage': self.report(value['result'], value['snapshot']['request']) if value['result'] is not None else None,
            'progress': self.progress(value), 'appends': deepcopy(value.get('appends', [])),
            'append_error': value['execution']['append_error'] if value.get('execution') else None,
            'version': digest([value['task_id'], value['revision']])}

    @staticmethod
    def progress(value):
        from collections import Counter
        execution = value.get('execution')
        return {'round': execution['round'], 'counts': dict(Counter(r['status'] for r in execution['outcomes'])),
                'selected': execution['selected'], 'deficits_reason': execution['deficits_reason']} if execution else None

    def get(self, identifier):
        with self.lock:
            if identifier in self.pending_commits and identifier not in self.refinement_busy:
                try:
                    self.files.put('prompt_tasks', identifier, self.pending_commits[identifier])
                    self.pending_commits.pop(identifier)
                except ServiceError:
                    pending = self.pending_commits[identifier]
                    return {**self.view(pending), **self.management_view(pending)}
            value = self.load(identifier)
            operation_id = self.active.get(identifier)
            if value['status'] == 'running' and operation_id:
                operation = self.runtime.supervisor.get(operation_id)
                if operation['status'] in ('failed', 'canceled', 'interrupted'):
                    value.update(status='paused', reason='prompt:interrupted', ready=False, revision=value['revision'] + 1)
                    self.files.put('prompt_tasks', identifier, value)
                    self.generations[identifier] = (value['generation'], 'paused')
                    self.emit(value)
            return {**self.view(value), **self.management_view(value)}

    def list(self):
        with self.lock:
            values = []
            for identifier in self.files.ids('prompt_tasks'):
                try:
                    values.append(self.get(identifier))
                except ServiceError as error:
                    if error.code == 'prompt:task_deleted':
                        continue
                    values.append({'task_id': identifier, 'status': 'failed', 'ready': False, 'revision': 0,
                                   'reason': error.code, 'created_at': None, 'random_seed': None, 'requested': 0, 'coverage': None,
                                   'storage_bytes': self.files.size('prompt_tasks', identifier), 'can_cleanup': False,
                                   'cleanup_reason': error.code, 'can_export_results': False})
            return values

    def emit(self, value):
        self.runtime.emit('prompt.task_changed', {key: value[key] for key in ('task_id', 'status', 'revision', 'ready')},
                          resource={'kind': 'prompt_task', 'id': value['task_id']}, version=self.view(value)['version'])

    def create(self, body, key):
        uuid_string(key)
        identifier = body['task_id']
        signature = digest(body)
        with self.lock:
            if self.runtime.draining:
                raise ServiceError(503, 'process:draining')
            if key in self.task_keys and self.task_keys[key] != identifier:
                raise ServiceError(409, 'prompt:request_key_conflict')
            try:
                old = self.load(identifier)
            except ServiceError as error:
                if error.status != 404:
                    raise
                old = None
            if old is not None:
                if old['request_key'] != key or old['request_digest'] != signature:
                    raise ServiceError(409, 'prompt:request_key_conflict')
                # A replay must never restart canceled/paused/finished work.
                return self.view(old)
            snapshot = self.freeze(body['request'])
            if snapshot['request']['plan']['random_seed'] is None or digest(snapshot) != body['preview_digest']:
                raise ServiceError(409, 'prompt:preview_stale')
            value = {'schema_version': 1, 'task_id': identifier, 'revision': 1, 'generation': 0,
                     'status': 'pending', 'ready': False, 'reason': None, 'snapshot': snapshot, 'result': None,
                     'created_at': utc_now(), 'request_key': key, 'request_digest': signature}
            if 'composer' in snapshot:
                value.update(schema_version=2, appends=[], execution={'round': 0, 'outcomes': [], 'selected': [],
                             'deficits_reason': None, 'append_error': None})
            self.files.put('prompt_tasks', identifier, value)
            self.task_keys[key] = identifier
            self._submit(value)
            return self.view(self.load(identifier))

    def _submit(self, value):
        identifier = value['task_id']
        if identifier in self.refinement_busy:
            raise ServiceError(409, 'prompt:inflight_busy')
        if identifier in self.pending_commits:
            pending = self.pending_commits[identifier]
            self.files.put('prompt_tasks', identifier, pending)
            self.pending_commits.pop(identifier)
            value = pending
        if value['snapshot']['python_version'] != list(sys.version_info[:3]):
            raise ServiceError(409, 'prompt:runtime_mismatch')
        value.update(status='running', reason=None, revision=value['revision'] + 1, generation=value['generation'] + 1)
        self.files.put('prompt_tasks', identifier, value)
        generation = value['generation']
        try:
            refined = value.get('execution') is not None
            if refined:
                self.refinement_busy.add(identifier)
            operation = self.runtime.supervisor.submit('prompt.sampling', lambda task: self._run_refinement(identifier, generation, task)
                                                       if refined else self._run(identifier, generation, task))
            self.active[identifier] = operation['operation_id']
        except Exception:
            self.refinement_busy.discard(identifier)
            value.update(status='paused', reason='prompt:dispatch_failed', revision=value['revision'] + 1)
            self.files.put('prompt_tasks', identifier, value)
            self.emit(value)
            raise
        self.emit(value)

    def _run(self, identifier, generation, operation):
        def stop():
            if operation.cancel.is_set() or self.runtime.draining:
                raise SamplingStopped()
            # Only a cheap revision lookup under the lock; the snapshot is immutable.
            with self.lock:
                if self.generations.get(identifier) != (generation, 'running'):
                    raise SamplingStopped()
        with self.lock:
            value = self.load(identifier)
            if value['generation'] != generation or value['status'] != 'running':
                return self.view(value)
            self.generations[identifier] = (generation, 'running')
            snapshot = value['snapshot']
        result, reason = None, None
        try:
            result = sample(snapshot, stop=stop)
        except SamplingStopped:
            reason = 'prompt:interrupted'
        except Exception:
            reason = 'prompt:sampling_failed'
        with self.lock:
            value = self.load(identifier)
            if value['generation'] == generation and value['status'] == 'running':
                if operation.cancel.is_set() or self.runtime.draining:
                    result, reason = None, 'prompt:interrupted'
                value.update(status='done' if result is not None else 'paused' if reason == 'prompt:interrupted' else 'failed',
                             reason=reason, result=result, ready=bool(result and result['ready']), revision=value['revision'] + 1)
                self.files.put('prompt_tasks', identifier, value)
                self.generations[identifier] = (generation, value['status'])
                self.emit(value)
            return self.view(value)

    def control(self, identifier, body):
        with self.lock:
            if identifier in self.pending_commits and identifier not in self.refinement_busy:
                self.files.put('prompt_tasks', identifier, self.pending_commits[identifier])
                self.pending_commits.pop(identifier)
            value = self.load(identifier)
            action = body['action']
            target = {'pause': 'paused', 'resume': 'running', 'cancel': 'canceled'}[action]
            if value['status'] == target:
                return self.view(value)
            if value['revision'] != body['expected_revision']:
                raise ServiceError(409, 'prompt:revision_conflict')
            if value['status'] in ('done', 'failed', 'canceled'):
                raise ServiceError(409, 'prompt:terminal_task')
            if self.runtime.draining:
                raise ServiceError(503, 'process:draining')
            if action == 'resume':
                if value['status'] != 'paused':
                    raise ServiceError(409, 'prompt:state_conflict')
                self._submit(value)
                return self.view(self.load(identifier))
            value.update(status=target, ready=False, revision=value['revision'] + 1)
            if action == 'cancel':
                self.pending_commits.pop(identifier, None)
                if value.get('execution'):
                    for row in value['execution']['outcomes']:
                        if row['status'] in ('pending', 'running'):
                            row.update(status='failed', reason='canceled')
            self.files.put('prompt_tasks', identifier, value)
            self.generations[identifier] = (value['generation'], target)
            self.emit(value)
            return self.view(value)

    def outcomes(self, identifier):
        with self.lock:
            value = self.load(identifier)
            if value.get('execution') is not None:
                return sorted(deepcopy(value['execution']['outcomes']), key=lambda r: r['index'])
            return deepcopy(value['result']['outcomes']) if value['result'] else []

    def seeds(self, identifier, revision):
        with self.lock:
            value = self.load(identifier)
            if value['revision'] != revision:
                raise ServiceError(409, 'prompt:revision_conflict')
            if not value['ready']:
                raise ServiceError(409, 'seeds:not_ready')
            items = deepcopy(value['result']['items'])
            return {'digest': digest(items), 'items': items, 'count': len(items)}
