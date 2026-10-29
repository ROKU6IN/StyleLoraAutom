"""Manual retention and read-only accepted-result export, independent of projects."""
from collections import Counter
from copy import deepcopy
import csv
import io

from ..project_db.validation import digest, encode, utc_now
from ..runtime.contracts import uuid_string
from ..runtime.errors import ServiceError
from ..vision.policy import RATINGS
from .composer import gate
from .storage import stored_size
from .types import Cleanup, Entry


class TaskManagement:
    def tombstone(self, identifier):
        try:
            value = self.files.get('prompt_tombstones', identifier)
        except ServiceError as error:
            if error.status == 404:
                return None
            raise
        try:
            if set(value) != {'schema_version', 'kind', 'task_id', 'previous_revision', 'revision', 'request_key',
                             'request_digest', 'content_digest', 'deleted_at', 'storage_bytes', 'cleanup_key', 'cleanup_digest'}:
                raise ValueError()
            if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'deleted_prompt_task' or value['task_id'] != identifier:
                raise ValueError()
            if type(value['previous_revision']) is not int or value['previous_revision'] < 1 or type(value['revision']) is not int or value['revision'] != value['previous_revision'] + 1:
                raise ValueError()
            if type(value['storage_bytes']) is not int or value['storage_bytes'] < 0 or not isinstance(value['deleted_at'], str):
                raise ValueError()
            for name in ('request_key', 'cleanup_key'):
                uuid_string(value[name])
            for name in ('request_digest', 'content_digest', 'cleanup_digest'):
                if not isinstance(value[name], str) or len(value[name]) != 64 or any(c not in '0123456789abcdef' for c in value[name]):
                    raise ValueError()
        except Exception:
            raise ServiceError(503, 'prompt:corrupt_snapshot') from None
        return value

    def cleanup_reason(self, value):
        identifier = value['task_id']
        if value['status'] in ('pending', 'running'):
            return 'prompt:stop_before_cleanup'
        if identifier in self.pending_commits:
            return 'prompt:pending_result'
        if identifier in self.refinement_busy:
            return 'prompt:inflight_busy'
        operation_id = self.active.get(identifier)
        if operation_id:
            with self.runtime.supervisor.lock:
                operation = self.runtime.supervisor.tasks.get(operation_id)
                if operation is None or not operation.finished.is_set():
                    return 'prompt:inflight_busy'
        return None

    def management_view(self, value):
        reason = self.cleanup_reason(value)
        return {'storage_bytes': self.files.size('prompt_tasks', value['task_id']),
                'can_cleanup': reason is None, 'cleanup_reason': reason, 'can_export_results': reason is None,
                'can_review': self.review_reason(value) is None,
                'review_blocked_reason': self.review_reason(value)}

    def storage_inventory(self):
        with self.lock:
            rows, receipts, receipt_bytes = [], 0, 0
            for identifier in self.files.ids('prompt_tasks'):
                try:
                    marker = self.tombstone(identifier)
                    if marker:
                        rows.append({'task_id': identifier, 'revision': marker['previous_revision'], 'status': 'cleanup_pending',
                                     'ready': False, 'can_cleanup': True, 'cleanup_reason': None,
                                     'storage_bytes': self.files.size('prompt_tasks', identifier), 'requested': None})
                    else:
                        value = self.load(identifier)
                        summary = self.view(value)
                        rows.append({k: summary[k] for k in ('task_id', 'revision', 'status', 'ready', 'reason', 'created_at', 'requested')} |
                                    self.management_view(value))
                except ServiceError as error:
                    rows.append({'task_id': identifier, 'revision': 0, 'status': 'unavailable', 'ready': False,
                                 'can_cleanup': False, 'cleanup_reason': error.code,
                                 'storage_bytes': self.files.size('prompt_tasks', identifier), 'requested': None})
            for identifier in self.files.ids('prompt_tombstones'):
                receipts += 1
                receipt_bytes += self.files.size('prompt_tombstones', identifier)
            return {'items': rows, 'total': len(rows), 'storage_bytes': sum(row['storage_bytes'] for row in rows),
                    'receipt_count': receipts, 'receipt_bytes': receipt_bytes, 'cleanup_limit': 200}

    def cleanup(self, body, key):
        body = Cleanup.model_validate(body).model_dump()
        uuid_string(key)
        signature = digest(body)
        with self.lock:
            if self.runtime.draining:
                raise ServiceError(503, 'process:draining')
            if key in self.cleanup_keys and self.cleanup_keys[key] != signature:
                raise ServiceError(409, 'prompt:request_key_conflict')
            planned = []
            # Validate every selection before changing any file. Each task is atomic;
            # a later disk failure is explicit and retries finish the same selection.
            for selection in body['tasks']:
                identifier = selection['task_id']
                marker = self.tombstone(identifier)
                if marker:
                    if marker['previous_revision'] != selection['expected_revision']:
                        raise ServiceError(409, 'prompt:revision_conflict')
                    if self.files.size('prompt_tasks', identifier) and digest(self.files.get('prompt_tasks', identifier)) != marker['content_digest']:
                        raise ServiceError(409, 'prompt:cleanup_conflict')
                else:
                    value = self.load(identifier)
                    if value['revision'] != selection['expected_revision']:
                        raise ServiceError(409, 'prompt:revision_conflict')
                    reason = self.cleanup_reason(value)
                    if reason:
                        raise ServiceError(409, reason)
                    marker = {'schema_version': 1, 'kind': 'deleted_prompt_task', 'task_id': identifier,
                              'previous_revision': value['revision'], 'revision': value['revision'] + 1,
                              'request_key': value['request_key'], 'request_digest': value['request_digest'],
                              'content_digest': digest(value), 'deleted_at': utc_now(),
                              'storage_bytes': self.files.size('prompt_tasks', identifier),
                              'cleanup_key': key, 'cleanup_digest': signature}
                planned.append(marker)
            self.cleanup_keys[key] = signature
            completed = []
            for marker in planned:
                identifier = marker['task_id']
                try:
                    # Durable deletion barrier first. Old POST /tasks cannot resurrect
                    # work even after a crash before the snapshot unlink.
                    self.files.put('prompt_tombstones', identifier, marker)
                    self.files.remove_checked('prompt_tasks', identifier, marker['content_digest'])
                except ServiceError:
                    raise ServiceError(503, 'prompt:cleanup_incomplete', outcome='unknown',
                                       details={'completed_task_ids': completed, 'task_id': identifier}) from None
                self.active.pop(identifier, None)
                self.generations.pop(identifier, None)
                self.unavailable.pop(identifier, None)
                with self.runtime.previews.lock:
                    self.runtime.previews.values = {k: v for k, v in self.runtime.previews.values.items()
                        if not (v[2] in ('prompt-results-export', 'prompt-review') and v[4]['task_id'] == identifier)}
                completed.append(identifier)
                self.runtime.emit('prompt.task_deleted', {'task_id': identifier, 'revision': marker['revision'], 'ready': False},
                                  resource={'kind': 'prompt_task', 'id': identifier}, version=digest(marker))
            return {'task_ids': completed, 'count': len(completed),
                    'removed_snapshot_bytes': sum(m['storage_bytes'] for m in planned),
                    'retained_receipt_bytes': sum(stored_size(m) for m in planned)}

    def accepted_results(self, value):
        reason = self.cleanup_reason(value)
        if reason:
            raise ServiceError(409, reason)
        rows = self.outcomes(value['task_id'])
        selected = set(value['execution']['selected']) if value.get('execution') else {r['index'] for r in rows}
        result, excluded, seen = [], Counter(), set()
        snapshot, fence = value['snapshot'], value['snapshot']['request']['fence']
        for row in sorted(rows, key=lambda r: r['index']):
            if row['status'] not in ('ok', 'skipped'):
                excluded[row['status']] += 1
                continue
            candidate = row.get('result', row)
            entry = Entry.model_validate(candidate['entry']).model_dump()
            entry['rating'] = candidate['rating']
            original = row.get('input', candidate)['entry']
            issue = gate(entry, original, snapshot)
            rating, verified = candidate['rating'], candidate['verified']
            if fence['enabled'] and (rating is not None and RATINGS.index(rating) > RATINGS.index(fence['threshold']) or not verified and fence['nsfwfallback']):
                issue = 'fenced'
            identity = tuple(sorted({snapshot['catalog']['entries'].get(tag, tag) for tag in entry['tags']}))
            if issue or identity in seen:
                excluded[issue or 'duplicate_tags'] += 1
                continue
            seen.add(identity)
            result.append({'index': row['index'], 'entry': entry, 'rating_verified': verified,
                           'refinement_status': row['status'], 'selected_for_seeds': value['ready'] and row['index'] in selected})
        return result, dict(excluded)

    def results_preview(self, identifier, body):
        with self.lock:
            value = self.load(identifier)
            if body['expected_revision'] != value['revision']:
                raise ServiceError(409, 'prompt:revision_conflict')
            rows, excluded = self.accepted_results(value)
            metadata = {'task_id': identifier, 'revision': value['revision'], 'source_status': value['status'],
                        'source_ready': value['ready'], 'requested': value['snapshot']['request']['plan']['target_count'],
                        'count': len(rows), 'scope': 'accepted_results', 'usable_for_start': False,
                        'excluded': excluded, 'coverage': self.report(value['result']) if value['result'] else None}
            format = body['format']
            if format == 'json':
                content = encode({'schema_version': 1, 'kind': 'prompt_task_results', **metadata, 'rows': rows}) + '\n'
                media, losses = 'application/json', []
            elif format == 'csv':
                output = io.StringIO(newline='')
                fields = ['index', 'tags', 'natural_language', 'aspect', 'rating', 'rating_verified',
                          'refinement_status', 'selected_for_seeds', 'source_ready', 'usable_for_start', 'requested']
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: v for k, v in row.items() if k != 'entry'} | row['entry'] |
                                    {'tags': encode(row['entry']['tags']), 'source_ready': value['ready'],
                                     'usable_for_start': False, 'requested': metadata['requested']})
                content, media, losses = '\ufeff' + output.getvalue(), 'text/csv', ['coverage_report', 'task_metadata']
            else:
                content = '\n'.join(', '.join(r['entry']['tags']) +
                                    (', ' + ' '.join(r['entry']['natural_language'].split()) if r['entry']['natural_language'] else '') for r in rows) + '\n'
                media, losses = 'text/plain', ['aspect', 'rating', 'structure', 'task_metadata', 'coverage_report']
            if len(content.encode('utf-8')) > 10 * 1024 * 1024:
                raise ServiceError(413, 'prompt:export_limit')
            report = {**metadata, 'format': format, 'lossy_fields': losses, 'requires_lossy_ack': format == 'txt',
                      'can_export': bool(rows), 'bytes': len(content.encode('utf-8')),
                      'examples': rows[:20], 'examples_truncated': len(rows) > 20}
            return self.runtime.previews.add(None, 'prompt-results-export', report,
                {'task_id': identifier, 'revision': value['revision'], 'content': content, 'media_type': media,
                 'filename': f'prompt-results-{identifier}.{format}'})

    def results_export(self, identifier, body):
        with self.lock:
            report, frozen = self.runtime.previews.get(None, 'prompt-results-export', body['preview_id'], body['digest'])
            if frozen['task_id'] != identifier:
                raise ServiceError(409, 'preview:stale')
            value = self.load(identifier)
            if value['revision'] != frozen['revision']:
                raise ServiceError(409, 'prompt:revision_conflict')
            if not report['can_export']:
                raise ServiceError(409, 'prompt:no_accepted_results')
            if report['requires_lossy_ack'] and not body['acknowledge_lossy']:
                raise ServiceError(409, 'prompt:lossy_ack_required')
            return {k: frozen[k] for k in ('filename', 'media_type', 'content')}
