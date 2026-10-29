"""Source-reviewed Launcher 4.3.1 reads; never authorizes a generation write.

Reference: Aaalice233/Aaalice_NAI_Launcher at
60b2129308de8b04df0798dee36ce21e7e6f3872. See docs/57 for source provenance.
The two input schemas below are declared in generation_tool_definitions.dart;
McpToolAdapter emits no outputSchema. Unrelated tools grant no permissions.
"""
import math
import re

from ..types import BackendError

REPOSITORY = 'https://github.com/Aaalice233/Aaalice_NAI_Launcher'
RELEASE = REPOSITORY + '/releases/tag/v4.3.1'
SOURCE_COMMIT = '60b2129308de8b04df0798dee36ce21e7e6f3872'
READONLY_TOOLS = frozenset(('get_generation_settings', 'get_generation_status'))
INPUT_SCHEMA = {'type': 'object', 'properties': {}, 'required': []}


def verify_readonly(client, name):
    if (name not in READONLY_TOOLS or client.info.app_version != '4.3.1'
            or client.server_version != '4.3.1' or client.server_name != 'nai-launcher'
            or client.protocol != '2025-11-25'):
        raise BackendError('backend:readonly_version_unreviewed', phase='compatibility')
    tool = next((tool for tool in client.tools if tool.name == name), None)
    if tool is None or tool.input_schema != INPUT_SCHEMA or tool.output_schema is not None:
        raise BackendError('backend:readonly_schema_mismatch', phase='compatibility')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value):
        raise ValueError()
    return value


def integer(value, low=0, high=10000000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError()
    return value


def settings_view(raw):
    """Project no prompt, paths, credentials or unknown provider fields."""
    result = {key: identifier(raw[key]) for key in ('model', 'sampler', 'noise_schedule')}
    if raw['effort'] not in (None, 'medium', 'high') or raw['model_mode'] not in ('anime', 'furry'):
        raise ValueError()
    result.update(effort=raw['effort'], model_mode=raw['model_mode'])
    for key in ('steps', 'width', 'height'):
        result[key] = integer(raw[key], 1, 16384)
    result['uc_preset'] = integer(raw['uc_preset'], 0, 100)
    for key in ('scale', 'cfg_rescale'):
        value = raw[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1000:
            raise ValueError()
        result[key] = value
    for key in ('quality_toggle', 'variety_plus', 'decrisp', 'smea', 'smea_dyn'):
        if type(raw[key]) is not bool:
            raise ValueError()
        result[key] = raw[key]
    locked = raw.get('locked_by_effort', [])
    allowed = {'steps', 'sampler', 'uc_preset', 'negative_prompt', 'cfg_rescale', 'noise_schedule', 'variety_plus'}
    if not isinstance(locked, list) or len(locked) > 7 or any(not isinstance(k, str) or k not in allowed for k in locked):
        raise ValueError()
    result['locked_by_effort'] = list(locked)
    models = raw['available_models']
    if not isinstance(models, list) or not 1 <= len(models) <= 128:
        raise ValueError()
    result['available_models'] = []
    for model in models:
        model_id = identifier(model['id'])
        efforts = model.get('efforts', [])
        if not isinstance(efforts, list) or len(efforts) > 2 or any(v not in ('medium', 'high') for v in efforts):
            raise ValueError()
        result['available_models'].append({'id': model_id, 'efforts': list(efforts)})
    return result


def status_view(raw):
    generation, queue = raw['generation'], raw['queue']
    return {'generation': {'status': identifier(generation['status']), 'progress': integer(generation['progress'], 0, 100)},
            'queue': {**{k: integer(queue[k]) for k in ('pending', 'completed', 'failed')},
                      'execution': identifier(queue['execution']), 'session_progress': integer(queue['session_progress'], 0, 100)}}


async def inspect(client):
    await client.connect()
    for name in READONLY_TOOLS:
        verify_readonly(client, name)
    generation = client.generation
    settings = await client.call_readonly('get_generation_settings')
    status = await client.call_readonly('get_generation_status')
    if client.generation != generation:
        raise BackendError('backend:inspection_session_changed')
    try:
        import json
        result = {'settings': settings_view(settings.payload), 'activity': status_view(status.payload)}
        if client.info.token in json.dumps(result, ensure_ascii=False):
            raise ValueError()
        return {'status': 'ready', 'reason': None, 'version': '4.3.1', 'source_commit': SOURCE_COMMIT,
                'generation_enabled': False, **result}
    except (KeyError, TypeError, ValueError, OverflowError):
        raise BackendError('backend:readonly_invalid_response') from None
