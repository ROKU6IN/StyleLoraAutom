"""Pure protocol conversion. Upstream bodies and URLs never become error text."""
import base64
import json

from .types import LlmFailure, LlmReply
from .contracts import native_compatible

ANTHROPIC_VERSION = '2026-10-21'


def headers(protocol, token):
    if protocol == 'anthropic_messages':
        return {'x-api-key': token, 'anthropic-version': ANTHROPIC_VERSION}
    return {'Authorization': 'Bearer ' + token}


def build(endpoint, request, image=None):
    config = endpoint['config']
    protocol = config['protocol']
    text = json.dumps(request.payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    system = request.system_text
    if request.response_schema:
        system += '\nReturn only JSON matching this schema:\n' + json.dumps(request.response_schema, ensure_ascii=False)
    schema = request.response_schema if config['supports_json_schema'] and native_compatible(request.response_schema) else None
    sampling = dict(request.sampling)
    maximum = sampling.pop('max_tokens', 4096)
    encoded = base64.b64encode(image).decode('ascii') if image else None
    body = {'model': request.binding.model, 'stream': False}
    if protocol == 'openai_chat':
        parts = [{'type': 'text', 'text': text}]
        if encoded:
            parts.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + encoded}})
        body.update(messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': parts}],
                    max_completion_tokens=maximum, store=False, **sampling)
        if schema:
            body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'result', 'strict': True, 'schema': schema}}
        path = '/chat/completions'
    elif protocol == 'openai_responses':
        parts = [{'type': 'input_text', 'text': text}]
        if encoded:
            parts.append({'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + encoded})
        body.update(instructions=system, input=[{'role': 'user', 'content': parts}], max_output_tokens=maximum, store=False, **sampling)
        if schema:
            body['text'] = {'format': {'type': 'json_schema', 'name': 'result', 'strict': True, 'schema': schema}}
        path = '/responses'
    elif protocol == 'anthropic_messages':
        if sampling:
            raise LlmFailure('unsupported_sampling', outcome='not_sent')
        parts = [{'type': 'text', 'text': text}]
        if encoded:
            parts.insert(0, {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': encoded}})
        body.update(system=system, messages=[{'role': 'user', 'content': parts}], max_tokens=maximum)
        if schema:
            body['output_config'] = {'format': {'type': 'json_schema', 'schema': schema}}
        path = '/messages'
    else:
        raise LlmFailure('unsupported_protocol', outcome='not_sent')
    return config['base_url'] + path, body


def parse(endpoint, request, value, tier, attempts):
    try:
        config = endpoint['config']
        protocol = config['protocol']
        model = next(m for m in config['models'] if m['name'] == request.binding.model)
        if value['model'] not in (model['name'], *model['response_aliases']):
            raise LlmFailure('model_mismatch')
        if protocol == 'openai_chat':
            choice = value['choices'][0]
            if choice['message'].get('refusal'):
                raise LlmFailure('refused')
            if choice.get('finish_reason') != 'stop':
                raise LlmFailure('incomplete_reply')
            text = choice['message']['content']
        else:
            if protocol == 'openai_responses':
                if value.get('status') != 'completed':
                    raise LlmFailure('incomplete_reply')
                parts = [part for item in value['output'] if item['type'] == 'message' for part in item['content']]
                kind = 'output_text'
            else:
                if value.get('stop_reason') == 'refusal':
                    raise LlmFailure('refused')
                if value.get('stop_reason') != 'end_turn':
                    raise LlmFailure('incomplete_reply')
                parts, kind = value['content'], 'text'
            if any(part['type'] == 'refusal' for part in parts):
                raise LlmFailure('refused')
            text = ''.join(part['text'] for part in parts if part['type'] == kind)
        if not isinstance(text, str) or not text:
            raise ValueError()
        usage = value.get('usage')
        # Never forward arbitrary provider metadata in usage.
        usage = {k: v for k, v in usage.items() if k in ('input_tokens', 'output_tokens', 'prompt_tokens',
                 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0} if isinstance(usage, dict) else None
        return LlmReply(text, value['model'], request.request_id, usage, tier, attempts)
    except LlmFailure:
        raise
    except (KeyError, IndexError, TypeError, ValueError, StopIteration):
        raise LlmFailure('invalid_reply') from None
