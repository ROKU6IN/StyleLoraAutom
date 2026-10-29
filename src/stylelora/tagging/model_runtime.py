"""Optional ONNX sessions and strict manifest tensor validation."""
from importlib import metadata

from ..runtime.errors import ServiceError


def cuda_libraries():
    result = {}
    for name in ('nvidia-cuda-runtime', 'nvidia-cudnn-cu13', 'nvidia-cublas'):
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = None
    return result


def runtime_status():
    found = []
    for name in ('onnxruntime', 'onnxruntime-gpu'):
        try:
            found.append((name, metadata.version(name)))
        except metadata.PackageNotFoundError:
            pass
    if len(found) > 1:
        return {'available': False, 'reason': 'tagger:runtime_conflict', 'version': None}
    if not found:
        return {'available': False, 'reason': 'tagger:runtime_missing', 'version': None}
    try:
        import onnxruntime as ort
        if 'CPUExecutionProvider' not in ort.get_available_providers():
            raise ImportError()
    except Exception:
        return {'available': False, 'reason': 'tagger:runtime_unavailable', 'version': found[0][1]}
    return {'available': True, 'reason': None, 'version': found[0][1],
            'cuda_available': 'CUDAExecutionProvider' in ort.get_available_providers(),
            'cuda_libraries': cuda_libraries() if found[0][0] == 'onnxruntime-gpu' else {}}


def create_session(manifest, path, provider='cpu', *, profile_prefix=None):
    status = runtime_status()
    if not status['available']:
        raise ServiceError(503, status['reason'])
    try:
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.log_severity_level = 4
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        if profile_prefix is not None:
            options.enable_profiling = True
            options.profile_file_prefix = str(profile_prefix)
        # Bytes forbid resolving external tensor files outside the checked bundle.
        if provider == 'cuda':
            if not status.get('cuda_available'):
                raise ServiceError(503, 'tagger:cuda_unavailable')
            ort.preload_dlls(directory='')
        providers = [('CUDAExecutionProvider', {'device_id': 0, 'use_tf32': 0,
            'arena_extend_strategy': 'kSameAsRequested'}), 'CPUExecutionProvider'] if provider == 'cuda' else ['CPUExecutionProvider']
        session = ort.InferenceSession(path.read_bytes(), sess_options=options, providers=providers)
        if provider == 'cuda' and 'CUDAExecutionProvider' not in session.get_providers():
            raise ValueError()
        session.disable_fallback()
        validate_session(manifest, session)
        return session
    except ServiceError:
        raise
    except Exception:
        raise ServiceError(503, 'tagger:cuda_unavailable' if provider == 'cuda' else 'tagger:tensor_mismatch') from None


def validate_session(manifest, session):
    inputs, outputs = session.get_inputs(), session.get_outputs()
    schema = manifest.tensor_schema
    if len(inputs) != 1 or len(outputs) != 1:
        raise ServiceError(503, 'tagger:tensor_mismatch')
    inp, out = inputs[0], outputs[0]
    dims = [schema.size, schema.size, 3] if schema.layout == 'NHWC' else [3, schema.size, schema.size]
    def batch_ok(dimension):
        return dimension == 1 if schema.batch == 'one' else (dimension is None or isinstance(dimension, str))
    if (inp.name != schema.input_name or out.name != schema.output_name
            or inp.type != 'tensor(float)' or out.type != 'tensor(float)'
            or len(inp.shape) != 4 or inp.shape[1:] != dims or not batch_ok(inp.shape[0])
            or len(out.shape) != 2 or out.shape[1] != schema.output_count or not batch_ok(out.shape[0])):
        raise ServiceError(503, 'tagger:tensor_mismatch')


def validate_tensors(manifest, path):
    create_session(manifest, path)
