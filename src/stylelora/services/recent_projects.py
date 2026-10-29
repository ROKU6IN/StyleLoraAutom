"""Bounded product history; only successful, explicitly authorized opens enroll roots."""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import threading
from uuid import uuid4

from ..exchange.common import is_link
from ..llm.secrets import safe_path
from ..project_db.errors import ProjectError
from ..project_db.storage import atomic_json, read_metadata
from ..project_db.validation import digest, encode, relative_path, utc_now
from ..runtime.contracts import strict_json, uuid_string
from ..runtime.errors import ServiceError

MAX_RECENT = 200
MAX_BYTES = 2*1024*1024


@contextmanager
def read_lock(root):
    """Inspect the existing lock without creating/writing project files or opening SQLite."""
    path = relative_path(root, '.project.lock')
    try:
        stream = path.open('rb')
    except FileNotFoundError:
        raise ServiceError(409, 'recent:lock_missing') from None
    locked = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBRLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            raise ServiceError(423, 'project:locked') from None
        locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def validate_root(row):
    raw = Path(row['path'])
    try:
        if (not raw.is_absolute() or str(raw).startswith(('\\\\', '//')) or raw.suffix != '.slproj' or
            any(is_link(p) for p in (raw, *raw.parents))):
            raise ServiceError(409, 'recent:unsafe')
        info = raw.stat()
        if [info.st_dev, info.st_ino] != row['identity']:
            raise ServiceError(409, 'recent:replaced')
        if not raw.is_dir():
            raise ServiceError(409, 'recent:invalid_project')
        meta = relative_path(raw, 'project.json')
        if meta.stat().st_size > 65536:
            raise ServiceError(409, 'recent:invalid_project')
        metadata = read_metadata(raw)
        if metadata.created_at != row['created_at']:
            raise ServiceError(409, 'recent:replaced')
        if not relative_path(raw, 'project.db').is_file() or any(not relative_path(raw, name).is_dir() for name in ('images', 'exports')):
            raise ServiceError(409, 'recent:invalid_project')
        return raw, metadata
    except FileNotFoundError:
        raise ServiceError(404, 'recent:missing') from None
    except PermissionError:
        raise ServiceError(403, 'recent:unreadable') from None
    except (OSError, ValueError, ProjectError):
        raise ServiceError(409, 'recent:invalid_project') from None


class RecentProjects:
    def __init__(self, runtime, directory=None):
        self.runtime, self.lock = runtime, threading.RLock()
        self.path = Path(directory)/'known-projects.json' if directory is not None else None
        self.memory = {'schema_version': 1, 'revision': 0, 'projects': {}}

    def _load(self):
        if self.path is None:
            return deepcopy(self.memory)
        try:
            path = safe_path(self.path)
            if not path.exists():
                return {'schema_version': 1, 'revision': 0, 'projects': {}}
            if path.stat().st_size > MAX_BYTES:
                raise ValueError()
            envelope = strict_json(path.read_bytes())
            if set(envelope) != {'content', 'digest'} or envelope['digest'] != digest(envelope['content']):
                raise ValueError()
            value = envelope['content']
            if (set(value) != {'schema_version', 'revision', 'projects'} or value['schema_version'] != 1 or
                type(value['revision']) is not int or value['revision'] < 0 or
                not isinstance(value['projects'], dict) or len(value['projects']) > MAX_RECENT):
                raise ValueError()
            paths = set()
            for identifier, row in value['projects'].items():
                uuid_string(identifier)
                if (set(row) != {'known_id', 'path', 'identity', 'created_at', 'name', 'last_opened_at', 'ordinal'} or
                    row['known_id'] != identifier or not isinstance(row['path'], str) or len(row['path']) > 32768 or
                    not Path(row['path']).is_absolute() or not isinstance(row['identity'], list) or len(row['identity']) != 2 or
                    any(type(i) is not int or i < 0 for i in row['identity']) or
                    any(not isinstance(row[key], str) for key in ('name', 'created_at', 'last_opened_at')) or
                    len(row['name']) > 512 or type(row['ordinal']) is not int or not 1 <= row['ordinal'] <= value['revision']):
                    raise ValueError()
                normalized = os.path.normcase(row['path'])
                if normalized in paths:
                    raise ValueError()
                paths.add(normalized)
            return value
        except OSError:
            raise ServiceError(503, 'recent:storage_unreadable') from None
        except Exception:
            raise ServiceError(503, 'recent:storage_invalid') from None

    def _commit(self, value):
        envelope = {'content': value, 'digest': digest(value)}
        if len(encode(envelope).encode('utf-8')) > MAX_BYTES:
            raise ServiceError(413, 'recent:storage_limit')
        if self.path is None:
            self.memory = deepcopy(value)
            return
        try:
            path = safe_path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(path, envelope)
        except Exception:
            # Atomic replacement may have succeeded before the caller saw an error.
            if self._load() != value:
                raise ServiceError(503, 'recent:save_failed') from None

    def remember(self, handle, *, touch=True):
        root = handle.root
        info = root.stat()
        normalized = os.path.normcase(str(root))
        with self.lock:
            state = self._load()
            old = next((r for r in state['projects'].values() if os.path.normcase(r['path']) == normalized), None)
            same = old and old['identity'] == [info.st_dev, info.st_ino] and old['created_at'] == handle.metadata.created_at
            if same and not touch and old['name'] == handle.metadata.name:
                return old['known_id']
            identifier = old['known_id'] if same else str(uuid4())
            if old and not same:
                del state['projects'][old['known_id']]
            state['revision'] += 1
            state['projects'][identifier] = {'known_id': identifier, 'path': str(root), 'identity': [info.st_dev, info.st_ino],
                'created_at': handle.metadata.created_at, 'name': handle.metadata.name,
                'last_opened_at': utc_now() if touch or not same else old['last_opened_at'],
                'ordinal': state['revision'] if touch or not same else old['ordinal']}
            if len(state['projects']) > MAX_RECENT:
                oldest = min(state['projects'].values(), key=lambda r: (r['ordinal'], r['known_id']))
                del state['projects'][oldest['known_id']]
            self._commit(state)
        self.runtime.emit('projects.changed', {'known_id': identifier})
        return identifier

    def snapshot(self, identifiers=None):
        with self.lock:
            state = self._load()
        if identifiers is None:
            values = list(state['projects'].values())
        else:
            if len(identifiers) != len(set(identifiers)):
                raise ServiceError(422, 'recent:duplicate_ids')
            if any(identifier not in state['projects'] for identifier in identifiers):
                raise ServiceError(404, 'recent:not_found')
            values = [state['projects'][i] for i in identifiers]
        return state['revision'], sorted(values, key=lambda r: (-r['ordinal'], r['known_id']))

    def resolve(self, known_id):
        _, values = self.snapshot([uuid_string(known_id)])
        path, _ = validate_root(values[0])
        return path, values[0]

    def public(self, row):
        view = {key: row[key] for key in ('known_id', 'name', 'last_opened_at')}
        view.update(display_name=Path(row['path']).name, project_id=None, status='available', reason=None)
        try:
            root, metadata = validate_root(row)
            view['name'] = metadata.name
            with self.runtime.condition:
                current = self.runtime.context
                if current and current.handle.root == root:
                    view.update(project_id=current.project_id, status='closing' if current.closing else 'current')
                    return view
            with read_lock(root):
                pass
        except ServiceError as error:
            view.update(status='unavailable', reason=error.code)
        except (OSError, ProjectError):
            view.update(status='unavailable', reason='recent:unreadable')
        return view

    def list(self, *, limit=50, cursor=None):
        revision, rows = self.snapshot()
        scope = digest(['recent', revision])
        state = self.runtime.cursors.decode(cursor, scope) if cursor else None
        offset = state['last'] if state else 0
        chosen = rows[offset:offset+limit]
        more = offset+len(chosen) < len(rows)
        return {'items': [self.public(row) for row in chosen], 'limit': limit, 'total': len(rows), 'upper_id': revision,
                'has_more': more, 'next_cursor': self.runtime.cursors.encode({'identity': scope, 'last': offset+limit}) if more else None}
