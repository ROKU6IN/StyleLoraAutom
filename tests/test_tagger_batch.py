from pathlib import Path
import tempfile
import threading
import unittest

from stylelora.runtime.errors import ServiceError
from stylelora.tagging.device import plan_batch
from stylelora.tagging.gpu_profiles import matching_profile
from stylelora.tagging.model_runtime import runtime_status, create_session
from stylelora.tagging.models import ModelStore
from stylelora.tagging.predictor import OrtTagger, TaggerConfig
from .support import TEMP_ROOT
from .tagger_support import dynamic_fixture, fixture, MemoryDownload, GPU_DEVICE, gpu_profile
from .test_tagger_predictor import picture


class ProfileTest(unittest.TestCase):
    def test_shipped_profile_is_backed_by_real_cuda_evidence(self):
        import json
        from stylelora.tagging.gpu_profiles import REVIEWED_PROFILES
        evidence = json.loads((Path(__file__).parent/'fixtures/wd-v3/cuda-rtx5060ti.json').read_text(encoding='utf-8'))
        profile = matching_profile(evidence['device'], evidence['manifest_id'], evidence['runtime']['version'])
        self.assertIsNotNone(profile)
        self.assertIn(profile, REVIEWED_PROFILES)
        self.assertTrue(evidence['passed'])
        for row in evidence['batches']:
            self.assertGreater(row['cuda_node_events'], 0)
            self.assertLess(row['max_abs_error'], 1e-5)
            self.assertGreaterEqual(profile['required_bytes'][str(row['size'])], row['observed_session_peak_bytes']*1.25+256*1024**2)
        changed = {**evidence['device'], 'cuda_libraries': {}}
        self.assertIsNone(matching_profile(changed, evidence['manifest_id'], evidence['runtime']['version']))

    def test_profile_requires_exact_model_runtime_device_driver_and_policy(self):
        manifest, _ = dynamic_fixture()
        profile = gpu_profile(manifest)
        match = lambda d=GPU_DEVICE, mid=manifest.digest, version='test', p=profile: matching_profile(d, mid, version, profiles=(p,))
        self.assertEqual(match(), profile)
        for key in ('gpu_name', 'driver_version', 'total_bytes'):
            self.assertIsNone(match(d={**GPU_DEVICE, key: None}))
        self.assertIsNone(match(mid='0'*64))
        self.assertIsNone(match(version='changed'))
        self.assertIsNone(match(p={**profile, 'policy': 'old'}))

    def test_memory_reserve_unknown_profile_fixed_model_and_cpu_force_one(self):
        manifest, _ = dynamic_fixture()
        profile = gpu_profile(manifest)
        self.assertEqual(plan_batch(GPU_DEVICE, manifest, 4, profile=profile), 4)
        self.assertEqual(plan_batch({**GPU_DEVICE, 'free_bytes': 6*1024**3}, manifest, 4, profile=profile), 2)
        self.assertEqual(plan_batch({**GPU_DEVICE, 'free_bytes': None}, manifest, 4, profile=profile), 1)
        self.assertEqual(plan_batch(GPU_DEVICE, manifest, 4), 1)
        self.assertEqual(plan_batch({**GPU_DEVICE, 'provider': 'cpu'}, manifest, 4, profile=profile), 1)
        self.assertEqual(plan_batch(GPU_DEVICE, fixture()[0], 4, profile=profile), 1)


@unittest.skipUnless(runtime_status()['available'], 'optional runtime not installed')
class BatchTest(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temp.cleanup)
        self.manifest, files = dynamic_fixture()
        self.cancel = threading.Event()
        self.store = ModelStore(Path(temp.name), download=MemoryDownload(files))
        self.store.install(self.manifest, self.cancel)
        self.config = TaggerConfig(manifest_id=self.manifest.digest, general_threshold=.5,
                                  character_threshold=.5, provider='auto', batch_size=4)
        self.calls = []

    def engine(self, callback=None, *, device=None, profiled=True):
        calls = self.calls
        def factory(manifest, path, provider):
            real = create_session(manifest, path, 'cpu')
            class Session:
                def run(inner, names, inputs):
                    n = next(iter(inputs.values())).shape[0]
                    calls.append((provider, n))
                    if callback:
                        result = callback(provider, n)
                        if result is not None:
                            return [result]
                    return real.run(names, inputs)
            return Session()
        engine = OrtTagger(self.store, self.manifest, self.config, self.cancel, session_factory=factory,
            device_probe=device or (lambda: GPU_DEVICE), profile=gpu_profile(self.manifest) if profiled else None)
        self.addCleanup(engine.close)
        return engine

    def test_dynamic_batch_uses_real_tensor_order_and_records_actual_size(self):
        result = self.engine().predict_many([picture('blue'), picture('lime'), picture('red'), picture('blue')], self.cancel)
        self.assertEqual(self.calls, [('cuda', 4)])
        self.assertEqual([[t.tag for t in r.tags] for r in result], [['blue hair'], ['fixture character'], [], ['blue hair']])
        self.assertEqual([r.params['actual']['batch_size'] for r in result], [4]*4)

    def test_unknown_profile_stays_one_on_cuda(self):
        self.engine(profiled=False).predict_many([picture(), picture()], self.cancel)
        self.assertEqual(self.calls, [('cuda', 1)]*2)

    def test_oom_halves_four_to_two_to_one_then_cpu_once_per_image(self):
        def fail(provider, size):
            if provider == 'cuda':
                raise MemoryError()
        result = self.engine(fail).predict_many([picture()]*4, self.cancel)
        self.assertEqual(self.calls, [('cuda', 4), ('cuda', 2), ('cuda', 1)]+[('cpu', 1)]*4)
        self.assertTrue(all(r.params['actual']['provider'] == 'cpu' and r.params['actual']['batch_size'] == 1 for r in result))

    def test_oom_smaller_cuda_batch_keeps_order_and_does_not_retry_success(self):
        def fail(provider, size):
            if size > 2:
                raise RuntimeError('CUDA out of memory')
        results = self.engine(fail).predict_many([picture()]*4, self.cancel)
        self.assertEqual(self.calls, [('cuda', 4), ('cuda', 2), ('cuda', 2)])
        self.assertTrue(all(r.params['actual']['batch_size'] == 2 for r in results))

    def test_cpu_oom_is_per_image_failure_without_retry_loop(self):
        def fail(provider, size):
            raise MemoryError()
        results = self.engine(fail).predict_many([picture()]*2, self.cancel)
        self.assertEqual(self.calls, [('cuda', 2), ('cuda', 1), ('cpu', 1), ('cpu', 1)])
        self.assertTrue(all(isinstance(r, ServiceError) and r.code == 'tagger:oom' for r in results))

    def test_decode_and_output_errors_are_isolated_to_their_image(self):
        import numpy as np
        def output(provider, size):
            return np.array([[1., 0., 0.], [float('nan'), 0., 0.]], dtype='float32')
        results = self.engine(output).predict_many([picture(), b'broken', picture()], self.cancel)
        self.assertEqual(self.calls, [('cuda', 2)])
        self.assertEqual(results[0].tags[0].tag, 'blue hair')
        self.assertEqual([r.code for r in results[1:]], ['image:decode_failed', 'tagger:invalid_output'])

    def test_cancel_after_batch_discards_all_results(self):
        def cancel(provider, size):
            self.cancel.set()
        with self.assertRaises(ServiceError) as caught:
            self.engine(cancel).predict_many([picture()]*4, self.cancel)
        self.assertEqual(caught.exception.code, 'operation:canceled')

    def test_lost_memory_telemetry_falls_back_before_call(self):
        samples = iter([GPU_DEVICE, {**GPU_DEVICE, 'free_bytes': None}])
        results = self.engine(device=lambda: next(samples)).predict_many([picture()]*2, self.cancel)
        self.assertEqual(self.calls, [('cpu', 1)]*2)
        self.assertTrue(all(r.params['fallback_reason'] == 'tagger:low_memory' for r in results))

    def test_profile_minimum_must_fit_above_reserved_memory(self):
        device = {**GPU_DEVICE, 'free_bytes': GPU_DEVICE['reserved_bytes']+512*1024**2}
        results = self.engine(device=lambda: device).predict_many([picture()]*2, self.cancel)
        self.assertEqual(self.calls, [('cpu', 1)]*2)
        self.assertTrue(all(r.params['fallback_reason'] == 'tagger:low_memory' for r in results))
