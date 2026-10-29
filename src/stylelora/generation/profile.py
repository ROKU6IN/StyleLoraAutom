import re

from ..project_db.validation import digest
from ..runtime.errors import ServiceError
from .types import Configuration


SIZES = {
    'small': {'landscape': {'width': 768, 'height': 512}, 'portrait': {'width': 512, 'height': 768}, 'square': {'width': 640, 'height': 640}},
    'large': {'landscape': {'width': 1536, 'height': 1024}, 'portrait': {'width': 1024, 'height': 1536}, 'square': {'width': 1472, 'height': 1472}},
}


def locked_projection(source):
    return {'type': source.type, 'config': source.config_json, 'style_prompt': source.style_prompt,
            'negative_prompt': source.negative_prompt, 'target_count': source.target_count}


def build_request(source, item):
    config = Configuration.model_validate(source.config_json)
    aspect = item.aspect or config.default_aspect
    return {'seq': item.seq, 'positive': ', '.join(part.strip() for part in (source.style_prompt, item.prompt) if part.strip()),
            'negative': source.negative_prompt, 'aspect': aspect, **config.sizes[aspect].model_dump(),
            'count': 1, 'operation': 'generate', 'characters': []}


def validate_capabilities(config, negative, capabilities):
    """Only a reviewed adapter can supply this configuration schema."""
    import jsonschema
    try:
        if set(capabilities) != {'backend', 'contract_digest', 'config_schema', 'negative_locked_by_effort'}:
            raise ValueError()
        if capabilities['backend'] != config['backend'] or not isinstance(capabilities['contract_digest'], str) or not re.fullmatch('[0-9a-f]{64}', capabilities['contract_digest']):
            raise ValueError()
        locks = capabilities['negative_locked_by_effort']
        if not isinstance(locks, list) or any(not isinstance(effort, str) or not effort.strip() for effort in locks):
            raise ValueError()
        schema = capabilities['config_schema']
        if not isinstance(schema, dict) or schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
            raise ValueError()
        if any(marker in str(schema) for marker in ('$ref', '$dynamicRef', '$recursiveRef')):
            raise ValueError()  # No remote schema retrieval during a preview.
        jsonschema.Draft202012Validator.check_schema(schema)
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(config))
    except Exception:
        raise ServiceError(503, 'generation:invalid_capabilities') from None
    if errors:
        # Never echo validator messages, submitted values, or arbitrary property
        # names (which may contain credentials) into an HTTP error.
        issues = [{'code': 'schema:' + error.validator, 'message': 'Unsupported parameter value',
                   'severity': 'error', 'field': 'config' + ''.join('.' + str(p) for p in error.absolute_path),
                   'row': None} for error in errors[:64]]
        raise ServiceError(422, 'generation:unsupported_configuration', issues=issues)
    if negative.strip() and config['profile']['effort'] in capabilities['negative_locked_by_effort']:
        raise ServiceError(422, 'generation:negative_locked')
    return digest(capabilities)
