"""A deliberately bounded text-to-image contract, reviewed 2026-10-28.

Wire names: https://image.novelai.net/docs/doc.json . Model IDs are also
cross-checked against the fixed official Launcher source audit in docs/57.
No shared settings, automatic quality tags, references, or batch inheritance.
"""
from copy import deepcopy

from ...generation.profile import SIZES, validate_capabilities
from ...project_db.validation import digest
from ...runtime.errors import ServiceError


DEFAULT_CONFIG = {
    'schema_version': 1, 'backend': 'nai_direct', 'default_aspect': 'portrait',
    'sizes': deepcopy(SIZES['small']),
    'profile': {'model': 'nai-diffusion-5-full', 'effort': 'high',
        'sampler': 'k_euler_ancestral', 'steps': 23, 'scale': 5.0, 'cfg_rescale': 0.0,
        'noise_schedule': 'karras', 'uc_preset': None, 'quality_toggle': False,
        'smea': False, 'smea_dyn': False, 'variety_plus': False, 'decrisp': False},
}


def _object(properties):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


PROFILE_SCHEMA = _object({**{k: {'const': v} for k, v in DEFAULT_CONFIG['profile'].items()},
    'model': {'enum': ['nai-diffusion-4-5-full', 'nai-diffusion-5-full']},
    'steps': {'type': 'integer', 'minimum': 1, 'maximum': 50},
    'scale': {'type': 'number', 'minimum': 1, 'maximum': 10},
    'cfg_rescale': {'type': 'number', 'minimum': 0, 'maximum': 1},
})
SIZE_SCHEMA = _object({k: {'type': 'integer', 'minimum': 64, 'maximum': 1536, 'multipleOf': 64}
                       for k in ('width', 'height')})
CONFIG_SCHEMA = _object({'schema_version': {'const': 1}, 'backend': {'const': 'nai_direct'},
    'default_aspect': {'enum': ['portrait', 'landscape', 'square']},
    'sizes': _object({k: deepcopy(SIZE_SCHEMA) for k in SIZES['small']}), 'profile': PROFILE_SCHEMA})
# Changes to request semantics must bump the version as well as the schema hash.
CONTRACT = digest({'version': 'nai-direct-txt2img-v1', 'schema': CONFIG_SCHEMA})
INSTANCE = digest('NovelAI official direct image API')


def capabilities():
    return {'backend': 'nai_direct', 'contract_digest': CONTRACT,
            'config_schema': deepcopy(CONFIG_SCHEMA), 'negative_locked_by_effort': []}


def payload(config, request, seed):
    validate_capabilities(config, request['negative'], capabilities())
    if (request['operation'] != 'generate' or request['count'] != 1 or request['characters']
            or type(seed) is not int or not 0 <= seed < 2**32
            or any(type(request[k]) is not int or not 64 <= request[k] <= 1536 or request[k] % 64
                   for k in ('width', 'height'))):
        raise ServiceError(422, 'generation:unsupported_configuration')
    profile = config['profile']
    def caption(text):
        return {'caption': {'base_caption': text, 'char_captions': []}, 'use_coords': False, 'use_order': True}
    return {'input': request['positive'], 'model': profile['model'], 'action': 'generate', 'parameters': {
        'params_version': 3, 'width': request['width'], 'height': request['height'], 'n_samples': 1,
        'seed': seed, 'sampler': profile['sampler'], 'steps': profile['steps'], 'scale': profile['scale'],
        'cfg_rescale': profile['cfg_rescale'], 'noise_schedule': profile['noise_schedule'],
        'negative_prompt': request['negative'], 'qualityToggle': False, 'ucPreset': 3,
        'sm': False, 'sm_dyn': False, 'dynamic_thresholding': False, 'image_format': 'png',
        'v4_prompt': caption(request['positive']), 'v4_negative_prompt': caption(request['negative']),
        'legacy': False, 'legacy_v3_extend': False, 'deliberate_euler_ancestral_bug': False,
        'prefer_brownian': True,
    }}
