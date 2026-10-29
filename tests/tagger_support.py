"""Synthetic contracts only; none of these are shipped as a trusted WD manifest."""
import base64
import hashlib
import json

from stylelora.tagging.manifests import manifest_digest, parse_manifest

MODEL = base64.b64decode('CAg6jwEKPAoGcGl4ZWxzEgZzY29yZXMiClJlZHVjZU1lYW4qDQoEYXhlc0ABQAKgAQcqDwoIa2VlcGRpbXMYAKABAhITc3R5bGVsb3JhLXRlc3Qtb25seVogCgZwaXhlbHMSFgoUCAESEAoCCAEKAggCCgIIAgoCCANiGAoGc2NvcmVzEg4KDAgBEggKAggBCgIIA0IECgAQDQ==')
CSV = b'name,category\nblue_hair,0\nfixture_character,4\ngeneral,9\n'
RUNTIME = {'available': True, 'reason': None, 'version': 'test-only'}
DEVICE = {'provider': 'cpu', 'total_bytes': None, 'free_bytes': None, 'source': None,
          'reserved_bytes': None, 'cuda_verified': False, 'reason': 'tagger:cuda_unavailable'}


def fixture(*, model=MODEL, csv=CSV, model_id='synthetic-tagger', revision='a'*40, change=None):
    content = {'model.onnx': model, 'selected_tags.csv': csv}
    value = {'model_id': model_id, 'repo_id': 'test-only/synthetic-tagger', 'revision': revision,
             'files': [{'name': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in content.items()],
             'tensor_schema': {'input_name': 'pixels', 'output_name': 'scores', 'size': 2, 'layout': 'NHWC',
                               'dtype': 'float32', 'batch': 'one', 'color_order': 'BGR', 'resize': 'pillow-bicubic',
                               'normalization': '0_255', 'alpha': 'white', 'aspect': 'pad_square', 'output_count': 3},
             'preprocess_version': 'wd-square-v1',
             'csv_schema': {'columns': ['name', 'category'], 'name_column': 'name', 'category_column': 'category',
                            'general': 0, 'character': 4, 'rating': 9}}
    if change:
        change(value)
    value['digest'] = manifest_digest(value)
    return parse_manifest(json.dumps(value).encode()), content


class MemoryDownload:
    def __init__(self, content):
        self.content, self.calls = content, []

    def __call__(self, manifest, file, cancel):
        self.calls.append((manifest.revision, file.name))
        yield self.content[file.name]


def inference_fixture(**kwargs):
    return fixture(change=lambda v: v['tensor_schema'].update(normalization='0_1'), **kwargs)


def dynamic_fixture():
    model = base64.b64decode('CAg6mQEKPAoGcGl4ZWxzEgZzY29yZXMiClJlZHVjZU1lYW4qDQoEYXhlc0ABQAKgAQcqDwoIa2VlcGRpbXMYAKABAhITc3R5bGVsb3JhLXRlc3Qtb25seVolCgZwaXhlbHMSGwoZCAESFQoHEgViYXRjaAoCCAIKAggCCgIIA2IdCgZzY29yZXMSEwoRCAESDQoHEgViYXRjaAoCCANCBAoAEA0=')
    return fixture(model=model, change=lambda v: v['tensor_schema'].update(normalization='0_1', batch='dynamic'))


GPU_DEVICE = {**DEVICE, 'provider': 'cuda', 'total_bytes': 16*1024**3,
              'free_bytes': 12*1024**3, 'reserved_bytes': 4*1024**3,
              'gpu_name': 'test-only GPU', 'driver_version': 'test', 'runtime_version': 'test'}


def gpu_profile(manifest, runtime='test'):
    from stylelora.tagging.gpu_profiles import CUDA_POLICY
    return {'manifest_id': manifest.digest, 'runtime_version': runtime, 'policy': CUDA_POLICY,
            'gpu_name': GPU_DEVICE['gpu_name'], 'driver_version': 'test',
            'total_bytes': GPU_DEVICE['total_bytes'],
            'required_bytes': {'1': 1024**3, '2': 2*1024**3, '4': 4*1024**3}}
