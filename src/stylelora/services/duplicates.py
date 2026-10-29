"""Snapshot-based, bounded, session-only duplicate scans. Never writes the project."""
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import struct
import tempfile
import threading
import time
from uuid import uuid4

from ..imaging.phash import ALGORITHM, HashRecord, PhashError, candidate_rows, valid_hash, validate_threshold
from ..project_db.connection import read_snapshot
from ..project_db.errors import UnsafePath
from ..project_db.validation import encode, relative_path
from ..runtime.errors import ServiceError
from ..runtime.contracts import Versions
from ..runtime.registry import expires

MAX_IMAGES = 10_000
MAX_BYTES = 64 * 1024 * 1024
PAIR = struct.Struct('>QQ')


@dataclass
class Scan:
    scan_id: str
    project_id: str
    threshold: int
    rows: tuple
    input_count: int = 0
    deadline: float = field(default_factory=lambda: time.monotonic()+900)
    expires_at: str = field(default_factory=lambda: expires(900))
    lock: object = field(default_factory=threading.RLock)
    released: object = field(default_factory=threading.Event)
    status: str = 'pending'
    revision: int = 0
    operation_id: str | None = None
    task: object = None
    directory: object = None
    counts: list = field(default_factory=lambda: [0]*65)
    skipped: dict = field(default_factory=dict)
    eligible: int = 0
    completed: int = 0
    total: int = 0
    matched: int = 0
    error: str | None = None

    @property
    def version(self):
        return f'{self.scan_id}-{self.revision}'

    def cleanup(self):
        if self.directory:
            self.directory.cleanup()
            self.directory = None


class DuplicateService:
    def __init__(self, runtime, *, temp_root=None):
        self.runtime, self.temp_root = runtime, temp_root
        self.lock, self.scans = threading.RLock(), {}
        self.max_bytes = MAX_BYTES

    def _expire(self):
        # Called with registry lock. At most three live result sets are retained.
        for key, scan in list(self.scans.items()):
            if scan.deadline <= time.monotonic():
                self._release(scan)
                del self.scans[key]

    def _release(self, scan):
        with scan.lock:
            scan.released.set()
            if scan.task:
                scan.task.cancel.set()
            if scan.status in ('done', 'failed', 'canceled'):
                scan.cleanup()

    def invalidate(self, project_id):
        with self.lock:
            for key, scan in list(self.scans.items()):
                if scan.project_id == project_id:
                    self._release(scan)
                    del self.scans[key]

    def _freeze(self, context, scope):
        ids, query = scope.get('selected_ids'), scope.get('filter')
        if (ids is None) == (query is None):
            raise ServiceError(422, 'input:invalid_scope')
        with read_snapshot(context.handle) as db:
            if ids is not None:
                if len(ids) > MAX_IMAGES:
                    raise ServiceError(413, 'batch:too_large')
                if len(ids) != len(set(ids)):
                    raise ServiceError(422, 'input:invalid_scope')
                where, args = 'image_id IN (SELECT value FROM json_each(?))', [encode(ids)]
            else:
                query = self.runtime.dataset.normalize_query(query)
                if not query['review_statuses']:
                    query['review_statuses'] = ['keep', 'unreviewed']
                where, args = self.runtime.dataset._filter(query)
            rows = tuple(dict(row) for row in db.execute(
                f'SELECT image_id,phash,filename,file_missing,file_size FROM images WHERE {where} ORDER BY image_id LIMIT ?',
                [*args, MAX_IMAGES+1]))
            if len(rows) > MAX_IMAGES:
                raise ServiceError(413, 'batch:too_large')
            if ids is not None and len(rows) != len(ids):
                raise ServiceError(404, 'resource:not_found')
        return rows

    def start(self, context, value):
        validate_threshold(value['threshold'])
        with self.lock:
            self._expire()
            if len(self.scans) >= 3:
                raise ServiceError(429, 'duplicates:scan_limit')
            rows = self._freeze(context, value['scope'])
            scan = Scan(str(uuid4()), context.project_id, value['threshold'], rows, input_count=len(rows))
            self.scans[scan.scan_id] = scan
            try:
                operation = self.runtime.supervisor.submit('duplicates.scan', lambda task: self._run(context, scan, task),
                                                           context=context, finalizer=lambda: self._finalize(scan))
            except BaseException:
                del self.scans[scan.scan_id]
                raise
            scan.operation_id = operation['operation_id']
            return {'scan_id': scan.scan_id, 'operation': operation,
                    'status_url': f'/api/projects/{context.project_id}/duplicates/scans/{scan.scan_id}'}

    def _finalize(self, scan):
        with scan.lock:
            if scan.status == 'pending':
                scan.status, scan.error = 'canceled', 'operation:canceled'
                scan.revision += 1
                scan.rows = ()

    def _run(self, context, scan, task):
        files = {}
        with scan.lock:
            scan.task, scan.status = task, 'running'
            scan.revision += 1
        cancelled = lambda: scan.released.is_set() or task.cancel.is_set()
        try:
            skipped, records = Counter(), []
            for row in scan.rows:
                if cancelled():
                    raise PhashError('operation:canceled')
                try:
                    path = relative_path(context.handle.root, 'images/' + row['filename'])
                    missing = row['file_missing'] or not path.is_file()
                    changed = not missing and path.stat().st_size != row['file_size']
                except (OSError, ValueError, UnsafePath):
                    missing, changed = True, False
                if missing:
                    skipped['missing'] += 1
                elif changed:
                    skipped['size_changed'] += 1
                elif row['phash'] is None:
                    skipped['no_hash'] += 1
                elif not valid_hash(row['phash']):
                    skipped['invalid_hash'] += 1
                else:
                    records.append(HashRecord(row['image_id'], row['phash']))
            with scan.lock:
                scan.eligible, scan.skipped = len(records), dict(skipped)
                scan.total = len(records) * (len(records)-1) // 2
            if self.temp_root:
                Path(self.temp_root).mkdir(parents=True, exist_ok=True)
            scan.directory = tempfile.TemporaryDirectory(prefix='stylelora-duplicates-', dir=self.temp_root)
            last_update = 0
            for comparisons, buckets in candidate_rows(records, threshold=scan.threshold, cancelled=cancelled):
                added = sum(map(len, buckets))
                if (scan.matched + added) * PAIR.size > self.max_bytes:
                    raise ServiceError(413, 'duplicates:result_limit')
                for distance, bucket in enumerate(buckets):
                    if bucket:
                        if distance not in files:
                            files[distance] = (Path(scan.directory.name)/f'{distance}.pairs').open('xb')
                        files[distance].write(b''.join(PAIR.pack(left, right) for left, right in bucket))
                        scan.counts[distance] += len(bucket)
                with scan.lock:
                    scan.completed += comparisons
                    scan.matched += added
                if time.monotonic() - last_update > .1:
                    self.runtime.supervisor.progress(task, completed=scan.completed, total=scan.total)
                    last_update = time.monotonic()
            for stream in files.values():
                stream.close()
            files.clear()
            with scan.lock:
                if cancelled():
                    raise PhashError('operation:canceled')
                scan.status = 'done'
                scan.revision += 1
            self.runtime.supervisor.progress(task, completed=scan.total, total=scan.total)
            return {'scan_id': scan.scan_id, 'total_pairs': scan.matched,
                    'status_url': f'/api/projects/{scan.project_id}/duplicates/scans/{scan.scan_id}'}
        except BaseException as error:
            with scan.lock:
                scan.status = 'canceled' if cancelled() or isinstance(error, PhashError) and str(error) == 'operation:canceled' else 'failed'
                scan.error = error.code if isinstance(error, ServiceError) else str(error) if isinstance(error, PhashError) else 'duplicates:scan_failed'
                scan.revision += 1
                if scan.status == 'canceled':
                    task.cancel.set()
            raise ServiceError(409 if scan.status == 'canceled' else 503, scan.error) from None
        finally:
            for stream in files.values():
                stream.close()
            with scan.lock:
                scan.rows = ()
                if scan.released.is_set() or scan.status != 'done':
                    scan.cleanup()

    def _require(self, context, scan_id):
        self._expire()
        scan = self.scans.get(scan_id)
        if scan is None or scan.project_id != context.project_id:
            raise ServiceError(410, 'session:expired')
        return scan

    def list(self, context):
        with self.lock:
            self._expire()
            items = []
            for scan in self.scans.values():
                if scan.project_id == context.project_id:
                    with scan.lock:
                        items.append({'scan_id': scan.scan_id, 'status': scan.status, 'threshold': scan.threshold,
                                      'version': scan.version, 'expires_at': scan.expires_at})
            return {'items': items, 'max_images': MAX_IMAGES, 'max_result_bytes': self.max_bytes}

    def page(self, context, scan_id, *, limit=50, cursor=None):
        with self.lock:
            scan = self._require(context, scan_id)
            with scan.lock:
                identity = [context.project_id, 'duplicates', scan_id]
                state = self.runtime.cursors.decode(cursor, identity) if cursor else None
                if cursor and scan.status != 'done':
                    raise ServiceError(422, 'page:invalid_cursor')
                start = state['last'] if state else 0
                total = scan.matched if scan.status == 'done' else 0
                pairs, offset, remaining = [], start, limit
                if scan.status == 'done':
                    for distance, count in enumerate(scan.counts):
                        if offset >= count:
                            offset -= count
                            continue
                        take = min(remaining, count-offset)
                        try:
                            with (Path(scan.directory.name)/f'{distance}.pairs').open('rb') as stream:
                                stream.seek(offset*PAIR.size)
                                raw = stream.read(take*PAIR.size)
                        except OSError:
                            raise ServiceError(503, 'duplicates:result_unavailable') from None
                        if len(raw) != take*PAIR.size:
                            raise ServiceError(503, 'duplicates:result_unavailable')
                        pairs.extend({'left_id': left, 'right_id': right, 'distance': distance} for left, right in PAIR.iter_unpack(raw))
                        remaining -= take
                        offset = 0
                        if not remaining:
                            break
                end = start + len(pairs)
                return {'scan_id': scan_id, 'operation_id': scan.operation_id, 'algorithm': ALGORITHM,
                        'threshold': scan.threshold, 'status': scan.status, 'version': scan.version,
                        'expires_at': scan.expires_at, 'input_count': scan.input_count, 'eligible': scan.eligible, 'skipped': scan.skipped,
                        'completed': scan.completed, 'total': scan.total, 'matched_so_far': scan.matched,
                        'reason': scan.error, 'items': pairs, 'limit': limit, 'upper_id': total,
                        'has_more': end < total, 'next_cursor': self.runtime.cursors.encode({'identity': identity, 'last': end}) if end < total else None}

    def delete(self, context, scan_id, expected):
        with self.lock:
            scan = self._require(context, scan_id)
            with scan.lock:
                Versions.require(expected, scan.version)
                self._release(scan)
                del self.scans[scan_id]
