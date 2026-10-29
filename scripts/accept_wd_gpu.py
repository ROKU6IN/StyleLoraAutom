"""Explicit offline CUDA measurement; reports never automatically become trusted profiles."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stylelora.tagging.device import probe_device
from stylelora.tagging.gpu_profiles import CUDA_POLICY
from stylelora.tagging.manifests import REVIEWED_MANIFESTS
from stylelora.tagging.models import ModelStore
from stylelora.tagging.model_runtime import create_session, runtime_status
from stylelora.tagging.predictor import preprocess, decode_scores, TaggerConfig
from tests.wd_cpu_reference import samples, compare_scores, expected_tags


def run(args, report):
    import numpy as np
    import pynvml
    manifest = REVIEWED_MANIFESTS[0]
    config = TaggerConfig(manifest_id=manifest.digest, general_threshold=.35, character_threshold=.85,
                          provider='auto', batch_size=4)
    contents = samples(args.reference_dir/'reference-power.jpg')
    golden_path = Path(__file__).resolve().parents[1]/'tests/fixtures/wd-v3/reference-scores.npz'
    metadata = json.loads(golden_path.with_suffix('.json').read_text(encoding='utf-8'))
    assert metadata['manifest_id'] == manifest.digest
    assert metadata['scores_sha256'] == hashlib.sha256(golden_path.read_bytes()).hexdigest()
    assert metadata['cases'] == {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    store = ModelStore(args.model_dir)
    store.verify(manifest, threading.Event())
    path = store.directory(manifest)/manifest.model_file.name
    with (path.parent/manifest.csv_file.name).open(encoding='utf-8-sig', newline='') as stream:
        labels = tuple((r['name'], int(r['category'])) for r in csv.DictReader(stream))
    device = probe_device()
    assert device['cuda_available'] and device['free_bytes'] > device['reserved_bytes']
    report.update(manifest_id=manifest.digest, device=device, runtime=runtime_status(), policy=CUDA_POLICY,
                  scope='CUDA implementation consistency and measured VRAM; not dataset accuracy', batches=[])
    values = {name: preprocess(content, manifest) for name, content in contents.items()}
    pynvml.nvmlInit()
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        memory = lambda: int(pynvml.nvmlDeviceGetMemoryInfo(handle).used)
        with np.load(golden_path, allow_pickle=False) as golden:
            for batch_size in (1, 2, 4):
                baseline, peak, stopped = memory(), [memory()], threading.Event()
                sampling_errors = []
                def watch():
                    try:
                        while not stopped.wait(.005):
                            peak[0] = max(peak[0], memory())
                    except Exception:
                        sampling_errors.append(True)
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
                session = None
                try:
                    started = time.perf_counter()
                    session = create_session(manifest, path, 'cuda', profile_prefix=args.output/f'ort-batch-{batch_size}')
                    loaded = time.perf_counter()-started
                    timings, errors = [], []
                    keys = list(values)
                    for repeat in range(3):
                        for offset in range(0, len(keys), batch_size):
                            names = keys[offset:offset+batch_size]
                            tensor = np.concatenate([values[name] for name in names])
                            started = time.perf_counter()
                            output = session.run([manifest.tensor_schema.output_name], {manifest.tensor_schema.input_name: tensor})[0]
                            elapsed = time.perf_counter()-started
                            timings.append({'repeat': repeat, 'count': len(names), 'seconds': elapsed})
                            for i, name in enumerate(names):
                                errors.append(compare_scores(output[i:i+1], golden[name]))
                                tags, _, _ = decode_scores(output[i:i+1], labels, config)
                                assert [t.tag for t in tags] == list(expected_tags(golden[name], labels, .35, .85))
                    profile = Path(session.end_profiling())
                    events = json.loads(profile.read_text(encoding='utf-8'))
                    nodes = [e.get('args', {}).get('provider') for e in events if e.get('cat') == 'Node']
                    cuda_nodes = nodes.count('CUDAExecutionProvider')
                    assert cuda_nodes > 0, 'No actual CUDA node execution recorded'
                    peak[0] = max(peak[0], memory())
                    report['batches'].append({'size': batch_size, 'load_seconds': loaded, 'calls': timings,
                        'warm_seconds_per_image': statistics.median([t['seconds']/t['count'] for t in timings if t['repeat'] > 0]),
                        'max_abs_error': max(errors), 'baseline_used_bytes': baseline, 'peak_used_bytes': peak[0],
                        'observed_session_peak_bytes': max(0, peak[0]-baseline),
                        'cuda_node_events': cuda_nodes, 'cpu_node_events': nodes.count('CPUExecutionProvider')})
                    print(json.dumps(report['batches'][-1]), flush=True)
                finally:
                    stopped.set()
                    watcher.join()
                    session = None
                    if sampling_errors:
                        raise RuntimeError('NVML sampling failed; memory profile is invalid')
    finally:
        pynvml.nvmlShutdown()
    if args.http_loop:
        from scripts.accept_wd_cpu import http_loop
        with np.load(golden_path, allow_pickle=False) as golden:
            expected = {name: expected_tags(golden[name], labels, .35, .85) for name in contents}
        report['http'] = http_loop(args.output, args.model_dir, manifest, config, contents, expected,
                                   expected_provider='cuda', expected_batch=4)
    report['passed'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True, type=Path)
    parser.add_argument('--reference-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--http-loop', action='store_true', help='Also verify product jobs against a reviewed hardware profile')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'passed': False}
    try:
        run(args, report)
    except Exception as error:
        report['error'] = type(error).__name__+': '+str(error)
        raise
    finally:
        (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
