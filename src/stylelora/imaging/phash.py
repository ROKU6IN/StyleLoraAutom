"""phash64-v1: Pillow 12.3.0 preprocessing and a fixed low-frequency DCT-II."""
from dataclasses import dataclass
import math
import re
import statistics
import warnings

from PIL import Image, ImageOps, __version__ as pillow_version

ALGORITHM = 'phash64-v1'
PILLOW_VERSION = '12.3.0'
HASH = re.compile(r'[0-9a-f]{16}\Z')
# Only the first eight frequencies of the unnormalized DCT-II are needed.
COSINES = tuple(tuple(2 * math.cos(math.pi * k * (2*n + 1) / 64) for n in range(32)) for k in range(8))


class PhashError(Exception):
    pass


@dataclass(frozen=True)
class HashRecord:
    image_id: int
    phash: str


@dataclass(frozen=True)
class DuplicatePair:
    left_id: int
    right_id: int
    distance: int


def compute_phash(path, *, algorithm=ALGORITHM):
    if algorithm != ALGORITHM or pillow_version != PILLOW_VERSION:
        raise PhashError('phash:unavailable')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as original:
                original.seek(0)
                if original.width * original.height > 100_000_000:
                    raise PhashError('phash:unavailable')
                oriented = ImageOps.exif_transpose(original)
                rgba = oriented.convert('RGBA')
                background = Image.new('RGBA', rgba.size, 'white')
                background.alpha_composite(rgba)
                gray = background.convert('L').resize((32, 32), Image.Resampling.LANCZOS)
                pixels = list(gray.get_flattened_data())
        horizontal = [[math.fsum(pixels[y*32+x] * basis[x] for x in range(32)) for basis in COSINES] for y in range(32)]
        # Fix numerical zero/tie behavior as part of v1. Units are unnormalized DCT coefficients.
        coefficients = [round(math.fsum(horizontal[y][x] * basis[y] for y in range(32)), 8)
                        for basis in COSINES for x in range(8)]
        middle = statistics.median(coefficients)  # Includes the DC coefficient.
        bits = 0
        for value in coefficients:
            bits = (bits << 1) | (value > middle)
        return f'{bits:016x}'
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhashError('phash:unavailable') from None


def valid_hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def hamming(left, right):
    if not valid_hash(left) or not valid_hash(right):
        raise PhashError('phash:invalid_hash')
    return (int(left, 16) ^ int(right, 16)).bit_count()


def validate_threshold(value):
    if type(value) is not int or not 0 <= value <= 64:
        raise PhashError('phash:invalid_threshold')


def candidate_rows(records, *, threshold, cancelled=lambda: False):
    """One bounded row per yield; no transitive grouping or implicit review changes."""
    validate_threshold(threshold)
    ordered = sorted(records, key=lambda item: item.image_id)
    if any(type(r.image_id) is not int or r.image_id < 1 or not valid_hash(r.phash) for r in ordered):
        raise PhashError('phash:invalid_hash')
    if len({r.image_id for r in ordered}) != len(ordered):
        raise PhashError('phash:duplicate_id')
    values = [(r.image_id, int(r.phash, 16)) for r in ordered]
    for index, (left, value) in enumerate(values[:-1]):
        if cancelled():
            raise PhashError('operation:canceled')
        buckets = [[] for _ in range(threshold+1)]
        for right, other in values[index+1:]:
            distance = (value ^ other).bit_count()
            if distance <= threshold:
                buckets[distance].append((left, right))
        yield len(values) - index - 1, buckets


def find_candidates(records, *, threshold):
    # Convenience helper for small in-memory callers; the HTTP scanner uses candidate_rows.
    pairs = [DuplicatePair(left, right, distance) for _, buckets in candidate_rows(records, threshold=threshold)
             for distance, bucket in enumerate(buckets) for left, right in bucket]
    return tuple(sorted(pairs, key=lambda pair: (pair.distance, pair.left_id, pair.right_id)))
