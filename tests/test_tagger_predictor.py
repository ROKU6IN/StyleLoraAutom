from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image

from stylelora.runtime.errors import ServiceError
from stylelora.tagging.model_runtime import runtime_status, create_session
from stylelora.tagging.models import ModelStore
from stylelora.tagging.predictor import TaggerConfig, OrtTagger, preprocess, decode_scores, cache_parameters
from .support import TEMP_ROOT
from .tagger_support import fixture, inference_fixture, MemoryDownload, DEVICE


def picture(color='blue', size=(2, 2), *, exif=None):
    image = Image.new('RGBA', size, color)
    stream = BytesIO()
    image.save(stream, format='PNG', **({'exif': exif} if exif else {}))
    return stream.getvalue()


@unittest.skipUnless(runtime_status()['available'], 'optional wd-cpu runtime not installed')
class PredictorTest(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.manifest, content = inference_fixture()
        self.cancel = threading.Event()
        self.store = ModelStore(Path(self.temp.name), download=MemoryDownload(content))
        self.store.install(self.manifest, self.cancel)
        self.config = TaggerConfig(manifest_id=self.manifest.digest, general_threshold=.5, character_threshold=.7, batch_size=4)

    def engine(self, **kwargs):
        engine = OrtTagger(self.store, self.manifest, self.config, self.cancel,
                           device_probe=lambda: DEVICE, **kwargs)
        self.addCleanup(engine.close)
        return engine

    def test_manifest_preprocess_white_alpha_padding_order_layout_and_normalization(self):
        import numpy as np
        manifest, _ = fixture()
        value = preprocess(picture((10, 20, 30, 255), (1, 2)), manifest)
        self.assertEqual(value.shape, (1, 2, 2, 3))
        self.assertEqual(value[0, 0, 0].tolist(), [30., 20., 10.])
        self.assertEqual(value[0, 0, 1].tolist(), [255., 255., 255.])
        alpha = preprocess(picture((10, 20, 30, 0)), self.manifest)
        self.assertTrue(np.array_equal(alpha, np.ones((1, 2, 2, 3))))
        other, _ = fixture(change=lambda v: v['tensor_schema'].update(layout='NCHW', color_order='RGB', normalization='minus1_1'))
        normalized = preprocess(picture('black'), other)
        self.assertEqual(normalized.shape, (1, 3, 2, 2))
        self.assertTrue(np.all(normalized == -1))
        self.assertEqual(normalized.dtype, np.float32)
        self.assertTrue(normalized.flags.c_contiguous)

    def test_exif_orientation_and_decode_failure(self):
        original = Image.new('RGB', (2, 1), 'red')
        original.putpixel((1, 0), (0, 0, 255))
        exif = original.getexif()
        exif[274] = 6
        stream = BytesIO()
        original.save(stream, format='PNG', exif=exif)
        value = preprocess(stream.getvalue(), self.manifest)
        self.assertEqual(value[0, 0, 0].tolist(), [0., 0., 1.])
        self.assertEqual(value[0, 1, 0].tolist(), [1., 0., 0.])
        with self.assertRaises(ServiceError) as caught:
            preprocess(b'bad', self.manifest)
        self.assertEqual(caught.exception.code, 'image:decode_failed')
        with self.assertRaises(ServiceError) as caught:
            preprocess(picture(size=(10_001, 1)), self.manifest)
        self.assertEqual(caught.exception.code, 'image:too_large')

    def test_threshold_boundaries_canonical_dedup_conf_max_and_separate_ratings(self):
        import numpy as np
        tags, evidence, ratings = decode_scores(np.array([[.5, .8, .99]], dtype='float32'),
            [(' BLUE_HAIR ', 0), ('blue hair', 4), ('explicit', 9)], self.config)
        self.assertEqual([t.tag for t in tags], ['blue hair'])
        self.assertAlmostEqual(tags[0].conf, .8)
        self.assertEqual([e['category'] for e in evidence], [0, 4])
        self.assertEqual(ratings[0]['name'], 'explicit')
        for value in [np.array([[float('nan'), 0., 0.]]), np.array([[1.1, 0., 0.]]), np.zeros((3,)), np.ones((1, 3), dtype='int32')]:
            with self.assertRaises(ServiceError):
                decode_scores(value, [('a', 0), ('b', 4), ('c', 9)], self.config)

    def test_real_inference_is_ordered_and_fixed_batch_one(self):
        engine = self.engine()
        blue = engine.predict_one(picture('blue'), self.cancel)
        green = engine.predict_one(picture('lime'), self.cancel)
        red = engine.predict_one(picture('red'), self.cancel)
        self.assertEqual([(t.tag, t.conf) for t in blue.tags], [('blue hair', 1.)])
        self.assertEqual([(t.tag, t.conf) for t in green.tags], [('fixture character', 1.)])
        self.assertEqual(red.tags, ())
        self.assertEqual(red.params['rating_scores'], [{'name': 'general', 'confidence': 1.}])
        self.assertEqual(blue.params['actual']['batch_size'], 1)
        self.assertEqual(blue.params['actual']['provider'], 'cpu')

    def test_cancel_after_blocking_run_discards_response(self):
        engine = self.engine()
        original = engine.session
        cancel = self.cancel
        class Session:
            def run(self, *args):
                result = original.run(*args)
                cancel.set()
                return result
        engine.session = Session()
        with self.assertRaises(ServiceError) as caught:
            engine.predict_one(picture(), cancel)
        self.assertEqual(caught.exception.code, 'operation:canceled')

    def test_model_mutation_never_reuses_loaded_session(self):
        engine = self.engine()
        (engine.path.parent/'selected_tags.csv').write_bytes(b'changed')
        with self.assertRaises(ServiceError) as caught:
            engine.predict_one(picture(), self.cancel)
        self.assertEqual(caught.exception.code, 'tagger:model_changed')

    def test_simulated_cuda_oom_retries_same_model_on_cpu_once(self):
        calls = []
        class Gpu:
            def run(self, *args):
                calls.append('gpu.run')
                raise RuntimeError('CUDA out of memory')
        def factory(manifest, path, provider):
            calls.append(provider)
            return Gpu() if provider == 'cuda' else create_session(manifest, path, provider)
        device = {**DEVICE, 'total_bytes': 10*1024**3, 'free_bytes': 8*1024**3, 'reserved_bytes': 2*1024**3}
        config = self.config.model_copy(update={'provider': 'auto'})
        engine = OrtTagger(self.store, self.manifest, config, self.cancel, session_factory=factory, device_probe=lambda: device)
        self.addCleanup(engine.close)
        result = engine.predict_one(picture(), self.cancel)
        self.assertEqual(calls, ['cuda', 'gpu.run', 'cpu'])
        self.assertEqual(result.params['actual']['provider'], 'cpu')
        self.assertEqual(result.params['fallback_reason'], 'tagger:oom')
        self.assertEqual(result.actual_model, self.manifest.model_id+'@'+self.manifest.revision)

    def test_cuda_init_failure_falls_back_and_cpu_failure_does_not_loop(self):
        calls = []
        class Cpu:
            def run(self, *args):
                calls.append('cpu.run')
                raise MemoryError()
        def factory(manifest, path, provider):
            calls.append(provider)
            if provider == 'cuda':
                raise RuntimeError('cannot initialize')
            return Cpu()
        device = {**DEVICE, 'total_bytes': 10*1024**3, 'free_bytes': 8*1024**3, 'reserved_bytes': 2*1024**3}
        config = self.config.model_copy(update={'provider': 'auto'})
        engine = OrtTagger(self.store, self.manifest, config, self.cancel, session_factory=factory, device_probe=lambda: device)
        self.addCleanup(engine.close)
        with self.assertRaises(ServiceError) as caught:
            engine.predict_one(picture(), self.cancel)
        self.assertEqual(caught.exception.code, 'tagger:oom')
        self.assertEqual(calls, ['cuda', 'cpu', 'cpu.run'])

    def test_cache_parameters_exclude_batch_and_telemetry_but_include_provider(self):
        one = cache_parameters(self.manifest, self.config, 'test', 'cpu')
        two = cache_parameters(self.manifest, self.config.model_copy(update={'batch_size': 1}), 'test', 'cpu')
        self.assertEqual(one, two)
        self.assertNotEqual(one, cache_parameters(self.manifest, self.config, 'test', 'cuda'))
        self.assertNotIn('free_bytes', one)
        self.assertNotIn('batch_size', one)
