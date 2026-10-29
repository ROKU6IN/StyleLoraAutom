from copy import deepcopy

from pydantic import ValidationError

from ..project_db.validation import digest, encode
from ..runtime.contracts import Versions, uuid_string
from ..runtime.errors import ServiceError
from ..vision.policy import RATINGS
from .types import DictionaryFile, DictionaryInput
from .deletion import LibraryDeletion


def entry_key(entry):
    return encode([sorted(set(entry['tags'])), ' '.join((entry['natural_language'] or '').split()), entry['aspect']])


def compact(value):
    """Dictionary duplicates differ from the stricter batch tag-only identity."""
    result = deepcopy(value)
    duplicates, issues = 0, []
    if value['kind'] == 'features':
        for name, rows in value['dimensions'].items():
            unique = {}
            for row in rows:
                previous = unique.get(row['tag'])
                if previous:
                    duplicates += 1
                    if previous['rating'] != row['rating']:
                        ratings = [r for r in (previous['rating'], row['rating']) if r is not None]
                        previous['rating'] = max(ratings, key=RATINGS.index) if ratings else None
                        issues.append({'code': 'dictionary:rating_merged', 'field': name, 'severity': 'warning'})
                else:
                    unique[row['tag']] = deepcopy(row)
            result['dimensions'][name] = list(unique.values())
    else:
        unique = {}
        for row in value['entries']:
            key = entry_key(row)
            if key in unique:
                duplicates += 1
                if unique[key]['rating'] != row['rating']:
                    raise ServiceError(409, 'dictionary:rating_conflict')
            else:
                unique[key] = row
        result['entries'] = list(unique.values())
    return result, duplicates, issues


class Dictionaries:
    def __init__(self, runtime, files):
        self.runtime, self.files, self.lock = runtime, files, files.lock
        self.deletions = LibraryDeletion(self, 'dictionary')

    def load(self, identifier):
        uuid_string(identifier)
        self.deletions.guard(identifier)
        found = []
        for kind in ('features', 'prompts'):
            try:
                value = self.files.get('dictionaries/' + kind, identifier)
            except ServiceError as error:
                if error.status == 404:
                    continue
                raise
            try:
                payload = {key: content for key, content in value.items() if key != '_operations'}
                # None fields inside entries/features are meaningful in the interchange format.
                parsed = DictionaryFile.model_validate(payload).model_dump()
                parsed.pop('entries' if kind == 'features' else 'dimensions')
                if parsed != payload or payload['dictionary_id'] != identifier or payload['kind'] != kind:
                    raise ValueError()
                operations = value['_operations']
                if not isinstance(operations, dict) or len(operations) > 4096:
                    raise ValueError()
                for key, operation in operations.items():
                    uuid_string(key)
                    if set(operation) != {'digest', 'result'} or not isinstance(operation['result'], dict) or len(operation['digest']) != 64:
                        raise ValueError()
                found.append(value)
            except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
                raise ServiceError(503, 'prompt:corrupt_snapshot') from None
        if len(found) > 1:
            raise ServiceError(503, 'prompt:corrupt_snapshot')
        if not found:
            raise ServiceError(404, 'resource:not_found')
        return found[0]

    @staticmethod
    def public(value):
        payload = {key: deepcopy(content) for key, content in value.items() if key != '_operations'}
        return {**payload, 'version': digest(payload)}

    def get(self, identifier):
        with self.lock:
            return self.public(self.load(identifier))

    def list(self):
        with self.lock:
            rows = []
            for kind in ('features', 'prompts'):
                for identifier in self.files.ids('dictionaries/' + kind):
                    if self.deletions.marker(identifier):
                        continue
                    value = self.get(identifier)
                    rows.append({key: value[key] for key in ('dictionary_id', 'name', 'kind', 'revision', 'version')} |
                                {'count': len(value['entries']) if kind == 'prompts' else sum(map(len, value['dimensions'].values()))})
            return sorted(rows, key=lambda row: row['dictionary_id'])

    def replay(self, identifier, key, signature):
        uuid_string(key)
        old = self.load(identifier)
        receipt = old['_operations'].get(key)
        if receipt is None:
            return None
        if receipt['digest'] != signature:
            raise ServiceError(409, 'dictionary:request_key_conflict')
        return deepcopy(receipt['result'])

    def save(self, identifier, body, expected, key, *, create=False, append=False, signature=None):
        uuid_string(key)
        signature = signature or digest([create, append, identifier, body])
        with self.lock:
            if self.runtime.draining:
                raise ServiceError(503, 'process:draining')
            try:
                old = self.load(identifier)
            except ServiceError as error:
                if error.status != 404 or not create:
                    raise
                old = None
            if old and key in old['_operations']:
                receipt = old['_operations'][key]
                if receipt['digest'] != signature:
                    raise ServiceError(409, 'dictionary:request_key_conflict')
                return deepcopy(receipt['result'])
            if create and old:
                raise ServiceError(409, 'dictionary:already_exists')
            if create:
                self.deletions.check_create_key(key)
                for kind in ('features', 'prompts'):
                    for other in self.files.ids('dictionaries/' + kind):
                        if self.deletions.marker(other):
                            continue
                        if other != identifier and key in self.load(other)['_operations']:
                            raise ServiceError(409, 'dictionary:request_key_conflict')
            if not create:
                Versions.require(expected, self.public(old)['version'])
            if old and old['kind'] != body['kind']:
                raise ServiceError(422, 'dictionary:kind_mismatch')
            payload = DictionaryInput.model_validate(body).model_dump()
            payload.pop('entries' if payload['kind'] == 'features' else 'dimensions')
            original_count = 0
            if append:
                if payload['kind'] == 'features':
                    original_count = sum(map(len, old['dimensions'].values()))
                    dimensions = deepcopy(old['dimensions'])
                    for name, rows in payload['dimensions'].items():
                        dimensions.setdefault(name, []).extend(rows)
                    payload['dimensions'] = dimensions
                else:
                    original_count = len(old['entries'])
                    payload['entries'] = old['entries'] + payload['entries']
                payload['name'] = old['name']
            value, duplicates, issues = compact(payload)
            # Check aggregate limits after appending/merging, not just the submitted portion.
            DictionaryInput.model_validate(value)
            operations = deepcopy(old['_operations']) if old else {}
            if len(operations) >= 4096:
                raise ServiceError(409, 'dictionary:receipt_limit')
            value.update(dictionary_id=identifier, revision=old['revision'] + 1 if old else 1)
            count = len(value['entries']) if value['kind'] == 'prompts' else sum(map(len, value['dimensions'].values()))
            result = {'dictionary_id': identifier, 'revision': value['revision'], 'version': digest(value),
                      'inserted': count - original_count, 'duplicates': duplicates, 'issues': issues[:100]}
            operations[key] = {'digest': signature, 'result': result}
            value['_operations'] = operations
            self.files.put('dictionaries/' + value['kind'], identifier, value)
            self.runtime.emit('prompt.dictionary_changed', {'dictionary_id': identifier, 'revision': value['revision']},
                              resource={'kind': 'dictionary', 'id': identifier}, version=result['version'])
            return result
