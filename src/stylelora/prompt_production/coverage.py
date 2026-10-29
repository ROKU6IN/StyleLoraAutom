"""Product-level coverage templates. JSON on disk, bounded JSON/YAML exchange."""
from copy import deepcopy
import itertools
import json
import math

from pydantic import Field, ValidationError, field_validator
import yaml

from ..project_db.validation import digest, encode
from ..runtime.contracts import Versions, strict_json, uuid_string
from ..runtime.errors import ServiceError
from .sampling import cell_id
from .types import Digest, Identifier, Plan, Strict
from .deletion import LibraryDeletion
from typing import Literal

MAX_TEMPLATE = 1024 * 1024
MAX_NODES = 100000
MAX_DEPTH = 32


class Template(Strict):
    schema_version: int = Field(default=1, ge=1, le=1)
    name: str = Field(min_length=1, max_length=200)
    plan: Plan

    @field_validator('name')
    @classmethod
    def name_valid(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid name')
        return value.strip()

    @field_validator('plan')
    @classmethod
    def weights_valid(cls, plan):
        cells = {cell_id(plan.primary_dimensions, values) for values in itertools.product(
            *(plan.cell_values[name] for name in plan.primary_dimensions))}
        if set(plan.cell_weights) - cells:
            raise ValueError('Unknown cell weight')
        return plan


class Document(Strict):
    format: Literal['json', 'yaml'] = 'json'
    content: str = Field(max_length=MAX_TEMPLATE)


class Create(Document):
    coverage_id: Identifier


class Stored(Template):
    coverage_id: Identifier
    revision: int = Field(ge=1)


class ReceiptResult(Strict):
    coverage_id: Identifier
    revision: int = Field(ge=1)
    version: Digest


class Receipt(Strict):
    digest: Digest
    result: ReceiptResult


class TemplateLoader(yaml.SafeLoader):
    """Never construct objects, expand aliases, merge maps or accept duplicate keys."""
    def __init__(self, stream):
        super().__init__(stream)
        self.template_nodes = self.template_depth = 0

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, 'anchor', None) is not None:
            raise ValueError('Anchors and aliases are not template data')
        self.template_nodes += 1
        self.template_depth += 1
        try:
            if self.template_nodes > MAX_NODES or self.template_depth > MAX_DEPTH:
                raise ValueError('Template structure limit')
            node = super().compose_node(parent, index)
            if node.tag not in {'tag:yaml.org,2002:' + kind for kind in ('map', 'seq', 'str', 'int', 'float', 'bool', 'null')}:
                raise ValueError('Unsupported tag')
            return node
        finally:
            self.template_depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError('Non-string or duplicate key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse(document):
    try:
        data = document['content'].encode('utf-8')
        if len(data) > MAX_TEMPLATE:
            raise ServiceError(413, 'coverage:template_too_large')
        if document['format'] == 'json':
            value = strict_json(data.decode('utf-8-sig'))
        elif document['format'] == 'yaml':
            value = yaml.load(data.decode('utf-8-sig'), Loader=TemplateLoader)
        else:
            raise ValueError('Unknown format')
        # Equal limits for JSON and YAML, including finite scalar values.
        stack, count = [(value, 1)], 0
        while stack:
            item, depth = stack.pop()
            count += 1
            if depth > MAX_DEPTH or count > MAX_NODES:
                raise ValueError('Template structure limit')
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for pair in item.items() for child in pair)
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError('Non-finite scalar')
        result = Template.model_validate(value).model_dump()
        if len(encode(result).encode('utf-8')) > MAX_TEMPLATE:
            raise ServiceError(413, 'coverage:template_too_large')
        return result
    except ServiceError as error:
        if error.code == 'coverage:template_too_large':
            raise
        raise ServiceError(422, 'coverage:invalid_template') from None
    except (ValueError, TypeError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ServiceError(422, 'coverage:invalid_template') from None


def view(template):
    plan = template['plan']
    seed = plan['random_seed']
    # Keep uint64 inside text across the browser JSON boundary, never round it.
    return {'template': json.dumps(template, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2), 'name': template['name'],
            'target_count': plan['target_count'], 'random_seed': str(seed) if seed is not None else None,
            'browser_compatible': seed is None or seed <= 2**53 - 1,
            'primary_dimensions': plan['primary_dimensions'],
            'cell_count': math.prod(len(values) for values in plan['cell_values'].values())}


class CoveragePlans:
    def __init__(self, runtime, files):
        self.runtime, self.files, self.lock = runtime, files, files.lock
        self.deletions = LibraryDeletion(self, 'coverage')

    def load(self, identifier):
        uuid_string(identifier)
        self.deletions.guard(identifier)
        value = self.files.get('coverage', identifier)
        try:
            payload = {k: v for k, v in value.items() if k != '_operations'}
            if Stored.model_validate(payload).model_dump() != payload or payload['coverage_id'] != identifier:
                raise ValueError()
            operations = value['_operations']
            if not isinstance(operations, dict) or len(operations) > 4096:
                raise ValueError()
            for key, raw in operations.items():
                uuid_string(key)
                receipt = Receipt.model_validate(raw)
                if receipt.result.coverage_id != identifier or receipt.result.revision > payload['revision']:
                    raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, ValidationError, ServiceError):
            raise ServiceError(503, 'prompt:corrupt_snapshot') from None
        return value

    @staticmethod
    def receipt(value):
        return {'coverage_id': value['coverage_id'], 'revision': value['revision'],
                'version': digest({k: v for k, v in value.items() if k != '_operations'})}

    def get(self, identifier):
        with self.lock:
            value = self.load(identifier)
            return {**self.receipt(value), **view({k: value[k] for k in ('schema_version', 'name', 'plan')})}

    def list(self):
        with self.lock:
            return [{k: v for k, v in self.get(identifier).items() if k != 'template'}
                    for identifier in self.files.ids('coverage') if self.deletions.marker(identifier) is None]

    def save(self, identifier, document, expected, key, *, create=False):
        uuid_string(key)
        signature = digest([create, identifier, document])
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
                    raise ServiceError(409, 'coverage:request_key_conflict')
                return deepcopy(receipt['result'])
            if create and old:
                raise ServiceError(409, 'coverage:already_exists')
            if create:
                self.deletions.check_create_key(key)
                for other in self.files.ids('coverage'):
                    if self.deletions.marker(other):
                        continue
                    if key in self.load(other)['_operations']:
                        raise ServiceError(409, 'coverage:request_key_conflict')
            else:
                Versions.require(expected, self.receipt(old)['version'])
            payload = parse(document)
            operations = deepcopy(old['_operations']) if old else {}
            if len(operations) >= 4096:
                raise ServiceError(409, 'coverage:receipt_limit')
            payload.update(coverage_id=identifier, revision=old['revision'] + 1 if old else 1)
            result = self.receipt(payload)
            operations[key] = {'digest': signature, 'result': result}
            self.files.put('coverage', identifier, {**payload, '_operations': operations})
            self.runtime.emit('prompt.coverage_changed', {'coverage_id': identifier, 'revision': payload['revision']},
                              resource={'kind': 'coverage', 'id': identifier}, version=result['version'])
            return result

    def export(self, identifier, format):
        if format not in ('json', 'yaml'):
            raise ServiceError(422, 'coverage:invalid_format')
        with self.lock:
            value = self.get(identifier)
            template = strict_json(value['template'])
            content = value['template'] + '\n' if format == 'json' else yaml.safe_dump(
                template, allow_unicode=True, sort_keys=False, default_flow_style=False)
            if len(content.encode('utf-8')) > MAX_TEMPLATE:
                raise ServiceError(413, 'coverage:template_too_large')
            return {'filename': f'coverage-{identifier}.{format}', 'content': content,
                    'media_type': 'application/json' if format == 'json' else 'application/yaml'}
