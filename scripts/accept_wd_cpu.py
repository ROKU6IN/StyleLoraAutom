"""Explicit, offline real-WD CPU acceptance. Never downloads or uploads data.

Run from the repository root with PYTHONPATH=src. Supply an installed model
directory and the pinned publisher reference inputs (see docs/18). Outputs go
into a new directory and are retained for review; normal unit tests stay offline.
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import __version__ as pillow_version
from fastapi.testclient import TestClient
from stylelora.api.app import create_app
from stylelora.project_db.connection import read_snapshot
from stylelora.project_db.repositories import annotations
from stylelora.runtime.lifecycle import Runtime
from stylelora.tagging.manifests import REVIEWED_MANIFESTS, reviewed_provenance
from stylelora.tagging.model_runtime import runtime_status
from stylelora.tagging.models import ModelStore
from stylelora.tagging.predictor import OrtTagger, TaggerConfig, preprocess
from tests.wd_cpu_reference import (REFERENCE_REVISION, REFERENCE_SHA256, IMAGE_SHA256,
    load_reference, samples, compare_scores, expected_tags)


def peak_memory():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                  'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                  'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]
        value = Counters()
        value.cb = ctypes.sizeof(value)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not query(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
            raise OSError('Unable to read process memory')
        return value.PeakWorkingSetSize
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform == 'darwin' else 1024)


def cpu_name():
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
            return winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
    return platform.processor()


def http_loop(output, model_dir, manifest, config, contents, expected, *, expected_provider='cpu', expected_batch=1):
    """Real API handlers/worker/DAL with ASGI transport; no production service."""
    inputs = output/'inputs'
    inputs.mkdir()
    for name, content in contents.items():
        (inputs/name).write_bytes(content)
    root = output/'acceptance.slproj'
    runtime = Runtime(model_dir=model_dir, product_dir=output/'product')
    def call(client, method, path, body=None, version=None, expected_status=200):
        response = client.request(method, path, json=body,
            headers={**({'Idempotency-Key': str(uuid4())} if method == 'POST' else {}),
                     **({'If-Match': '"'+version+'"'} if version else {})})
        if response.status_code != expected_status:
            raise AssertionError(f'{method} {path}: {response.status_code} {response.text[:500]}')
        return response.json()
    def wait(client, url, timeout=300):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            value = call(client, 'GET', url)
            if value['status'] == 'done':
                return value
            if value['status'] in ('failed', 'canceled', 'paused', 'interrupted'):
                raise AssertionError('Acceptance task stopped: '+str(value.get('error')))
            time.sleep(.05)
        raise AssertionError('Acceptance task timed out')
    with TestClient(create_app(runtime), base_url='http://127.0.0.1:8000',
            headers={'Authorization': 'Bearer '+runtime.token}) as client:
        grant = runtime.grant(root, 'project_create')
        op = call(client, 'POST', '/api/projects', {'grant_id': grant['grant_id'], 'name': 'Real WD acceptance',
            'trigger_word': 'wd_acceptance_style'}, expected_status=202)
        pid = wait(client, op['status_url'])['result']['project_id']
        p = '/api/projects/'+pid
        grant = runtime.grant(inputs, 'import_root', pid)
        op = call(client, 'POST', p+'/imports/preview', {'root_handle': grant['grant_id']}, expected_status=202)
        preview = wait(client, op['status_url'])['result']
        op = call(client, 'POST', p+'/imports', {'preview_id': preview['preview_id'], 'digest': preview['digest']}, expected_status=202)
        wait(client, op['status_url'])
        rows = call(client, 'GET', p+'/images?limit=100')['items']
        # Exact input SHA binds expected tags even when import renamed files.
        by_sha = {hashlib.sha256(content).hexdigest(): expected[name] for name, content in contents.items()}
        def start(mode):
            return call(client, 'POST', p+'/jobs/batches', {'batch_id': str(uuid4()), 'executors': ['wd'],
                'mode': mode, 'scope': {'filter': {}}, 'config': config.model_dump()}, expected_status=202)
        first = start('uncovered')
        complete = wait(client, first['status_url'])
        assert complete['done_count'] == len(rows) and complete['failed_count'] == 0
        assert call(client, 'GET', '/api/tagger/capabilities')['available']
        versions, actual_batches = {}, []
        with read_snapshot(runtime.context.handle) as db:
            for row in rows:
                value = annotations.get_current(db, row['image_id'], 'wd', 'tags')
                assert [tag.tag for tag in value.content] == list(by_sha[row['sha256']])
                assert value.params_json['actual']['provider'] == expected_provider
                assert value.params_json['actual']['manifest_hash'] == manifest.digest
                versions[row['image_id']] = value.annotation_id
                actual_batches.append(value.params_json['actual']['batch_size'])
        assert max(actual_batches) == expected_batch
        cached = wait(client, start('uncovered')['status_url'])
        assert cached['done_count'] == 0 and cached['skipped_count'] == len(rows)
        with read_snapshot(runtime.context.handle) as db:
            assert versions == {row['image_id']: annotations.get_current(db, row['image_id'], 'wd', 'tags').annotation_id for row in rows}
        # Observe real inference entry without fake scores or delays.
        entered, observed_calls = threading.Event(), []
        class Observed(OrtTagger):
            def predict_many(self, contents, cancel):
                observed_calls.extend(hashlib.sha256(content).hexdigest() for content in contents)
                entered.set()
                return super().predict_many(contents, cancel)
        runtime.jobs.engine_factory = Observed
        def idle(job_id):
            deadline = time.monotonic()+300
            while time.monotonic() < deadline:
                with runtime.jobs.lock:
                    if (pid, job_id) not in runtime.jobs.active:
                        return
                time.sleep(.05)
            raise AssertionError('Real inference worker did not finish')
        def control(accepted, action):
            row = call(client, 'GET', accepted['status_url'])
            return call(client, 'POST', accepted['status_url']+'/control', {'action': action}, row['version'])
        paused = start('all')
        assert entered.wait(300)
        control(paused, 'pause')
        idle(paused['task']['id'])
        paused_row = call(client, 'GET', paused['status_url'])
        assert paused_row['status'] == 'paused' and paused_row['done_count'] == expected_batch
        control(paused, 'resume')
        resumed = wait(client, paused['status_url'])
        assert resumed['done_count'] == len(rows) and len(observed_calls) == len(rows)
        entered.clear()
        with read_snapshot(runtime.context.handle) as db:
            before_cancel = {r['image_id']: annotations.get_current(db, r['image_id'], 'wd', 'tags').annotation_id for r in rows}
        canceled = start('all')
        assert entered.wait(300)
        control(canceled, 'cancel')
        idle(canceled['task']['id'])
        canceled_row = call(client, 'GET', canceled['status_url'])
        assert canceled_row['status'] == 'canceled' and canceled_row['done_count'] == 0
        with read_snapshot(runtime.context.handle) as db:
            assert before_cancel == {r['image_id']: annotations.get_current(db, r['image_id'], 'wd', 'tags').annotation_id for r in rows}
        # Exercise human editing after real inference, then the real export path.
        image_id = rows[0]['image_id']
        value = call(client, 'GET', p+f'/annotations/{image_id}')
        wanted = [*by_sha[rows[0]['sha256']], 'acceptance manual']
        call(client, 'PUT', p+f'/annotations/{image_id}/manual',
            {'tags': [{'tag': name} for name in wanted], 'read_set': value['refs']}, value['version'])
        rows = call(client, 'GET', p+'/images?limit=100')['items']
        call(client, 'POST', p+'/images/review', {'ids': [r['image_id'] for r in rows], 'target': 'keep',
            'expected_versions': {str(r['image_id']): r['version'] for r in rows}})
        op = call(client, 'POST', p+'/exports/preview', {'options': {}}, expected_status=202)
        preview = wait(client, op['status_url'])['result']
        op = call(client, 'POST', p+'/exports', {'preview_id': preview['preview_id'], 'digest': preview['digest']}, expected_status=202)
        wait(client, '/api/operations/'+op['operation_id'])
        exported = call(client, 'GET', op['status_url'])
        report = call(client, 'GET', exported['report_url'])
        texts = list((root/report['directory']).glob('*.txt'))
        assert len(texts) == len(rows)
        assert any('acceptance manual' in path.read_text(encoding='utf-8') for path in texts)
        current = call(client, 'GET', p)
        close = call(client, 'POST', p+'/close', {}, current['version'], expected_status=202)
        wait(client, close['status_url'])
        grant = runtime.grant(root, 'project_open')
        opened = call(client, 'POST', '/api/projects/open', {'grant_id': grant['grant_id']}, expected_status=202)
        pid = wait(client, opened['status_url'])['result']['project_id']
        record = call(client, 'GET', f'/api/projects/{pid}/jobs/{complete["job_id"]}')
        assert record['done_count'] == len(rows) and record['status'] == 'done'
        return {'transport': 'ASGI TestClient', 'imported': len(rows), 'written': complete['done_count'],
                'cache_hits': cached['skipped_count'], 'exported_captions': len(texts), 'reopened_record': True,
                'paused_inflight_committed': paused_row['done_count'], 'resumed_without_repeating': True,
                'canceled_late_result_discarded': True,
                'actual_provider': expected_provider, 'actual_batches': actual_batches,
                'project': 'acceptance.slproj'}


def run(args, report):
    import numpy as np
    import onnxruntime as ort
    manifest = REVIEWED_MANIFESTS[0]
    reference = load_reference(args.reference_dir/'reference-app.py')
    contents = samples(args.reference_dir/'reference-power.jpg')
    report.update(manifest_id=manifest.digest, model=manifest.model_id, revision=manifest.revision,
        provenance=reviewed_provenance(manifest), runtime=runtime_status(), pillow=pillow_version,
        python=platform.python_version(), cpu=cpu_name(), logical_cpus=os.cpu_count(),
        reference={'revision': REFERENCE_REVISION, 'code_sha256': REFERENCE_SHA256, 'image_sha256': IMAGE_SHA256},
        tolerances={'atol': 1e-5, 'rtol': 1e-4}, cases=[])
    store = ModelStore(args.model_dir)
    config = TaggerConfig(manifest_id=manifest.digest, general_threshold=.35, character_threshold=.85, provider='cpu')
    started = time.perf_counter()
    engine = OrtTagger(store, manifest, config, threading.Event())
    report['verified_engine_load_seconds'] = time.perf_counter()-started
    labels = engine.labels
    class RecordingSession:
        def __init__(self, native):
            self.native, self.last = native, None
        def run(self, *values):
            self.last = self.native.run(*values)
            return self.last
    recorder = RecordingSession(engine.session)
    engine.session = recorder
    product_scores, tags, timings = {}, {}, []
    try:
        for name, content in contents.items():
            if not np.array_equal(preprocess(content, manifest), reference(content, 448)):
                raise AssertionError('Reference preprocessing mismatch: '+name)
            start = time.perf_counter()
            result = engine.predict_one(content, threading.Event())
            elapsed = time.perf_counter()-start
            timings.append(elapsed)
            product_scores[name] = recorder.last[0].copy()
            tags[name] = {tag.tag: tag.conf for tag in result.tags}
            report['cases'].append({'name': name, 'sha256': hashlib.sha256(content).hexdigest(),
                'seconds': elapsed, 'tag_count': len(result.tags), 'provider': result.params['actual']['provider']})
            print(f'{name}: {elapsed:.3f}s, {len(result.tags)} tags', flush=True)
    finally:
        engine.close()
        del recorder
        gc.collect()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    # Publisher-style CPU execution with ORT's default optimization, independent
    # of the product's deliberately conservative ORT_DISABLE_ALL session.
    session = ort.InferenceSession((store.directory(manifest)/manifest.model_file.name).read_bytes(),
        sess_options=options, providers=['CPUExecutionProvider'])
    reference_scores = {}
    if args.golden:
        metadata = json.loads(args.golden.with_suffix('.json').read_text(encoding='utf-8'))
        if (metadata['manifest_id'] != manifest.digest or metadata['reference'] != report['reference']
                or metadata['cases'] != {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
                or hashlib.sha256(args.golden.read_bytes()).hexdigest() != metadata['scores_sha256']):
            raise AssertionError('Frozen golden provenance changed')
    golden = np.load(args.golden, allow_pickle=False) if args.golden else None
    try:
        if golden is not None and set(golden.files) != set(contents):
            raise AssertionError('Golden sample set changed')
        for case in report['cases']:
            name = case['name']
            scores = session.run([session.get_outputs()[0].name], {session.get_inputs()[0].name: reference(contents[name], 448)})[0]
            reference_scores[name] = scores
            case['max_abs_error'] = compare_scores(product_scores[name], scores)
            if golden is not None:
                case['golden_max_abs_error'] = compare_scores(scores, golden[name])
            expected = expected_tags(scores, labels, .35, .85)
            if list(expected) != list(tags[name]):
                raise AssertionError('Threshold/tag mapping mismatch: '+name)
            case['top_tags'] = sorted(tags[name], key=tags[name].get, reverse=True)[:12]
    finally:
        if golden is not None:
            golden.close()
        del session
        gc.collect()
    np.savez_compressed(args.output/'reference-scores.npz', **reference_scores)
    report['inference_seconds'] = {'first': timings[0], 'warm_median': float(np.median(timings[1:])),
        'p95': float(np.percentile(timings, 95)), 'samples': len(timings)}
    report['http'] = http_loop(args.output, args.model_dir, manifest, config, contents, tags)
    report['peak_process_working_set_bytes'] = peak_memory()
    report['passed'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--reference-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory; existing output is never overwritten')
    parser.add_argument('--golden', type=Path, help='Previously reviewed frozen reference-scores.npz')
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'passed': False, 'scope': 'implementation consistency, not dataset accuracy'}
    try:
        run(args, report)
    except Exception as error:
        report['error'] = type(error).__name__+': '+str(error)
        raise
    finally:
        (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print('CPU acceptance passed; report:', args.output/'report.json', flush=True)


if __name__ == '__main__':
    main()
