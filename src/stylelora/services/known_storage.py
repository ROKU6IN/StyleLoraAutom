"""Read-only inventories of a frozen subset of recently authorized projects."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import threading
import time

from ..project_db.errors import ProjectError
from ..project_db.validation import digest, utc_now
from ..runtime.errors import ServiceError
from .recent_projects import read_lock, validate_root
from .storage_files import inventory

TTL = 900
MAX_SCANS = 3


class KnownStorage:
    def __init__(self, runtime):
        self.runtime, self.lock = runtime, threading.RLock()
        self.results, self.active = {}, None
        self.latest = None

    def _prune(self):
        self.results = {key: value for key, value in self.results.items() if value[0] > time.monotonic()}

    @contextmanager
    def access(self, root):
        context = None
        with self.runtime.condition:
            current = self.runtime.context
            if current and current.handle.root == root:
                self.runtime.retain(current, readonly=True)
                context = current
        try:
            if context:
                yield context.project_id
            else:
                with read_lock(root):
                    yield None
        finally:
            if context:
                self.runtime.release(context)

    def start(self, identifiers=None):
        history_revision, records = self.runtime.recent.snapshot(identifiers)
        config = self.runtime.product_config.get()
        scope = 'all_known' if identifiers is None else 'selected_known'
        with self.lock:
            if self.active:
                raise ServiceError(409, 'storage:scan_busy')
            self.active = True
            def finalizer():
                with self.lock:
                    self.active = None
            try:
                return self.runtime.supervisor.submit('storage.known_scan', lambda task: self._run(task, records, config, scope, history_revision), finalizer=finalizer)
            except BaseException:
                self.active = None
                raise

    def _run(self, task, records, config, scope, history_revision):
        thresholds = config['storage_thresholds']
        rows = []
        for index, record in enumerate(records):
            if task.cancel.is_set():
                raise ServiceError(409, 'operation:canceled')
            result = {key: record[key] for key in ('known_id', 'name')}
            result.update(display_name=Path(record['path']).name, project_id=None, status='unavailable', reason=None,
                          total_bytes=None, partial=True, errors=[], error_count=0, file_count=None, hardlinked_files=None,
                          exceeds_threshold=None)
            try:
                root, metadata = validate_root(record)
                with self.access(root) as project_id:
                    def check():
                        if task.cancel.is_set():
                            return True
                        validate_root(record)
                        return False
                    files, errors, _ = inventory(root, canceled=check)
                    validate_root(record)
                    total = sum(row['bytes'] for row in files)
                    result.update(name=metadata.name, project_id=project_id, status='partial' if errors else 'scanned',
                                  total_bytes=total, partial=bool(errors), errors=errors[:20], error_count=len(errors),
                                  file_count=len(files), hardlinked_files=sum(f['links'] > 1 for f in files),
                                  exceeds_threshold=total > thresholds['project_bytes'])
            except ServiceError as error:
                if error.code == 'operation:canceled':
                    raise
                result.update(reason=error.code, error_count=1)
            except (OSError, ValueError, ProjectError):
                result.update(reason='recent:unreadable', error_count=1)
            rows.append(result)
            self.runtime.supervisor.progress(task, completed=index+1, total=len(records))
        if task.cancel.is_set():
            raise ServiceError(409, 'operation:canceled')
        summary = {'scan_id': task.operation_id, 'as_of': utc_now(), 'project_count': len(rows),
                   'scanned_count': sum(r['total_bytes'] is not None for r in rows),
                   'unavailable_count': sum(r['total_bytes'] is None for r in rows),
                   'observed_total_bytes': sum(r['total_bytes'] or 0 for r in rows), 'partial': any(r['partial'] for r in rows),
                   'total_threshold_bytes': thresholds['total_bytes'], 'project_threshold_bytes': thresholds['project_bytes'],
                   'config_revision': config['revision'], 'scope': scope, 'history_revision': history_revision,
                   'over_threshold_count': sum(r['exceeds_threshold'] is True for r in rows),
                   'accounting': 'logical_file_bytes', 'retention_seconds': TTL,
                   'results_url': '/api/storage/scans/'+task.operation_id}
        summary['exceeds_threshold'] = summary['observed_total_bytes'] > thresholds['total_bytes']
        with self.lock:
            self._prune()
            while len(self.results) >= MAX_SCANS:
                del self.results[next(iter(self.results))]
            self.results[task.operation_id] = (time.monotonic()+TTL, deepcopy(rows), summary)
            self.latest = deepcopy(summary)
        self.runtime.emit('storage.changed', {'scan_id': task.operation_id})
        return summary

    def latest_summary(self):
        with self.lock:
            self._prune()
            if self.latest is None:
                return None
            value = deepcopy(self.latest)
            value['expired'] = value['scan_id'] not in self.results
        # No filesystem inventory: this is a historical summary, including after expiry.
        revision, _ = self.runtime.recent.snapshot()
        value['history_changed'] = revision != value['history_revision']
        return value

    def get(self, scan_id, *, limit=50, cursor=None, sort='-total_bytes,known_id'):
        if sort not in ('-total_bytes,known_id', 'known_id'):
            raise ServiceError(422, 'storage:invalid_sort')
        with self.lock:
            self._prune()
            value = self.results.get(scan_id)
            if value is None:
                raise ServiceError(410, 'session:expired')
            _, rows, summary = deepcopy(value)
        scope = digest(['known-storage', scan_id, sort])
        state = self.runtime.cursors.decode(cursor, scope) if cursor else None
        offset = state['last'] if state else 0
        rows.sort(key=(lambda r: r['known_id']) if sort == 'known_id' else
                  (lambda r: (r['total_bytes'] is None, -(r['total_bytes'] or 0), r['known_id'])))
        selected = rows[offset:offset+limit]
        more = offset+len(selected) < len(rows)
        return {'items': selected, 'summary': summary, 'total': len(rows), 'limit': limit, 'upper_id': len(rows),
                'has_more': more, 'next_cursor': self.runtime.cursors.encode({'identity': scope, 'last': offset+limit}) if more else None}
