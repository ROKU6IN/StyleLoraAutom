"""Reviewed measurements only; no HTTP or startup profiling/import mechanism.

Budgets are conservative full-session peaks (resident allocations are not added
back to free VRAM). Exact model, runtime, device, driver and session policy must
match. An unknown combination always retains batch one.
"""
CUDA_POLICY = 'fp32-no-tf32-arena-same-v1'
REVIEWED_PROFILES = ({
    'profile_id': 'eva02-rtx5060ti-61088-ort131-fp32-v1',
    'manifest_id': '90f63fb27ed54b7abc1c72c9b2009e0fe5248e7bf15293c195d7f5ed117d35d1',
    'runtime_version': '1.31.0', 'policy': CUDA_POLICY,
    'gpu_name': 'NVIDIA GeForce RTX 5060 Ti', 'driver_version': '610.88',
    'total_bytes': 17103323136,
    'cuda_libraries': {'nvidia-cuda-runtime': '13.4.92', 'nvidia-cudnn-cu13': '9.27.0.42',
                       'nvidia-cublas': '13.8.0.4'},
    # Observed 1.88/2.04/2.68 GiB plus 25% and 256 MiB, rounded up to GiB.
    # Full-session amounts, intentionally conservative when the session is loaded.
    'required_bytes': {'1': 3*1024**3, '2': 3*1024**3, '4': 4*1024**3},
    'reviewed_at': '2026-10-25',
},)


def matching_profile(device, manifest_id, runtime_version, *, profiles=None):
    for profile in REVIEWED_PROFILES if profiles is None else profiles:
        if (profile['manifest_id'] == manifest_id and profile['runtime_version'] == runtime_version
                and profile['policy'] == CUDA_POLICY
                and profile['gpu_name'] == device.get('gpu_name')
                and profile['driver_version'] == device.get('driver_version')
                and profile.get('cuda_libraries') == device.get('cuda_libraries')
                and profile['total_bytes'] == device.get('total_bytes')):
            return profile
    return None
