"""Read-only matrix projections; bounded, short-lived preview details."""
from copy import deepcopy
import secrets
import threading
import time
from uuid import uuid4

from ..project_db.validation import digest, encode
from ..runtime.contracts import uuid_string
from ..runtime.errors import ServiceError
from ..runtime.registry import expires

MAX_VIEW_BYTES = 64 * 1024 * 1024
MAX_VIEWS = 3
VIEW_TTL = 900


def matrix(request):
    plan = request['plan']
    return {'enabled': request['coverage_enabled'],
            'primary_dimensions': deepcopy(plan['primary_dimensions']) if request['coverage_enabled'] else [],
            'cell_values': deepcopy(plan['cell_values']) if request['coverage_enabled'] else {}}


def entries(report):
    # This is the selected set counted by cell_counts, not every accepted or
    # attempted refinement outcome. Do not expose unrelated private pool fields.
    return [{'index': row['index'], 'entry': deepcopy(row['entry']), 'cell': row['cell'],
             'rating': row['rating'], 'verified': row['verified']}
            for row in report['outcomes']]


def cell_entries(targets, rows, cell):
    if not isinstance(cell, str) or len(cell) > 4096:
        raise ServiceError(422, 'coverage:invalid_cell')
    if cell not in targets:
        raise ServiceError(404, 'coverage:cell_not_found')
    return deepcopy([row for row in rows if row['cell'] == cell])


class CoverageViews:
    """At most three views / 64 MiB in total; evict oldest, expire after 15 min."""
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    def prune(self):
        self.values = {key: value for key, value in self.values.items() if value['deadline'] > time.monotonic()}

    def add(self, report):
        data = {'targets': deepcopy(report['cell_targets']), 'rows': entries(report)}
        size = len(encode(data).encode('utf-8'))
        if size > MAX_VIEW_BYTES:
            # A detail view must not disable an otherwise valid sampling preview.
            return None
        with self.lock:
            self.prune()
            while self.values and (len(self.values) >= MAX_VIEWS or sum(v['size'] for v in self.values.values()) + size > MAX_VIEW_BYTES):
                del self.values[next(iter(self.values))]
            identifier, signature = str(uuid4()), digest(data)
            self.values[identifier] = {'deadline': time.monotonic() + VIEW_TTL, 'digest': signature, 'data': data, 'size': size}
            return {'preview_id': identifier, 'digest': signature, 'expires_at': expires(VIEW_TTL)}

    def get(self, identifier, signature, cell):
        uuid_string(identifier)
        with self.lock:
            self.prune()
            value = self.values.get(identifier)
            if value is None:
                raise ServiceError(410, 'preview:expired')
            if not secrets.compare_digest(value['digest'], signature):
                raise ServiceError(409, 'preview:stale')
            return cell_entries(value['data']['targets'], value['data']['rows'], cell)

    def release(self, identifier, signature):
        uuid_string(identifier)
        with self.lock:
            self.prune()
            value = self.values.get(identifier)
            if value is not None and not secrets.compare_digest(value['digest'], signature):
                raise ServiceError(409, 'preview:stale')
            self.values.pop(identifier, None)
            return {'released': True}


class TaskMatrix:
    def matrix_entries(self, identifier, revision, cell):
        with self.lock:
            value = self.load(identifier)
            if value['revision'] != revision:
                raise ServiceError(409, 'prompt:revision_conflict')
            if value['result'] is None:
                raise ServiceError(409, 'coverage:results_unavailable')
            return cell_entries(value['result']['cell_targets'], entries(value['result']), cell)
