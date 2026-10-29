"""Durable prepare fence. This is not evidence of external execution or ownership."""
from uuid import UUID

from ..project_db.storage import atomic_json
from ..project_db.validation import digest, relative_path
from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError
from .costs import _hash


def path_for(project, item_id, attempt):
    if any(type(v) is not int or v < 1 for v in (item_id, attempt)):
        raise ServiceError(422, 'generation:invalid_attempt')
    return relative_path(project.root, f'images/.operations/{item_id}/{attempt}.json')


def read_operation(project, item_id, attempt):
    try:
        path = path_for(project, item_id, attempt)
        if not path.exists():
            return None
        with path.open('rb') as stream:
            data = stream.read(8193)
        if len(data) > 8192:
            raise ValueError()
        envelope = strict_json(data)
        row = envelope['content']
        if set(envelope) != {'content', 'digest'} or envelope['digest'] != digest(row):
            raise ValueError()
        if (set(row) != {'schema_version', 'source_id', 'item_id', 'attempt', 'operation_key',
                        'lease_id', 'instance_id', 'contract_digest', 'request_digest', 'config_digest', 'closed'}
                or type(row['schema_version']) is not int or row['schema_version'] != 1
                or type(row['source_id']) is not int or row['source_id'] < 1
                or type(row['item_id']) is not int or row['item_id'] != item_id
                or type(row['attempt']) is not int or row['attempt'] != attempt
                or type(row['closed']) is not bool
                or str(UUID(row['lease_id'])) != row['lease_id']
                or not all(_hash(row[k]) for k in ('operation_key', 'instance_id', 'contract_digest',
                                                  'request_digest', 'config_digest'))):
            raise ValueError()
        return row
    except Exception:
        raise ServiceError(503, 'generation:invalid_operation') from None


def write_operation(project, row, previous):
    try:
        if read_operation(project, row['item_id'], row['attempt']) != previous:
            raise ValueError()
        path = path_for(project, row['item_id'], row['attempt'])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {'content': row, 'digest': digest(row)})
        if read_operation(project, row['item_id'], row['attempt']) != row:
            raise ValueError()
    except Exception:
        try:
            if read_operation(project, row['item_id'], row['attempt']) == row:
                return
        except ServiceError:
            pass
        raise ServiceError(503, 'generation:operation_write_unknown', outcome='unknown') from None
