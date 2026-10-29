from copy import deepcopy
from uuid import uuid5, NAMESPACE_URL

from ..project_db.validation import digest
from ..runtime.errors import ServiceError
from .composer import gate
from .dictionaries import compact
from .types import DictionaryInput


class TaskAppend:
    def _selection(self, value, indices, automatic=False):
        if value['status'] != 'done':
            raise ServiceError(409, 'prompt:append_requires_done')
        outcomes = self.outcomes(value['task_id'])
        selected = [r for r in outcomes if r['index'] in indices]
        if len(selected) != len(indices) or any(r['status'] not in (('ok',) if automatic else ('ok', 'skipped')) for r in selected):
            raise ServiceError(422, 'prompt:invalid_selection')
        entries = []
        for row in selected:
            candidate = row.get('result', row)
            entry = deepcopy(candidate['entry'])
            entry['rating'] = candidate['rating']
            original = row.get('input', candidate)['entry']
            if gate(entry, original, value['snapshot']):
                raise ServiceError(422, 'prompt:invalid_selection')
            entries.append(entry)
        return entries

    def append_preview(self, identifier, body):
        with self.lock:
            value = self.load(identifier)
            entries = self._selection(value, body['indices'])
            target = self.dictionaries.get(body['target_id'])
            if target['kind'] != 'prompts':
                raise ServiceError(422, 'dictionary:kind_mismatch')
            merged, duplicates, issues = compact({'schema_version': 1, 'kind': 'prompts', 'name': target['name'],
                                                  'entries': target['entries'] + entries})
            DictionaryInput.model_validate(merged)
            return {**body, 'expected_revision': value['revision'], 'target_version': target['version'],
                    'selected': len(entries), 'inserted': len(merged['entries']) - len(target['entries']),
                    'duplicates': duplicates, 'issues': issues[:100], 'digest': digest(entries)}

    def append(self, identifier, body, automatic=False):
        with self.lock:
            value = self.load(identifier)
            entries = self._selection(value, body['indices'], automatic)
            signature = digest(['prompt-append', identifier, body['target_id'], entries])
            request_key = str(uuid5(NAMESPACE_URL, signature))
            # The dictionary receipt is authoritative across the two-file gap.
            result = self.dictionaries.replay(body['target_id'], request_key, signature)
            if result is None:
                if value['revision'] != body['expected_revision']:
                    raise ServiceError(409, 'prompt:revision_conflict')
                target = self.dictionaries.get(body['target_id'])
                result = self.dictionaries.save(body['target_id'], {'schema_version': 1, 'kind': 'prompts',
                    'name': target['name'], 'entries': entries}, body['target_version'], request_key,
                    append=True, signature=signature)
            marker = {'request_key': request_key, 'target_id': body['target_id'], 'result': result}
            if value['schema_version'] == 1:
                value['schema_version'] = 2
                value['execution'] = None
                value['appends'] = []
            if marker not in value['appends']:
                if len(value['appends']) >= 4096:
                    raise ServiceError(409, 'dictionary:receipt_limit')
                value['appends'].append(marker)
                value['revision'] += 1
                self.files.put('prompt_tasks', identifier, value)
                self.emit(value)
            return result
