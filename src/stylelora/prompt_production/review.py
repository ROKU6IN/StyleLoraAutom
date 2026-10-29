"""Explicit, local-only correction of finished prompt outcomes awaiting review."""
from collections import Counter
from copy import deepcopy
import sys
from typing import Literal

from pydantic import Field

from ..project_db.validation import digest, normalize_tag, utc_now
from ..runtime.contracts import uuid_string
from ..runtime.errors import ServiceError
from ..vision.policy import RATINGS
from .composer import gate, validate
from .sampling import classify, frozen_pool, sample
from .types import Aspect, Digest, Entry, Freeze, Identifier, ImportCommit, Rating, Strict


class ReviewedEntry(Entry):
    rating: Rating
    aspect: Aspect


class ReviewPreview(Freeze):
    index: int = Field(ge=0, lt=10000)
    entry: ReviewedEntry


class ReviewCommit(ImportCommit):
    acknowledge: bool = False


class ReviewResult(Strict):
    task_id: Identifier
    index: int = Field(ge=0, lt=10000)
    revision: int = Field(ge=1)
    ready: bool
    selected: bool
    count: int = Field(ge=0, le=10000)


class ReviewRecord(Strict):
    request_key: Identifier
    request_digest: Digest
    entry_digest: Digest
    reviewed_at: str = Field(min_length=1, max_length=64)
    previous_status: Literal['review_required'] = 'review_required'
    previous_reason: str | None = Field(max_length=2048)
    source: Literal['model_output', 'original_input']
    result: ReviewResult


class TaskReview:
    def review_detail(self, identifier, index):
        with self.lock:
            value = self.load(identifier)
            row = next((r for r in (value.get('execution') or {}).get('outcomes', []) if r['index'] == index), None)
            if row is None or row['status'] != 'review_required' and not row.get('review'):
                raise ServiceError(404, 'resource:not_found')
            reason = self.review_reason(value) or ('prompt:already_reviewed' if row.get('review') else None)
            source = row.get('result') or row.get('review_candidate') or row['input']
            audit = row.get('review')
            return {'task_id': identifier, 'index': index, 'revision': value['revision'], 'status': row['status'],
                    'reason': row['reason'], 'can_review': reason is None, 'blocked_reason': reason,
                    'original_entry': deepcopy(row['input']['entry']),
                    'model_entry': deepcopy(row['review_candidate']['entry']) if row.get('review_candidate') and row.get('reply') else None,
                    'entry': deepcopy(source['entry']), 'options': deepcopy(value['snapshot']['request']['options']),
                    'out_of_frame': deepcopy(row.get('reply', {}).get('out_of_frame', [])),
                    'rating_evidence': sorted({r for r in (row['input']['rating'], row.get('rating'), source['rating']) if r}, key=RATINGS.index),
                    'review': {k: audit[k] for k in ('reviewed_at', 'previous_reason', 'source')} if audit else None}

    def review_reason(self, value):
        if value['status'] != 'done' or value.get('execution') is None:
            return 'prompt:review_requires_done'
        if value['snapshot'].get('content_gate') != 'r1-v1':
            return 'prompt:review_unsupported'
        return self.cleanup_reason(value)

    def _review_task(self, identifier, revision):
        value = self.load(identifier)
        if value['revision'] != revision:
            raise ServiceError(409, 'prompt:revision_conflict')
        reason = self.review_reason(value)
        if reason:
            raise ServiceError(409, reason)
        if value['snapshot']['python_version'] != list(sys.version_info[:3]):
            raise ServiceError(409, 'prompt:runtime_mismatch')
        return value

    def _review_projection(self, value, body):
        snapshot = value['snapshot']
        rows = value['execution']['outcomes']
        row = next((r for r in rows if r['index'] == body['index']), None)
        if row is None or row['status'] != 'review_required':
            raise ServiceError(409, 'prompt:not_awaiting_review')
        source = row.get('review_candidate') or row['input']
        entry = deepcopy(body['entry'])
        canon = lambda tag: snapshot['catalog']['entries'].get(normalize_tag(tag), normalize_tag(tag))
        entry['tags'] = list(dict.fromkeys(canon(tag) for tag in entry['tags']))
        original = {canon(tag) for tag in row['input']['entry']['tags']}
        tags = set(entry['tags'])
        known = [rating for rating in (row['input']['rating'], row.get('rating'), source['rating']) if rating]
        known += [item['rating'] for item in snapshot['policy']['keywords'] if item['tag'] in tags]
        lower_bound = max(known, key=RATINGS.index) if known else None
        issues = []
        if lower_bound and RATINGS.index(entry['rating']) < RATINGS.index(lower_bound):
            issues.append('rating_below_evidence')
        # Human edits still obey the frozen R1 contract; no semantic bypass flag.
        # Previously declared out-of-frame content cannot leak back into this edit.
        out = row.get('reply', {}).get('out_of_frame', [])
        contract = {'status': 'ok', **{k: entry[k] for k in ('tags', 'natural_language', 'aspect')},
                    'added': sorted(tags - original), 'out_of_frame': out,
                    'dropped': [{'tag': tag, 'reason': 'human edit'} for tag in sorted(original - tags - set(map(canon, out)))]}
        issue = validate(contract, row['input'], snapshot)
        if issue:
            issues.append(issue)
        if snapshot['request']['coverage_enabled']:
            before, _ = classify(original, snapshot['request']['plan'])
            after, issue = classify(tags, snapshot['request']['plan'])
            if issue or before != after:
                issues.append('coverage_drift')
        fence = snapshot['request']['fence']
        if fence['enabled'] and RATINGS.index(entry['rating']) > RATINGS.index(fence['threshold']):
            issues.append('fenced')
        for other in rows:
            if other['index'] != row['index'] and other['status'] in ('ok', 'skipped'):
                if tags == {canon(t) for t in other['result']['entry']['tags']}:
                    issues.append('duplicate')
                    break
        report = {'index': row['index'], 'expected_revision': value['revision'], 'entry': entry,
                  'rating_lower_bound': lower_bound, 'valid': not issues, 'issues': list(dict.fromkeys(issues)),
                  'source': 'model_output' if row.get('review_candidate') and row.get('reply') else 'original_input',
                  'previous_ready': value['ready'], 'ready': False, 'selected': False, 'count': 0,
                  'selection_changes': 0, 'coverage': None}
        if issues:
            return report, None
        candidate = deepcopy(source)
        candidate.update(entry=entry, rating=entry['rating'], verified=True,
                         route='nsfw' if entry['rating'] in ('questionable', 'explicit') else 'normal')
        projected = deepcopy(value)
        edited = next(r for r in projected['execution']['outcomes'] if r['index'] == row['index'])
        edited.update(status='ok', reason='manual_review', result=candidate)
        # Rebuild only the immutable local pool metadata. Never invoke a model,
        # resume a task, replenish candidates or load today's dictionaries/policy.
        pool, complete, diagnostic = frozen_pool(snapshot)
        accepted, seen = [], set()
        for item in sorted(projected['execution']['outcomes'], key=lambda r: r['index']):
            if item['status'] not in ('ok', 'skipped'):
                continue
            output = item['result']
            identity = tuple(sorted({canon(t) for t in output['entry']['tags']}))
            if gate(output['entry'], item['input']['entry'], snapshot):
                raise ServiceError(409, 'prompt:invalid_selection')
            if identity not in seen:
                seen.add(identity)
                accepted.append(output)
        remaining = len(pool) - len(rows)
        final = sample(snapshot, pool=(accepted, complete and remaining == 0, Counter(diagnostic)))
        final.update(input_pool_complete=complete, unprocessed_candidates=remaining)
        selected = [r['pool_index'] for r in final['outcomes']]
        projected.update(result=final, ready=final['ready'], reason=None, revision=value['revision'] + 1)
        projected['execution'].update(selected=selected, deficits_reason=None if final['ready'] else 'manual_review_deficits')
        report.update(ready=final['ready'], selected=row['index'] in selected, count=final['count'], coverage=self.report(final, snapshot['request']),
                      selection_changes=len(set(selected) ^ set(value['execution']['selected'])))
        return report, projected

    def review_preview(self, identifier, body):
        if not self.preview_gate.acquire(blocking=False):
            raise ServiceError(409, 'prompt:preview_busy')
        try:
            with self.lock:
                value = self._review_task(identifier, body['expected_revision'])
                report, projected = self._review_projection(value, body)
                return self.runtime.previews.add(None, 'prompt-review', report,
                    {'task_id': identifier, 'body': body, 'projection_digest': digest(projected) if projected else None})
        finally:
            self.preview_gate.release()

    def review_commit(self, identifier, body, key):
        uuid_string(key)
        signature = digest([identifier, body])
        with self.lock:
            if self.runtime.draining:
                raise ServiceError(503, 'process:draining')
            value = self.load(identifier)
            for row in (value.get('execution') or {}).get('outcomes', []):
                receipt = row.get('review')
                if receipt and receipt['request_key'] == key:
                    if receipt['request_digest'] != signature:
                        raise ServiceError(409, 'prompt:request_key_conflict')
                    return deepcopy(receipt['result'])
            if not body['acknowledge']:
                raise ServiceError(409, 'prompt:review_ack_required')
            report, private = self.runtime.previews.get(None, 'prompt-review', body['preview_id'], body['digest'])
            if private['task_id'] != identifier:
                raise ServiceError(409, 'preview:stale')
            value = self._review_task(identifier, private['body']['expected_revision'])
            if not report['valid']:
                raise ServiceError(422, 'prompt:review_has_errors')
            if not self.preview_gate.acquire(blocking=False):
                raise ServiceError(409, 'prompt:preview_busy')
            try:
                checked, projected = self._review_projection(value, private['body'])
            finally:
                self.preview_gate.release()
            if not checked['valid'] or digest(projected) != private['projection_digest']:
                raise ServiceError(409, 'preview:stale')
            row = next(r for r in projected['execution']['outcomes'] if r['index'] == report['index'])
            receipt = {'task_id': identifier, 'index': row['index'], 'revision': projected['revision'],
                       'ready': projected['ready'], 'count': checked['count'], 'selected': checked['selected']}
            row['review'] = ReviewRecord(request_key=key, request_digest=signature, entry_digest=digest(row['result']['entry']),
                reviewed_at=utc_now(), previous_reason=next(r['reason'] for r in value['execution']['outcomes'] if r['index'] == row['index']),
                source=report['source'], result=receipt).model_dump()
            self.files.put('prompt_tasks', identifier, projected)
            self.emit(projected)
            return receipt
