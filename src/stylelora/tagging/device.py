"""Bounded, read-only device telemetry; visible CUDA hardware is not a verified provider."""
import os
from pathlib import Path
import subprocess

from .model_runtime import runtime_status
from .gpu_profiles import matching_profile


def _nvml():
    import pynvml
    pynvml.nvmlInit()
    try:
        info = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0))
        return int(info.total), int(info.free)
    finally:
        pynvml.nvmlShutdown()


def _smi():
    text = _query_smi('memory.total,memory.free')
    total, free = (int(v.strip()) * 1024**2 for v in text.strip().split(','))
    return total, free


def _query_smi(fields):
    # Use known installation paths, never an executable from the working directory.
    roots = [Path(os.environ.get('SystemRoot', 'C:/Windows'))/'System32',
             Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'NVIDIA Corporation/NVSMI']
    executable = next((p/'nvidia-smi.exe' for p in roots if (p/'nvidia-smi.exe').is_file()), None)
    if executable is None:
        raise OSError()
    result = subprocess.run([str(executable), '--query-gpu='+fields, '--format=csv,noheader,nounits', '--id=0'],
                            capture_output=True, timeout=3, check=True, text=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if len(result.stdout) > 256:
        raise ValueError()
    return result.stdout


def _identity():
    name, driver = _query_smi('name,driver_version').strip().split(',')
    return {'gpu_name': name.strip(), 'driver_version': driver.strip()}


def probe_device(*, nvml=_nvml, smi=_smi, status=runtime_status, identity=_identity):
    total = free = None
    source = None
    for name, fn in (('nvml', nvml), ('nvidia-smi', smi)):
        try:
            total, free = fn()
            if not isinstance(total, int) or not isinstance(free, int) or not 0 <= free <= total or total <= 0:
                raise ValueError()
            source = name
            break
        except Exception:
            total = free = None
    runtime = status()
    details = {}
    if total and runtime.get('cuda_available'):
        try:
            details = identity()
        except Exception:
            pass
    # Provider registration and hardware are insufficient proof of a model session.
    reason = runtime['reason'] or ('tagger:cuda_not_verified' if total else 'tagger:cuda_unavailable')
    return {**details, 'provider': 'cpu', 'total_bytes': total, 'free_bytes': free, 'source': source,
            'runtime_version': runtime['version'], 'cuda_available': runtime.get('cuda_available', False),
            'cuda_libraries': runtime.get('cuda_libraries', {}),
            'reserved_bytes': max(1024**3, total//5) if total else None,
            'cuda_verified': False, 'reason': reason}


def plan_batch(device, manifest, requested, *, profile=None):
    if isinstance(requested, bool) or not isinstance(requested, int) or not 1 <= requested <= 64:
        raise ValueError('Invalid batch size')
    if device.get('provider') != 'cuda' or manifest.tensor_schema.batch != 'dynamic':
        return 1
    profile = profile or matching_profile(device, manifest.digest, device.get('runtime_version'))
    free, reserved = device.get('free_bytes'), device.get('reserved_bytes')
    if profile is None or free is None or reserved is None:
        return 1
    budget = max(0, free-reserved)
    return max((int(n) for n, size in profile['required_bytes'].items()
                if 1 <= int(n) <= min(4, requested) and size <= budget), default=1)
