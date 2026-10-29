"""Bounded, atomic product files; no project IDs, paths or external credentials."""
from copy import deepcopy
from pathlib import Path
import threading

from ..llm.secrets import safe_path
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, encode
from ..runtime.contracts import strict_json, uuid_string
from ..runtime.errors import ServiceError

MAX_FILE = 64 * 1024 * 1024
GROUPS = {'dictionaries/features', 'dictionaries/prompts', 'coverage', 'prompt_tasks', 'prompt_tombstones',
          'dictionary_tombstones', 'coverage_tombstones'}


def stored_size(value):
    return len((encode({'content': value, 'digest': digest(value)}) + '\n').encode('utf-8'))


class ProductFiles:
    def __init__(self, directory=None):
        self.directory = safe_path(Path(directory).absolute()) if directory else None
        self.lock = threading.RLock()
        self.memory = {}

    def path(self, group, identifier):
        uuid_string(identifier)
        if group not in GROUPS:
            raise ValueError('Invalid product group')
        return safe_path(self.directory / group / (identifier + '.json')) if self.directory else None

    def ids(self, group):
        with self.lock:
            if group not in GROUPS:
                raise ValueError('Invalid product group')
            if self.directory:
                folder = safe_path(self.directory / group)
                paths = sorted(folder.glob('*.json')) if folder.exists() else []
                if len(paths) > self.limit(group):
                    raise ServiceError(413, 'prompt:storage_limit')
                for path in paths:
                    safe_path(path)
                    uuid_string(path.stem)
                return [p.stem for p in paths]
            return sorted(identifier for kind, identifier in self.memory if kind == group)

    @staticmethod
    def limit(group):
        return 100000 if group.endswith('_tombstones') else 2048

    def size(self, group, identifier):
        with self.lock:
            path = self.path(group, identifier)
            if path is None:
                value = self.memory.get((group, identifier))
                return stored_size(value) if value is not None else 0
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0
            except OSError:
                raise ServiceError(503, 'prompt:io_error') from None

    def remove_checked(self, group, identifier, expected_digest):
        """Remove one UUID file only after a durable caller-owned tombstone exists."""
        with self.lock:
            try:
                value = self.get(group, identifier)
            except ServiceError as error:
                if error.status == 404:
                    return
                raise
            if digest(value) != expected_digest:
                raise ServiceError(409, 'prompt:cleanup_conflict')
            path = self.path(group, identifier)
            if path is None:
                self.memory.pop((group, identifier), None)
                return
            try:
                path.unlink()
            except OSError:
                if not path.exists():
                    return
                raise ServiceError(503, 'prompt:cleanup_incomplete', outcome='unknown') from None

    def get(self, group, identifier):
        with self.lock:
            path = self.path(group, identifier)
            if path is None:
                value = self.memory.get((group, identifier))
                if value is None:
                    raise ServiceError(404, 'resource:not_found')
                return deepcopy(value)
            try:
                if path.stat().st_size > MAX_FILE:
                    raise ValueError()
                envelope = strict_json(path.read_bytes())
                if set(envelope) != {'content', 'digest'} or digest(envelope['content']) != envelope['digest']:
                    raise ValueError()
                return envelope['content']
            except FileNotFoundError:
                raise ServiceError(404, 'resource:not_found') from None
            except Exception:
                raise ServiceError(503, 'prompt:corrupt_snapshot') from None

    def put(self, group, identifier, value):
        with self.lock:
            path = self.path(group, identifier)
            envelope = {'content': value, 'digest': digest(value)}
            if len(encode(envelope).encode('utf-8')) > MAX_FILE:
                raise ServiceError(413, 'prompt:storage_limit')
            if path is None:
                if (group, identifier) not in self.memory and len(self.ids(group)) >= self.limit(group):
                    raise ServiceError(413, 'prompt:storage_limit')
                self.memory[group, identifier] = deepcopy(value)
                return
            if not path.exists() and len(self.ids(group)) >= self.limit(group):
                raise ServiceError(413, 'prompt:storage_limit')
            try:
                safe_path(path.parent).mkdir(parents=True, exist_ok=True)
                atomic_json(safe_path(path), envelope)
            except OSError:
                # os.replace may have committed before a later durability failure.
                try:
                    if self.get(group, identifier) == value:
                        return
                except ServiceError:
                    pass
                raise ServiceError(503, 'prompt:io_error') from None
