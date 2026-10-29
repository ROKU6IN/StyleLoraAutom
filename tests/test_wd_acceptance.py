import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stylelora.runtime.errors import ServiceError
from stylelora.tagging.provenance import verify_git_blob
from stylelora.tagging.manifests import REVIEWED_MANIFESTS, reviewed_provenance
from stylelora.tagging.models import validate_csv
from .support import TEMP_ROOT
from .wd_cpu_reference import checked_bytes, compare_scores, expected_tags, load_reference


class ProvenanceTest(unittest.TestCase):
    def test_reference_download_rejects_redirects_oversize_and_wrong_hash(self):
        import httpx
        from scripts.prepare_wd_cpu import fetch_reference
        native = httpx.Client
        content = b'pinned reference'
        expected = hashlib.sha256(content).hexdigest()
        def invoke(response, digest=expected, limit=100):
            transport = httpx.MockTransport(lambda request: response)
            with patch('httpx.Client', side_effect=lambda **kwargs: native(transport=transport, **kwargs)):
                return fetch_reference('app.py', digest, limit)
        self.assertEqual(invoke(httpx.Response(200, content=content)), content)
        for response, digest, limit in [(httpx.Response(302, headers={'Location': 'https://example.com/private'}), expected, 100),
            (httpx.Response(200, content=content), '0'*64, 100),
            (httpx.Response(200, content=content), expected, 1),
            (httpx.Response(500, content=b'server details'), expected, 100)]:
            with self.assertRaises(RuntimeError):
                invoke(response, digest, limit)

    def test_reviewed_manifest_matches_pinned_publisher_label_table(self):
        manifest = REVIEWED_MANIFESTS[0]
        self.assertEqual(manifest.digest, '90f63fb27ed54b7abc1c72c9b2009e0fe5248e7bf15293c195d7f5ed117d35d1')
        path = Path(__file__).parent/'fixtures/wd-v3/selected_tags.csv'
        raw = path.read_bytes()
        proof = reviewed_provenance(manifest)
        self.assertEqual(verify_git_blob(raw, oid=proof['csv_git_blob'], size=manifest.csv_file.size), manifest.csv_file.sha256)
        validate_csv(manifest, path)
        TEMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            changed = Path(temp)/'changed.csv'
            changed.write_bytes(raw.replace(b'long_hair', b'fake_hair', 1))
            with self.assertRaises(ServiceError) as caught:
                validate_csv(manifest, changed)
            self.assertEqual(caught.exception.code, 'tagger:provenance_mismatch')

    def test_git_object_header_and_content_are_bound(self):
        content = b'name,category\nblue_hair,0\n'
        oid = hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
        self.assertEqual(verify_git_blob(content, oid=oid, size=len(content)), hashlib.sha256(content).hexdigest())
        for data, identity, size in [(content, '0'*40, len(content)), (content+b'\n', oid, len(content)),
                                    (content, oid, True), (content, 'main', len(content)),
                                    (content.replace(b'blue', b'cyan'), oid, len(content))]:
            with self.assertRaises(ServiceError):
                verify_git_blob(data, oid=identity, size=size)

    def test_reference_code_is_rejected_before_execution_on_digest_mismatch(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temp:
            path = Path(temp)/'app.py'
            path.write_bytes(b'raise RuntimeError("must not execute")')
            with self.assertRaises(ValueError):
                load_reference(path)
            with self.assertRaises(ValueError):
                checked_bytes(path, hashlib.sha256(path.read_bytes()).hexdigest(), 1)

    def test_numerical_comparison_rejects_nan_shape_and_large_drift(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('optional numpy unavailable')
        self.assertLess(compare_scores([[.2, .8]], [[.200001, .8]]), 1e-5)
        for actual in [[[.4, .8]], [[float('nan'), .8]], [[.2]], [[1.1, .8]], [.2, .8]]:
            with self.assertRaises(AssertionError):
                compare_scores(actual, [[.2, .8]])
        self.assertEqual(expected_tags(np.array([[.5, .8, .9]]), [('BLUE_HAIR',0), ('blue hair',4), ('general',9)], .5, .7), {'blue hair': .8})
