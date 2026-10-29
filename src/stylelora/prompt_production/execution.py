from collections import Counter
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from ..runtime.errors import ServiceError
from .composer import Composer, FATAL
from .sampling import SamplingStopped, frozen_pool, sample, classify


class RefinedTasks:
    def _write_execution(self, identifier, generation, update, *, inflight=False):
        """A pause accepts this generation's reply; cancel never accepts it."""
        with self.lock:
            current = self.load(identifier)
            if current['generation'] != generation or current['status'] not in (('running', 'paused') if inflight else ('running',)):
                raise SamplingStopped()
            value = deepcopy(self.pending_commits.get(identifier, current))
            value['status'] = current['status']
            value['revision'] = max(value['revision'], current['revision'])
            update(value)
            value['revision'] += 1
            # Keep a known reply in memory across a failed atomic write. Resume
            # commits this value before dispatch, and never calls the model again.
            self.pending_commits[identifier] = deepcopy(value)
            self.files.put('prompt_tasks', identifier, value)
            self.pending_commits.pop(identifier, None)
            self.emit(value)
            return value

    def _run_refinement(self, identifier, generation, operation):
        with self.lock:
            value = self.load(identifier)
            if value['generation'] != generation or value['status'] != 'running':
                self.refinement_busy.discard(identifier)
                return
            self.generations[identifier] = (generation, 'running')
            snapshot = value['snapshot']
        def stop():
            with self.lock:
                if self.runtime.draining or operation.cancel.is_set() or self.generations.get(identifier) != (generation, 'running'):
                    raise SamplingStopped()
        def patch_outcome(index, change, inflight=True):
            def update(value):
                next(row for row in value['execution']['outcomes'] if row['index'] == index).update(change)
            try:
                return self._write_execution(identifier, generation, update, inflight=inflight)
            except ServiceError:
                # Phase checkpoints happen before a call or after a known R5
                # response. An I/O failure here must resume this unsent phase.
                if 'phase' in change:
                    with self.lock:
                        cached = self.pending_commits.get(identifier)
                        if cached:
                            next(r for r in cached['execution']['outcomes'] if r['index'] == index)['status'] = 'pending'
                raise
        service = self
        class Canceled:
            def is_set(self):
                with service.lock:
                    state = service.generations.get(identifier)
                    return operation.cancel.is_set() or service.runtime.draining or state is not None and (state[0] != generation or state[1] == 'canceled')
        try:
            pool, complete, diagnostic = frozen_pool(snapshot, stop)
            composer = Composer(self.runtime, snapshot, Canceled(), stop)
            def process(outcome):
                stop()
                index = outcome['index']
                patch_outcome(index, {'status': 'running'}, inflight=False)
                try:
                    result = composer.compose(outcome, lambda changes: patch_outcome(index, changes))
                except SamplingStopped:
                    patch_outcome(index, {'status': 'pending'})
                    raise
                except ServiceError as error:
                    if error.code in FATAL or error.code.startswith('prompt:'):
                        patch_outcome(index, {'status': 'pending', 'reason': error.code})
                        def pause(value):
                            value.update(status='paused', reason=error.code, ready=False)
                            self.generations[identifier] = (generation, 'paused')
                        self._write_execution(identifier, generation, pause, inflight=True)
                        raise
                    result = {'status': 'failed', 'reason': 'llm_outcome_unknown' if error.outcome == 'unknown' else error.code}
                if result['status'] in ('ok', 'skipped') and snapshot['request']['coverage_enabled']:
                    old, _ = classify(set(outcome['input']['entry']['tags']), snapshot['request']['plan'])
                    new, issue = classify(set(result['result']['entry']['tags']), snapshot['request']['plan'])
                    if issue or old != new:
                        result = {'status': 'failed', 'reason': 'coverage_drift'}
                patch_outcome(index, result)
            while True:
                stop()
                with self.lock:
                    execution = self.load(identifier)['execution']
                pending = [r for r in execution['outcomes'] if r['status'] == 'pending']
                if not pending:
                    accepted = [r['result'] for r in execution['outcomes'] if r['status'] in ('ok', 'skipped')]
                    # Stable priority, canonical tag-only dedup, then final quotas.
                    seen, usable = set(), []
                    for row in sorted(accepted, key=lambda r: r['pool_index']):
                        key = tuple(sorted(snapshot['catalog']['entries'].get(t, t) for t in row['entry']['tags']))
                        if key not in seen:
                            seen.add(key)
                            usable.append(row)
                    attempted = {r['index'] for r in execution['outcomes']}
                    remaining_count = len(pool) - len(attempted)
                    final = sample(snapshot, pool=(usable, complete and remaining_count == 0, Counter(diagnostic)), stop=stop)
                    final.update(input_pool_complete=complete, unprocessed_candidates=remaining_count)
                    if final['ready'] or execution['round'] >= 3:
                        def finish(value):
                            value.update(status='done', reason=None, ready=final['ready'], result=final)
                            value['execution']['selected'] = [r['pool_index'] for r in final['outcomes']]
                            value['execution']['deficits_reason'] = None if final['ready'] else 'refill_exhausted'
                        value = self._write_execution(identifier, generation, finish)
                        if snapshot['request']['append_target_id'] and value['ready']:
                            self._automatic_append(identifier)
                        return
                    used = {r['index'] for r in execution['outcomes']}
                    remaining = [r for r in pool if r['pool_index'] not in used]
                    proposal = sample(snapshot, pool=(usable + remaining, complete, Counter(diagnostic)), stop=stop)
                    chosen = [r for r in proposal['outcomes'] if r['pool_index'] not in used]
                    if not chosen:
                        # No feasible replenishment within the frozen candidate pool.
                        def exhausted(value):
                            value['execution']['round'] = 3
                        self._write_execution(identifier, generation, exhausted)
                        continue
                    def enqueue(value):
                        value['execution']['round'] += 1
                        value['execution']['outcomes'].extend({'index': row['pool_index'], 'status': 'pending', 'phase': 'new',
                            'reason': None, 'input': {k: v for k, v in row.items() if k not in ('index', 'status')}} for row in chosen)
                    self._write_execution(identifier, generation, enqueue)
                    continue
                # Only two futures admitted, not an unbounded executor queue.
                # LlmService also shares a global two-call gate with vision tasks.
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix='prompt-refine') as workers:
                    remaining = iter(pending)
                    active = {workers.submit(process, row) for row in (next(remaining, None), next(remaining, None)) if row is not None}
                    failure = None
                    while active:
                        done, active = wait(active, return_when=FIRST_COMPLETED)
                        for future in done:
                            try:
                                future.result()
                            except Exception as error:
                                failure = failure or error
                        if failure is None:
                            for _ in range(2 - len(active)):
                                row = next(remaining, None)
                                if row is not None:
                                    active.add(workers.submit(process, row))
                    if failure:
                        raise failure
        except SamplingStopped:
            with self.lock:
                value = self.load(identifier)
                if value['status'] == 'running' and value['generation'] == generation:
                    value.update(status='paused', reason='prompt:interrupted', ready=False, revision=value['revision'] + 1)
                    self.files.put('prompt_tasks', identifier, value)
                    self.generations[identifier] = (generation, 'paused')
                    self.emit(value)
        except Exception as error:
            reason = error.code if isinstance(error, ServiceError) else 'prompt:execution_failed'
            with self.lock:
                value = self.load(identifier)
                if value['status'] in ('running', 'paused') and value['generation'] == generation:
                    for row in value['execution']['outcomes']:
                        if row['status'] == 'running':
                            row.update(status='failed', reason='llm_outcome_unknown')
                    value.update(status='paused', reason=reason, ready=False, revision=value['revision'] + 1)
                    # Don't overwrite an uncommitted known reply on disk.
                    if identifier in self.pending_commits:
                        pending = self.pending_commits[identifier]
                        pending.update(status='paused', reason=reason, ready=False, revision=max(value['revision'], pending['revision']) + 1)
                    try:
                        self.files.put('prompt_tasks', identifier, value)
                    except ServiceError:
                        pass
                    self.generations[identifier] = (generation, 'paused')
                    self.emit(value)
        finally:
            with self.lock:
                self.refinement_busy.discard(identifier)

    def _automatic_append(self, identifier):
        with self.lock:
            value = self.load(identifier)
            indices = [r['index'] for r in value['execution']['outcomes'] if r['status'] == 'ok' and r['index'] in value['execution']['selected']]
            if not indices:
                return
            target = value['snapshot']['append_target']
            try:
                self.append(identifier, {'target_id': target['dictionary_id'], 'indices': indices,
                    'expected_revision': value['revision'], 'target_version': target['version']}, automatic=True)
            except ServiceError as error:
                value = self.load(identifier)
                value['execution']['append_error'] = error.code
                value['revision'] += 1
                self.files.put('prompt_tasks', identifier, value)
                self.emit(value)
