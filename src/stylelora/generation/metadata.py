"""Bounded PNG inspection; metadata is evidence, never source configuration."""
from dataclasses import dataclass
import hashlib
import json
from io import BytesIO
from pathlib import Path
import struct
import warnings
import zlib

from PIL import Image

from ..imaging.phash import compute_phash, PhashError
from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError
from ..tagging.tokenizer import tokenize_prompt, VERSION

MAX_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 100_000_000
MAX_COMMENT = 1024 * 1024
QUALITY_TAGS = frozenset(('very aesthetic', 'masterpiece', 'no text'))


@dataclass(frozen=True)
class ArtifactInspection:
    width: int
    height: int
    file_size: int
    sha256: str
    phash: str | None
    png_meta: dict | None
    validation: str
    reason_codes: tuple[str, ...]
    missing_style_tags: tuple[str, ...] = ()
    unexpected_quality_tags: tuple[str, ...] = ()


def _chunks(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError()
    offset, comments, ended = 8, 0, False
    while offset + 12 <= len(data):
        length = struct.unpack_from('>I', data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError()
        kind = data[offset+4:offset+8]
        payload = data[offset+8:end-4]
        if zlib.crc32(kind + payload) != struct.unpack_from('>I', data, end-4)[0]:
            raise ValueError()
        if kind == b'acTL':
            raise ValueError()  # A generated item must be one static image.
        if kind in (b'tEXt', b'zTXt', b'iTXt') and payload.split(b'\0', 1)[0] == b'Comment':
            comments += 1
        offset = end
        if kind == b'IEND':
            if length or offset != len(data):
                raise ValueError()
            ended = True
            break
    if not ended:
        raise ValueError()
    return comments


def _tags(value):
    tokens = tokenize_prompt(value)
    if any(token.reason for token in tokens):
        return None
    return {' '.join(token.normalized.split()) for token in tokens}


def _positive(comment):
    candidates = []
    if 'prompt' in comment:
        if not isinstance(comment['prompt'], str) or not comment['prompt'].strip():
            raise ValueError('meta:invalid_comment')
        candidates.append(comment['prompt'])
    if 'v4_prompt' in comment:
        value = comment['v4_prompt']
        if not isinstance(value, dict) or not isinstance(value.get('caption'), dict):
            raise ValueError('meta:invalid_comment')
        caption = value['caption']
        if caption.get('char_captions') not in (None, []):
            raise ValueError('meta:unsupported_structure')
        if not isinstance(caption.get('base_caption'), str) or not caption['base_caption'].strip():
            raise ValueError('meta:invalid_comment')
        candidates.append(caption['base_caption'])
    if not candidates:
        raise ValueError('meta:missing_prompt')
    parsed = [_tags(value) for value in candidates]
    if any(value is None for value in parsed):
        raise ValueError('meta:unsupported_syntax')
    if any(value != parsed[0] for value in parsed):
        raise ValueError('meta:ambiguous_prompt')
    return parsed[0]


def inspect_png(path: Path, *, style_prompt: str, sent_prompt: str, metadata_stripped=None,
                expected_width: int, expected_height: int):
    if metadata_stripped is not None and type(metadata_stripped) is not bool:
        raise ServiceError(422, 'artifact:invalid_inspection')
    if any(type(value) is not int or value < 1 for value in (expected_width, expected_height)):
        raise ServiceError(422, 'artifact:invalid_inspection')
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ServiceError(413, 'artifact:too_large')
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ServiceError(413, 'artifact:too_large')
    except OSError:
        raise ServiceError(422, 'artifact:io_error') from None
    try:
        comments = _chunks(data)
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                if probe.format != 'PNG' or probe.width * probe.height > MAX_PIXELS:
                    raise ValueError()
                probe.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
                width, height = image.size
                comment_text = image.info.get('Comment')
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ServiceError(422, 'artifact:invalid_png') from None
    if (width, height) != (expected_width, expected_height):
        raise ServiceError(422, 'artifact:size_mismatch')
    try:
        phash = compute_phash(BytesIO(data))
    except PhashError:
        phash = None
    meta, reasons, missing, added = None, (), (), ()
    if comments == 0:
        reasons = ('meta:stripped' if metadata_stripped else 'meta:missing',)
    else:
        try:
            if comments != 1 or not isinstance(comment_text, str) or len(comment_text.encode('utf-8')) > MAX_COMMENT:
                raise ValueError()
            comment = strict_json(comment_text)
            if not isinstance(comment, dict):
                raise ValueError()
            json.dumps(comment, ensure_ascii=False, allow_nan=False).encode('utf-8')
            meta = {'schema_version': 1, 'comment': comment}
        except (ValueError, ServiceError, UnicodeError, RecursionError):
            reasons = ('meta:invalid_comment',)
        if meta is not None:
            try:
                actual = _positive(comment)
                style, sent = _tags(style_prompt), _tags(sent_prompt)
                if style is None or sent is None:
                    raise ValueError('meta:unsupported_syntax')
                missing = tuple(sorted(style - actual))
                added = tuple(sorted((actual - sent) & QUALITY_TAGS))
                reasons = tuple(code for code, present in (
                    ('meta:style_tag_missing', missing), ('meta:unexpected_quality_tags', added)) if present)
            except ValueError as error:
                reasons = (str(error),)
    verdict = 'mismatch' if missing or added else 'unverifiable' if reasons else 'pass'
    return ArtifactInspection(width, height, len(data), hashlib.sha256(data).hexdigest(), phash,
                              meta, verdict, reasons, missing, added)


def inspection_summary(value, *, metadata_stripped=None):
    return {'schema_version': 1, 'validator': 'generation-png-v1', 'tokenizer': VERSION,
            'validation': value.validation, 'reason_codes': list(value.reason_codes),
            'missing_style_tags': list(value.missing_style_tags),
            'unexpected_quality_tags': list(value.unexpected_quality_tags), 'metadata_stripped': metadata_stripped}
