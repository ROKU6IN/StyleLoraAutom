"""Explicit offline acceptance reference, pinned to the publisher's demo code.

No remote imports or top-level code execute. Only the reviewed prepare_image
method is compiled after checking the entire downloaded source's fixed digest.
The source and example image are local acceptance inputs, not shipped assets.
"""
import ast
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageOps

REFERENCE_REVISION = 'fbd35b187d6e8cb8a00c529d52999f82d6b5193b'
REFERENCE_SHA256 = 'a6526595fe9582d4307732ac1b3778df8a89a0a69d690e17f91813bf4b0063f7'
IMAGE_SHA256 = '6fbc5cdb614aaa882067e80abbcc2773c284fdeabccd0ee269364504e9fc0995'


def checked_bytes(path, expected, limit):
    with Path(path).open('rb') as stream:
        content = stream.read(limit+1)
    if len(content) > limit or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError('Acceptance input does not match its pinned SHA-256')
    return content


def load_reference(path):
    content = checked_bytes(path, REFERENCE_SHA256, 128*1024)
    import numpy as np
    tree = ast.parse(content)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Predictor')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'prepare_image')
    module = ast.Module(body=[method], type_ignores=[])
    namespace = {'Image': Image, 'np': np}
    exec(compile(module, '<pinned-wd-reference-prepare-image>', 'exec'), namespace)
    def prepare(content, size):
        with Image.open(BytesIO(content)) as image:
            # Our import contract honors EXIF; the publisher function expects an
            # already oriented RGBA image from its UI image component.
            oriented = ImageOps.exif_transpose(image).convert('RGBA')
            return namespace['prepare_image'](SimpleNamespace(model_target_size=size), oriented)
    return prepare


def samples(path):
    """One real publisher sample and deterministic format/geometry variants."""
    content = checked_bytes(path, IMAGE_SHA256, 10*1024**2)
    with Image.open(BytesIO(content)) as opened:
        original = opened.convert('RGB')
    result = {'publisher-original.jpg': content}
    def add(name, image, **options):
        buffer = BytesIO()
        image.save(buffer, format='PNG', **options)
        result[name] = buffer.getvalue()
    add('portrait.png', original)
    add('landscape.png', original.rotate(90, expand=True))
    rgba = original.convert('RGBA')
    rgba.putalpha(150)
    add('alpha.png', rgba)
    rotated = original.transpose(Image.Transpose.ROTATE_90)
    exif = rotated.getexif()
    exif[274] = 6
    add('exif.png', rotated, exif=exif)
    add('small.png', original.resize((96, 128)))
    return result


def compare_scores(actual, expected, *, atol=1e-5, rtol=1e-4):
    import numpy as np
    actual, expected = np.asarray(actual), np.asarray(expected)
    if (actual.shape != expected.shape or actual.ndim != 2 or actual.shape[0] != 1
            or not np.isfinite(actual).all() or not np.isfinite(expected).all()
            or (actual < 0).any() or (actual > 1).any()
            or (expected < 0).any() or (expected > 1).any()):
        raise AssertionError('Invalid score shape or probability')
    delta = float(np.max(np.abs(actual-expected)))
    if not np.allclose(actual, expected, atol=atol, rtol=rtol):
        raise AssertionError(f'Reference score mismatch; maximum absolute error={delta}')
    return delta


def expected_tags(scores, labels, general, character):
    # Independent category/normalization oracle: do not call product decoder.
    values = {}
    for (name, category), score in zip(labels, scores[0].tolist()):
        if category == 9 or score < (general if category == 0 else character):
            continue
        key = name.strip().lower().replace('_', ' ').strip()
        values[key] = max(values.get(key, 0.), score)
    return values
