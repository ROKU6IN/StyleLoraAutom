from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from stylelora.imaging.phash import HashRecord, PhashError, candidate_rows, compute_phash, find_candidates, hamming
from stylelora.exchange.importer import import_images
from stylelora.project_db.repositories import images
from .exchange_support import ExchangeTest
from .support import TEMP_ROOT


def pattern(size=(64, 48)):
    image = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((3, 7, 31, 38), fill='black')
    draw.ellipse((25, 9, 59, 40), fill=(90, 25, 160))
    draw.line((1, 46, 62, 2), fill=(20, 180, 70), width=3)
    return image


class PhashTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, image, name='image.png', **kwargs):
        path = self.root/name
        image.save(path, **kwargs)
        return path

    def test_dc_and_zero_golden(self):
        self.assertEqual(compute_phash(self.save(Image.new('RGB', (32, 32), 'black'))), '0000000000000000')
        self.assertEqual(compute_phash(self.save(Image.new('RGB', (32, 32), 'white'))), '8000000000000000')

    def test_pattern_golden_and_original_bytes_unchanged(self):
        path = self.save(pattern())
        before = path.read_bytes()
        value = compute_phash(path)
        self.assertEqual(value, 'ab1ac5c6d4e162ec')
        self.assertEqual(compute_phash(path), value)
        self.assertEqual(before, path.read_bytes())

    def test_orientation_normalization(self):
        original = pattern()
        upright = self.save(original, 'upright.png')
        exif = Image.Exif()
        exif[274] = 6
        rotated = self.save(original.transpose(Image.Transpose.ROTATE_90), 'rotated.png', exif=exif)
        self.assertEqual(compute_phash(upright), compute_phash(rotated))

    def test_white_alpha_composite_and_first_frame(self):
        rgba = pattern().convert('RGBA')
        rgba.putalpha(100)
        white = Image.new('RGBA', rgba.size, 'white')
        white.alpha_composite(rgba)
        self.assertEqual(compute_phash(self.save(rgba, 'alpha.png')), compute_phash(self.save(white.convert('RGB'), 'white.png')))
        first = pattern().convert('P')
        animated = self.save(first, 'animated.gif', save_all=True, append_images=[Image.new('P', first.size, 0)], duration=100)
        self.assertEqual(compute_phash(animated), compute_phash(self.save(first, 'first.png')))

    def test_resized_content_remains_a_candidate(self):
        original = pattern()
        a = compute_phash(self.save(original, 'a.png'))
        b = compute_phash(self.save(original.resize((128, 96), Image.Resampling.LANCZOS), 'b.png'))
        self.assertLessEqual(hamming(a, b), 6)

    def test_bad_image_and_unpinned_preprocessor_fail_safely(self):
        path = self.root/'bad.png'
        path.write_bytes(b'not image')
        with self.assertRaises(PhashError):
            compute_phash(path)
        with patch('stylelora.imaging.phash.pillow_version', 'future-version'):
            with self.assertRaises(PhashError):
                compute_phash(self.save(pattern()))

    def test_hamming_extremes_and_validation(self):
        self.assertEqual(hamming('0'*16, '0'*16), 0)
        self.assertEqual(hamming('0'*16, 'f'*16), 64)
        for invalid in ('F'*16, '0'*15, None, 'x'*16):
            with self.assertRaises(PhashError):
                hamming(invalid, '0'*16)
        for threshold in (-1, 65, True, 1.0):
            with self.assertRaises(PhashError):
                find_candidates([], threshold=threshold)

    def test_pairs_stable_unique_nontransitive(self):
        rows = [HashRecord(3, '0000000000000003'), HashRecord(1, '0'*16), HashRecord(2, '0000000000000001')]
        pairs = find_candidates(rows, threshold=1)
        self.assertEqual([(p.left_id, p.right_id, p.distance) for p in pairs], [(1, 2, 1), (2, 3, 1)])
        self.assertEqual(pairs, find_candidates(tuple(reversed(rows)), threshold=1))
        self.assertEqual(len(find_candidates(rows, threshold=64)), 3)
        with self.assertRaises(PhashError):
            find_candidates([rows[0], rows[0]], threshold=0)

    def test_cancellation_stops_at_row_boundary(self):
        canceled = False
        stream = candidate_rows([HashRecord(i, '0'*16) for i in range(1, 1001)], threshold=64, cancelled=lambda: canceled)
        count, _ = next(stream)
        self.assertEqual(count, 999)
        canceled = True
        with self.assertRaisesRegex(PhashError, 'operation:canceled'):
            next(stream)


class PhashImportTests(ExchangeTest):
    def test_import_persists_hash_without_merging_distance_zero(self):
        first = self.input_image('red.png', color='red')
        second = self.input_image('blue.png', color='blue')
        report = import_images(self.project, self.inputs)
        self.assertEqual(report.imported, 2)
        rows = [self.read(images.get_image, result.image_id) for result in report.items]
        self.assertEqual(hamming(rows[0].phash, rows[1].phash), 0)
        self.assertNotEqual(rows[0].sha256, rows[1].sha256)
        self.assertEqual({row.review_status for row in rows}, {'unreviewed'})
        self.assertEqual(import_images(self.project, self.inputs).skipped, 2)
        self.assertTrue(first.is_file() and second.is_file())

    def test_hash_failure_reports_warning_and_keeps_valid_import(self):
        self.input_image('valid.png')
        with patch('stylelora.exchange.importer.compute_phash', side_effect=PhashError('phash:unavailable')):
            report = import_images(self.project, self.inputs)
        self.assertEqual(report.imported, 1)
        self.assertIn('phash:unavailable', report.items[0].warnings)
        self.assertIsNone(self.read(images.get_image, report.items[0].image_id).phash)

    def test_receipt_recovery_keeps_computed_hash(self):
        self.input_image('recover.png')
        def fault(point):
            if point == 'import_after_receipt':
                raise RuntimeError('simulated interruption')
        self.project._fault_hook = fault
        with self.assertRaises(RuntimeError):
            import_images(self.project, self.inputs)
        self.project._fault_hook = None
        self.reopen()
        rows = self.sql('SELECT image_id,phash FROM images')
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0][1], '^[0-9a-f]{16}$')
