"""Packaged, content-addressed runtime inputs. No arbitrary filesystem loading."""
import hashlib
from importlib.resources import files
import json

from .contracts import validator
from .types import LlmFailure


class TemplateRegistry:
    def __init__(self):
        root = files('stylelora.llm').joinpath('data')
        self.entries = json.loads(root.joinpath('manifest.json').read_text(encoding='utf-8'))
        self.loaded = {}
        for entry in self.entries:
            if not entry['available']:
                continue
            parts = {}
            for kind, item in entry['files'].items():
                raw = root.joinpath(item['name']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != item['sha256']:
                    raise LlmFailure('template_digest_mismatch')
                parts[kind] = raw.decode('utf-8')
            validator(json.loads(parts['schema']))
            self.loaded[entry['id']] = parts

    def views(self):
        return json.loads(json.dumps(self.entries))

    def snapshot(self, template_id):
        if template_id not in self.loaded:
            raise LlmFailure('template_unavailable', outcome='not_sent')
        entry = next(item for item in self.entries if item['id'] == template_id)
        return {'id': template_id, 'hashes': {kind: value['sha256'] for kind, value in entry['files'].items()},
                'system_text': self.loaded[template_id]['prompt'], 'response_schema': json.loads(self.loaded[template_id]['schema'])}
