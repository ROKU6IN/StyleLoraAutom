"""Human decision to stop a direct source; never an upstream settlement proof."""
from uuid import uuid4
import re

from ..project_db import commands
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import sources
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, primitive, relative_path, utc_now
from ..runtime.contracts import Versions, strict_json
from ..runtime.errors import ServiceError
from .direct import bound_receipt
from .profile import locked_projection


def record_path(project, source_id):
    if type(source_id) is not int or source_id < 1:
        raise ServiceError(422, 'input:invalid')
    return relative_path(project.root, f'images/.nai-direct/reviews/{source_id}.json')


def read_record(project, source_id):
    try:
        path = record_path(project, source_id)
        if not path.exists():
            return None
        with path.open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError()
        envelope = strict_json(raw)
        row = envelope['content']
        if (set(envelope) != {'content', 'digest'} or envelope['digest'] != digest(row)
                or set(row) != {'schema_version', 'source_id', 'decision_id', 'action', 'phase',
                               'basis_digest', 'unknown_count', 'pending_count', 'created_at'}
                or type(row['schema_version']) is not int or row['schema_version'] != 1
                or type(row['source_id']) is not int or row['source_id'] != source_id
                or row['action'] != 'stop_source_preserve_unknown'
                or row['phase'] not in ('prepared', 'completed')
                or not isinstance(row['decision_id'], str) or not re.fullmatch('[0-9a-f]{32}', row['decision_id'])
                or not isinstance(row['basis_digest'], str) or not re.fullmatch('[0-9a-f]{64}', row['basis_digest'])
                or any(type(row[k]) is not int or not 0 <= row[k] <= 10000 for k in ('unknown_count', 'pending_count'))
                or row['unknown_count'] < 1 or not isinstance(row['created_at'], str)
                or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z', row['created_at'])):
            raise ValueError()
        return row
    except Exception:
        raise ServiceError(503, 'nai:manual_review_invalid') from None


def save_record(project, row, previous):
    try:
        if read_record(project, row['source_id']) != previous:
            raise ValueError()
        path = record_path(project, row['source_id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {'content': row, 'digest': digest(row)})
        if read_record(project, row['source_id']) != row:
            raise ValueError()
    except Exception:
        raise ServiceError(503, 'nai:manual_review_write_unknown', outcome='unknown') from None


def item_rows(db, source_id):
    rows = [dict(row) for row in db.execute(
        'SELECT item_id,seq,status,attempts,image_id FROM source_items WHERE source_id=? ORDER BY item_id LIMIT 10001', (source_id,))]
    if len(rows) > 10000:
        raise ServiceError(413, 'nai:manual_review_too_large')
    return rows


def evidence_basis(project, source, items, sent):
    evidence = []
    for item in sent:
        row, grant = bound_receipt(project, source, item)
        if row is None or grant is None:
            raise ServiceError(409, 'nai:manual_review_evidence_missing')
        if row['phase'] != 'intent':
            raise ServiceError(409, 'nai:local_response_available')
        evidence.append({'receipt': row, 'cost': grant})
    return digest({'source_id': source.source_id, 'created_at': source.created_at,
                   'config': locked_projection(source), 'items': items, 'evidence': evidence})


def inspect(runtime, context, source_id):
    """Caller owns generation_sources.lock; all reads are local and bounded."""
    with read_snapshot(context.handle) as db:
        source = runtime.generation_sources._source(db, source_id)
        if source.config_json.get('backend') != 'nai_direct':
            raise ServiceError(422, 'nai:manual_review_direct_only')
        items = item_rows(db, source_id)
        sent = [sources.get_item(db, row['item_id']) for row in items if row['status'] == 'sent']
    pending = sum(row['status'] == 'pending' for row in items)
    record, reason = None, None
    try:
        record = read_record(context.handle, source_id)
        basis = evidence_basis(context.handle, source, items, sent)
        if record and record['basis_digest'] != basis:
            reason = 'nai:manual_review_changed'
    except ServiceError as error:
        basis, reason = None, error.code
    busy = runtime.nai_direct.driver.recovery_busy() or runtime.generation_recovery.pending is not None
    if not reason:
        if record and record['phase'] == 'completed':
            reason = None
        elif not sent:
            reason = 'nai:no_unknown_attempts'
        elif source.status not in ('paused', 'canceled'):
            reason = 'recovery:pause_required'
        elif busy:
            reason = 'recovery:busy'
    completed = bool(record and record['phase'] == 'completed')
    version = runtime.versions.get(f'{context.project_id}:nai-manual-review:{source_id}',
        {'source': primitive(source), 'items': items, 'basis': basis, 'record': record, 'reason': reason, 'busy': busy})
    result = {'source_id': source_id, 'source_status': source.status,
              'unknown_count': len(sent), 'pending_count': pending,
              'unknown_items': [{'item_id': item.item_id, 'seq': item.seq, 'attempt': item.attempts} for item in sent[:100]],
              'items_truncated': len(sent) > 100, 'busy': busy, 'reason': reason,
              'can_stop': not reason and not completed and bool(sent) and not busy,
              'decision': {k: record[k] for k in ('decision_id', 'action', 'phase', 'unknown_count', 'pending_count', 'created_at')} if record else None,
              'upstream_outcome': 'unknown' if sent else 'see_item_records', 'version': version}
    return result, record, basis


def view(runtime, context, source_id):
    with runtime.generation_sources.lock:
        return inspect(runtime, context, source_id)[0]


def stop(runtime, context, source_id, expected):
    with runtime.generation_sources.lock:
        if context.closing or runtime.draining:
            raise ServiceError(409, 'project:closing')
        value, previous, basis = inspect(runtime, context, source_id)
        Versions.require(expected, value['version'])
        if previous and previous['phase'] == 'completed':
            if value['source_status'] != 'canceled' or value['reason']:
                raise ServiceError(409, 'nai:manual_review_changed')
            return value
        if not value['can_stop']:
            raise ServiceError(409, value['reason'] or 'recovery:busy')
        row = previous or {'schema_version': 1, 'source_id': source_id, 'decision_id': uuid4().hex,
            'action': 'stop_source_preserve_unknown', 'phase': 'prepared', 'basis_digest': basis,
            'unknown_count': value['unknown_count'], 'pending_count': value['pending_count'], 'created_at': utc_now()}
        try:
            if previous is None:
                save_record(context.handle, row, None)
            context.handle._fault('nai_review_after_prepare')
            def write(db):
                source = sources.get_source(db, source_id)
                items = item_rows(db, source_id)
                sent = [sources.get_item(db, item['item_id']) for item in items if item['status'] == 'sent']
                if (source.status not in ('paused', 'canceled')
                        or evidence_basis(context.handle, source, items, sent) != row['basis_digest']):
                    raise ServiceError(409, 'nai:manual_review_changed')
                commands.transition_source(db, source_id, expected=source.status, target='canceled',
                                           reason='nai:manual_stop_preserve_unknown')
            submit_write(context.handle, write).result()
            context.handle._fault('nai_review_after_cancel')
            save_record(context.handle, {**row, 'phase': 'completed'}, row)
            context.handle._fault('nai_review_after_complete')
            runtime.generation_sources._emit(context, source_id)
            return inspect(runtime, context, source_id)[0]
        except Exception:
            # Any stage may already be durable. A fresh GET and explicit action
            # can finish the same local decision; never infer upstream absence.
            raise ServiceError(503, 'nai:manual_review_write_unknown', outcome='unknown') from None
