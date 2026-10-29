"""Versioned library deletion with a durable barrier before payload removal."""
from typing import Literal

from pydantic import Field, ValidationError

from ..project_db.validation import digest, utc_now
from ..runtime.contracts import Versions, uuid_string
from ..runtime.errors import ServiceError
from .types import Digest, Identifier, Strict


class DeletedLibrary(Strict):
    schema_version: Literal[1] = 1
    kind: Literal['dictionary', 'coverage']
    resource_id: Identifier
    group: Literal['dictionaries/features', 'dictionaries/prompts', 'coverage']
    name: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    version: Digest
    content_digest: Digest
    operation_keys: list[Identifier] = Field(max_length=4096)
    request_key: Identifier
    request_digest: Digest
    deleted_at: str = Field(min_length=1, max_length=64)


class LibraryDeletion:
    def __init__(self, owner, kind):
        self.owner, self.kind = owner, kind
        self.files, self.runtime = owner.files, owner.runtime
        self.group = kind + '_tombstones'
        self.payload_groups = ('coverage',) if kind == 'coverage' else ('dictionaries/features', 'dictionaries/prompts')

    def marker(self, identifier):
        uuid_string(identifier)
        try:
            raw = self.files.get(self.group, identifier)
        except ServiceError as error:
            if error.status == 404:
                return None
            raise
        try:
            value = DeletedLibrary.model_validate(raw).model_dump()
            if (value != raw or value['resource_id'] != identifier or value['kind'] != self.kind
                    or value['group'] not in self.payload_groups or type(raw['schema_version']) is not int
                    or len(set(value['operation_keys'])) != len(value['operation_keys'])
                    or value['request_key'] in value['operation_keys']
                    or value['request_digest'] != digest([self.kind, identifier, value['version']])):
                raise ValueError()
        except (ValueError, KeyError, TypeError, ValidationError):
            raise ServiceError(503, 'prompt:corrupt_snapshot') from None
        return value

    def guard(self, identifier):
        if self.marker(identifier):
            raise ServiceError(410, self.kind + ':deleted')

    def check_create_key(self, key):
        # Keep old creation/update receipts reserved after the payload is removed.
        for identifier in self.files.ids(self.group):
            marker = self.marker(identifier)
            if key == marker['request_key'] or key in marker['operation_keys']:
                raise ServiceError(409, self.kind + ':request_key_conflict')

    @staticmethod
    def summary(marker):
        return {name: marker[name] for name in ('kind', 'resource_id', 'name', 'revision', 'version')}

    def pending(self):
        with self.files.lock:
            result = []
            for group in self.payload_groups:
                for identifier in self.files.ids(group):
                    marker = self.marker(identifier)
                    if marker:
                        result.append(self.summary(marker))
            return sorted(result, key=lambda row: row['resource_id'])

    def delete(self, identifier, expected, key):
        uuid_string(identifier)
        uuid_string(key)
        with self.files.lock:
            if self.runtime.draining:
                raise ServiceError(503, 'process:draining')
            marker = self.marker(identifier)
            if marker:
                Versions.require(expected, marker['version'])
                if key in marker['operation_keys']:
                    raise ServiceError(409, self.kind + ':request_key_conflict')
            else:
                value = self.owner.load(identifier)
                version = (self.owner.receipt(value) if self.kind == 'coverage' else self.owner.public(value))['version']
                Versions.require(expected, version)
                if key in value['_operations']:
                    raise ServiceError(409, self.kind + ':request_key_conflict')
                marker = {'schema_version': 1, 'kind': self.kind, 'resource_id': identifier,
                          'group': 'coverage' if self.kind == 'coverage' else 'dictionaries/' + value['kind'],
                          'name': value['name'], 'revision': value['revision'], 'version': version,
                          'content_digest': digest(value), 'operation_keys': sorted(value['_operations']),
                          'request_key': key, 'request_digest': digest([self.kind, identifier, expected]), 'deleted_at': utc_now()}
            for other in self.files.ids(self.group):
                if other == identifier:
                    continue
                previous = self.marker(other)
                if key == previous['request_key'] or key in previous['operation_keys']:
                    raise ServiceError(409, self.kind + ':request_key_conflict')
            # Both steps are serialized with every library read/write. If unlink
            # fails, the validated barrier still blocks reads, appends and recreation.
            try:
                self.files.put(self.group, identifier, marker)
            except ServiceError as error:
                if error.code == 'prompt:io_error':
                    # A failed durability/readback step may follow os.replace.
                    # Do not claim the deletion was rejected or the marker exists.
                    raise ServiceError(503, 'library:delete_unknown', outcome='unknown') from None
                raise
            try:
                self.files.remove_checked(marker['group'], identifier, marker['content_digest'])
            except ServiceError as error:
                if error.code == 'prompt:cleanup_conflict':
                    raise ServiceError(409, 'library:delete_conflict') from None
                raise ServiceError(503, 'library:delete_incomplete', outcome='unknown') from None
            self.runtime.emit('prompt.' + self.kind + '_deleted',
                              {'resource_id': identifier, 'revision': marker['revision']},
                              resource={'kind': self.kind, 'id': identifier}, version=digest(marker))
            return {**self.summary(marker), 'deleted': True}
