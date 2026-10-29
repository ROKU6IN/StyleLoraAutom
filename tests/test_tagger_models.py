import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from stylelora.runtime.errors import ServiceError
from stylelora.tagging.manifests import REVIEWED_MANIFESTS, parse_manifest
from stylelora.tagging.model_runtime import runtime_status, validate_tensors
from stylelora.tagging.models import HuggingFaceDownload, ModelStore, allowed_url
from stylelora.tagging.device import probe_device, plan_batch
from .support import TEMP_ROOT
from .tagger_support import fixture, MemoryDownload, RUNTIME, MODEL


class ModelsTest(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'models'
        self.manifest, self.content = fixture()
        self.download = MemoryDownload(self.content)
        self.cancel = threading.Event()
        self.store = ModelStore(self.root, download=self.download, validator=lambda *_: None)

    def assert_code(self, code, function):
        with self.assertRaises(ServiceError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_trust_registry_is_fixed_and_json_contract_is_closed(self):
        self.assertEqual(len(REVIEWED_MANIFESTS), 1)
        self.assertEqual(REVIEWED_MANIFESTS[0].model_id, 'wd-eva02-large-tagger-v3')
        original = self.manifest.model_dump(mode='json')
        changes = [lambda v: v.update(revision='main'), lambda v: v.update(repo_id='https://example.com/model'),
                   lambda v: v['files'][0].update(name='../model.onnx'), lambda v: v.update(model_id='CON'),
                   lambda v: v['files'][0].update(name='c:model.onnx'), lambda v: v.update(digest='0'*64),
                   lambda v: v.update(endpoint='https://example.com'), lambda v: v['files'][0].update(size=True)]
        for change in changes:
            with self.subTest(change=change):
                value = json.loads(json.dumps(original))
                change(value)
                self.assert_code('tagger:manifest_invalid', lambda: parse_manifest(json.dumps(value).encode()))
        self.assert_code('tagger:manifest_invalid', lambda: parse_manifest(b'{"a":1,"a":2}'))
        self.assert_code('tagger:manifest_invalid', lambda: parse_manifest(b' '*32769))

    def test_atomic_install_reuse_and_verified_marker(self):
        phases, progress = [], []
        result = self.store.install(self.manifest, self.cancel, phase=phases.append, progress=lambda *p: progress.append(p))
        self.assertFalse(result['reused'])
        directory = self.store.directory(self.manifest)
        self.assertTrue((directory/'ready.json').exists())
        self.assertEqual(progress[-1], (sum(map(len, self.content.values())),)*2)
        self.assertEqual(phases, ['downloading', 'verifying_files', 'verifying_csv', 'verifying_tensors', 'publishing'])
        again = self.store.install(self.manifest, self.cancel)
        self.assertTrue(again['reused'])
        self.assertEqual(again['stamp'], result['stamp'])
        self.assertEqual(len(self.download.calls), 2)
        self.assertEqual(list(directory.parent.glob('*.partial')), [])

    def test_checksum_length_short_and_overflow_never_publish(self):
        for data in (MODEL[:-1], MODEL+b'x', b'x'*len(MODEL)):
            with self.subTest(size=len(data)):
                self.download.content = {**self.content, 'model.onnx': data}
                self.assert_code('tagger:checksum_mismatch', lambda: self.store.install(self.manifest, self.cancel))
                self.assertFalse(self.store.directory(self.manifest).exists())
                self.assertEqual(list(self.root.rglob('*.partial')), [])

    def test_csv_structure_categories_count_and_duplicate_names(self):
        values = [b'tag,category\na,0\nb,4\nc,9\n', b'name,category\na,0\n',
                  b'name,category\na,0\na,4\nc,9\n', b'name,category\na,2\nb,4\nc,9\n',
                  b'name,category\na,0\nb,4\nc,9\nd,0\n', b'name,category\n,0\nb,4\nc,9\n']
        for data in values:
            with self.subTest(data=data):
                manifest, content = fixture(csv=data)
                store = ModelStore(self.root, download=MemoryDownload(content), validator=lambda *_: None)
                self.assert_code('tagger:csv_invalid', lambda: store.install(manifest, self.cancel))
                self.assertFalse(store.directory(manifest).exists())

    def test_cancel_during_download_or_validation_cleans_only_this_version(self):
        self.store.install(self.manifest, self.cancel)
        original = self.store.directory(self.manifest)/'ready.json'
        original_bytes = original.read_bytes()
        other, _ = fixture(revision='b'*40)
        for where in ('downloading', 'verifying_tensors', 'publishing'):
            self.cancel.clear()
            def phase(value):
                if value == where:
                    self.cancel.set()
            self.assert_code('operation:canceled', lambda: self.store.install(other, self.cancel, phase=phase))
            self.assertFalse(self.store.directory(other).exists())
            self.assertEqual(list(original.parent.parent.glob('*.partial')), [])
            self.assertEqual(original.read_bytes(), original_bytes)

    def test_disk_failure_cleans_staging_and_does_not_publish(self):
        with patch('stylelora.tagging.models.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.install(self.manifest, self.cancel)
        self.assertFalse(self.store.directory(self.manifest).exists())
        self.assertEqual(list(self.root.rglob('*.partial')), [])

    def test_cancel_before_start_makes_no_directory_or_network_request(self):
        self.cancel.set()
        self.assert_code('operation:canceled', lambda: self.store.install(self.manifest, self.cancel))
        self.assertFalse(self.root.exists())
        self.assertEqual(self.download.calls, [])

    def test_corrupt_ready_version_is_not_overwritten(self):
        self.store.install(self.manifest, self.cancel)
        path = self.store.directory(self.manifest)/'model.onnx'
        path.write_bytes(b'x'*len(MODEL))
        self.assert_code('tagger:checksum_mismatch', lambda: self.store.install(self.manifest, self.cancel))
        self.assertEqual(path.read_bytes(), b'x'*len(MODEL))
        self.assertEqual(len(self.download.calls), 2)

    def test_stale_partial_ignored_and_missing_or_forged_marker_rejected(self):
        parent = self.store.directory(self.manifest).parent
        orphan = parent/'old.partial'
        orphan.mkdir(parents=True)
        (orphan/'keep').write_text('owned by a previous invocation')
        self.store.install(self.manifest, self.cancel)
        self.assertTrue((orphan/'keep').exists())
        marker = self.store.directory(self.manifest)/'ready.json'
        marker.write_text('{}')
        self.assert_code('tagger:model_invalid', lambda: self.store.verify(self.manifest, self.cancel))
        marker.unlink()
        self.assert_code('tagger:model_invalid', lambda: self.store.verify(self.manifest, self.cancel))

    def test_path_link_rejection_precedes_any_io(self):
        with patch('stylelora.tagging.models.is_link', side_effect=lambda p: p == self.root):
            self.assert_code('tagger:path_unsafe', lambda: self.store.install(self.manifest, self.cancel))
        self.assertEqual(self.download.calls, [])
        self.assertFalse(self.root.exists())

    @unittest.skipUnless(runtime_status()['available'], 'optional wd-cpu runtime not installed')
    def test_real_onnx_cpu_validation_and_tensor_mismatch(self):
        self.store.validator = validate_tensors
        self.store.install(self.manifest, self.cancel)
        path = self.store.directory(self.manifest)/'model.onnx'
        # This is actual ORT execution of a synthetic graph, not WD accuracy testing.
        import onnxruntime as ort
        import numpy as np
        session = ort.InferenceSession(path.read_bytes(), providers=['CPUExecutionProvider'])
        self.assertEqual(session.run(None, {'pixels': np.ones((1, 2, 2, 3), dtype=np.float32)})[0].tolist(), [[1., 1., 1.]])
        for key, value in [('layout', 'NCHW'), ('input_name', 'wrong'), ('batch', 'dynamic'), ('output_count', 4)]:
            manifest, _ = fixture(change=lambda v: v['tensor_schema'].update({key: value}))
            self.assert_code('tagger:tensor_mismatch', lambda: validate_tensors(manifest, path))
        path.write_bytes(b'not an ONNX model')
        self.assert_code('tagger:tensor_mismatch', lambda: validate_tensors(self.manifest, path))


class DownloadTest(unittest.TestCase):
    def test_fixed_revision_redirect_and_stream(self):
        manifest, content = fixture()
        seen = []
        def handle(request):
            seen.append(str(request.url))
            self.assertNotIn('authorization', request.headers)
            if request.url.host == 'huggingface.co':
                return httpx.Response(302, headers={'Location': 'https://cdn-lfs.hf.co/test?signature=private'})
            return httpx.Response(200, content=content['model.onnx'])
        download = HuggingFaceDownload(transport=httpx.MockTransport(handle))
        self.assertEqual(b''.join(download(manifest, manifest.model_file, threading.Event())), MODEL)
        self.assertIn('/resolve/'+'a'*40+'/model.onnx', seen[0])
        self.assertEqual(len(seen), 2)

    def test_reject_unbounded_redirects_encoding_wrong_length_and_network_errors(self):
        manifest, _ = fixture()
        replies = [lambda r: httpx.Response(302, headers={'Location': 'http://127.0.0.1/secret'}),
                   lambda r: httpx.Response(302, headers={'Location': str(r.url)}),
                   lambda r: httpx.Response(200, content=b'too short'),
                   lambda r: httpx.Response(500, content=b'secret response')]
        def timeout(request):
            raise httpx.ReadTimeout('https://cdn.hf.co/?secret=value', request=request)
        replies.append(timeout)
        for handler in replies:
            with self.subTest(handler=handler), self.assertRaises(ServiceError) as caught:
                list(HuggingFaceDownload(transport=httpx.MockTransport(handler))(manifest, manifest.model_file, threading.Event()))
            self.assertEqual(caught.exception.code, 'tagger:download_failed')
            self.assertNotIn('secret', str(caught.exception))
        for url in ['https://huggingface.co.evil.com/x', 'https://u:p@huggingface.co/x', 'https://huggingface.co:888/x', 'file:///x']:
            self.assertFalse(allowed_url(url))

    def test_deadline_and_cancel_do_not_send(self):
        manifest, _ = fixture()
        def no_send(request):
            self.fail('Unexpected network call')
        clock = iter([0, 1801])
        with self.assertRaises(ServiceError):
            list(HuggingFaceDownload(transport=httpx.MockTransport(no_send), clock=lambda: next(clock))(manifest, manifest.model_file, threading.Event()))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ServiceError):
            list(HuggingFaceDownload(transport=httpx.MockTransport(no_send))(manifest, manifest.model_file, cancel))

    def test_streaming_trickle_checks_total_deadline_before_buffer_fills(self):
        manifest, _ = fixture()
        now = [0]
        class Trickle(httpx.SyncByteStream):
            def __iter__(self):
                yield b'x'
                now[0] = 1801
                yield b'y'
                raise AssertionError('Read beyond the deadline')
        download = HuggingFaceDownload(clock=lambda: now[0], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Trickle())))
        iterator = download(manifest, manifest.model_file, threading.Event())
        self.assertEqual(next(iterator), b'x')
        with self.assertRaises(ServiceError) as caught:
            next(iterator)
        self.assertEqual(caught.exception.code, 'tagger:download_failed')


class DeviceTest(unittest.TestCase):
    def test_nvml_first_smi_fallback_and_no_false_cuda_claim(self):
        def unavailable():
            raise OSError('not installed')
        device = probe_device(nvml=lambda: (10*1024**3, 7*1024**3), smi=lambda: self.fail(), status=lambda: RUNTIME)
        self.assertEqual(device['provider'], 'cpu')
        self.assertFalse(device['cuda_verified'])
        self.assertEqual(device['reserved_bytes'], 2*1024**3)
        self.assertEqual(probe_device(nvml=unavailable, smi=lambda: (100, 50), status=lambda: RUNTIME)['source'], 'nvidia-smi')
        missing = probe_device(nvml=lambda: (10, 100), smi=unavailable, status=lambda: RUNTIME)
        self.assertIsNone(missing['total_bytes'])
        self.assertEqual(missing['reason'], 'tagger:cuda_unavailable')
        manifest, _ = fixture()
        self.assertEqual(plan_batch(device, manifest, 64), 1)
        with self.assertRaises(ValueError):
            plan_batch(device, manifest, True)

    def test_optional_runtime_missing_and_conflict(self):
        from importlib.metadata import PackageNotFoundError
        with patch('stylelora.tagging.model_runtime.metadata.version', side_effect=PackageNotFoundError):
            self.assertEqual(runtime_status()['reason'], 'tagger:runtime_missing')
        with patch('stylelora.tagging.model_runtime.metadata.version', return_value='1.0'):
            self.assertEqual(runtime_status()['reason'], 'tagger:runtime_conflict')
