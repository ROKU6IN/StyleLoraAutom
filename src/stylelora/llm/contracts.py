import copy
import math

from ..runtime.contracts import strict_json
from .types import LlmFailure


def native_compatible(schema):
    """A deliberately small portable subset; full schemas still validate locally."""
    if not isinstance(schema, dict) or set(schema) - {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'description'}:
        return False
    if schema.get('type') == 'object' or 'properties' in schema:
        if schema.get('additionalProperties') is not False or set(schema.get('required', ())) != set(schema.get('properties', {})):
            return False
        if not all(native_compatible(child) for child in schema.get('properties', {}).values()):
            return False
    return 'items' not in schema or native_compatible(schema['items'])


def validator(schema):
    # Use a local validator with no external schema retrieval.
    from jsonschema import Draft202012Validator
    schema = copy.deepcopy(schema)

    def walk(value, partial=False):
        if isinstance(value, dict):
            if any(k in value and not value[k].startswith('#') for k in ('$ref', '$dynamicRef')):
                raise ValueError('External schema references are not allowed')
            if not partial and (value.get('type') == 'object' or 'properties' in value):
                value.setdefault('additionalProperties', False)
            for key, child in value.items():
                walk(child, key in ('if', 'then', 'else', 'allOf', 'anyOf', 'oneOf', 'not'))
        elif isinstance(value, list):
            for child in value:
                walk(child, partial)
    try:
        walk(schema)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)
    except Exception:
        raise LlmFailure('invalid_schema', outcome='not_sent') from None


def validate_reply(text, check):
    try:
        value = strict_json(text)
        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError()
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)
        finite(value)
        if not check.is_valid(value):
            raise ValueError()
        return value
    except Exception:
        raise LlmFailure('invalid_json') from None
