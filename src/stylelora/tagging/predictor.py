"""Manifest-driven local WD inference. No project writes, network or rating decisions."""
import csv
from dataclasses import dataclass
import hashlib
from io import BytesIO
from typing import Literal
import warnings

from PIL import Image, ImageOps, __version__ as pillow_version
from pydantic import Field

from ..exchange.common import read_bounded
from ..project_db.types import Tag
from ..project_db.validation import normalize_tag, relative_path
from ..runtime.errors import ServiceError
from .device import probe_device, plan_batch
from .manifests import Contract, Hex
from .model_runtime import create_session, runtime_status
from .models import check_cancel
from .gpu_profiles import CUDA_POLICY, matching_profile


class TaggerConfig(Contract):
    manifest_id: Hex
    general_threshold: float = Field(ge=0, le=1)
    character_threshold: float = Field(ge=0, le=1)
    batch_size: int = Field(default=1, ge=1, le=4)
    provider: Literal['cpu', 'auto'] = 'cpu'
    backend: Literal['onnx'] = 'onnx'


@dataclass(frozen=True)
class TaggerResult:
    tags: tuple[Tag, ...]
    actual_model: str
    params: dict


def model_name(manifest):
    return manifest.model_id + '@' + manifest.revision


def cache_parameters(manifest, config, runtime_version, provider):
    return {'backend': 'onnx', 'manifest_hash': manifest.digest, 'csv_hash': manifest.csv_file.sha256,
            'preprocess_version': manifest.preprocess_version, 'normalization_version': 'f01-tags-v1',
            'tag_index_hash': 'none:wd-v1', 'pillow_version': pillow_version, 'runtime_version': runtime_version,
            'general_threshold': config.general_threshold, 'character_threshold': config.character_threshold,
            'provider': provider, **({'cuda_policy': CUDA_POLICY} if provider == 'cuda' else {})}


def read_original(project, row):
    if row.file_missing:
        raise ServiceError(410, 'image:file_missing')
    path = relative_path(project.root, 'images/' + row.filename)
    try:
        if not path.is_file():
            raise FileNotFoundError()
        if path.stat().st_size > 100*1024**2:
            raise ServiceError(413, 'image:too_large')
        content = read_bounded(path, 100*1024**2)
    except FileNotFoundError:
        raise ServiceError(410, 'image:file_missing') from None
    if len(content) != row.file_size or hashlib.sha256(content).hexdigest() != row.sha256:
        raise ServiceError(409, 'image:image_changed')
    return content


def preprocess(content, manifest):
    import numpy as np
    schema = manifest.tensor_schema
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as original:
                # Bound the padded canvas too: a very thin image may have few
                # source pixels but would otherwise allocate an enormous square.
                if max(original.size)**2 > 100_000_000:
                    raise ServiceError(413, 'image:too_large')
                original.seek(0)
                rgba = ImageOps.exif_transpose(original).convert('RGBA')
                white = Image.new('RGBA', rgba.size, 'white')
                rgb = Image.alpha_composite(white, rgba).convert('RGB')
                side = max(rgb.size)
                canvas = Image.new('RGB', (side, side), 'white')
                canvas.paste(rgb, ((side-rgb.width)//2, (side-rgb.height)//2))
                if side != schema.size:
                    canvas = canvas.resize((schema.size, schema.size), Image.Resampling.BICUBIC)
                value = np.asarray(canvas, dtype=np.float32)
        if schema.color_order == 'BGR':
            value = value[:, :, ::-1]
        if schema.normalization == '0_1':
            value = value / np.float32(255)
        elif schema.normalization == 'minus1_1':
            value = value / np.float32(127.5) - np.float32(1)
        if schema.layout == 'NCHW':
            value = value.transpose(2, 0, 1)
        return np.ascontiguousarray(value[None], dtype=np.float32)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise ServiceError(422, 'image:decode_failed') from None


def decode_scores(scores, labels, config):
    import numpy as np
    value = np.asarray(scores)
    if value.shape != (1, len(labels)) or not np.issubdtype(value.dtype, np.floating):
        raise ServiceError(503, 'tagger:invalid_output')
    if not np.isfinite(value).all() or (value < 0).any() or (value > 1).any():
        raise ServiceError(503, 'tagger:invalid_output')
    tags, evidence, ratings = {}, [], []
    for (name, category), score in zip(labels, value[0].tolist()):
        if category == 9:
            ratings.append({'name': name, 'confidence': score})
            continue
        threshold = config.general_threshold if category == 0 else config.character_threshold
        if score < threshold:
            continue
        canonical = normalize_tag(name)
        tags[canonical] = max(tags.get(canonical, 0.), score)
        evidence.append({'original_name': name, 'category': category, 'canonical': canonical, 'confidence': score})
    return tuple(Tag(name, score) for name, score in tags.items()), evidence, ratings


def is_oom(error):
    return isinstance(error, MemoryError) or any(word in str(error).lower() for word in
        ('out of memory', 'cudaerrormemoryallocation', 'failed to allocate memory', 'alloc failed'))


class OrtTagger:
    def __init__(self, store, manifest, config, cancel, *, session_factory=create_session,
                 device_probe=probe_device, status=runtime_status, profile=None):
        self.manifest, self.config, self.store = manifest, config, store
        self.session_factory, self.device_probe = session_factory, device_probe
        self.runtime_version = status()['version']
        self.provider, self.fallback_reason = 'cpu', None
        self.profile, self.batch_limit = profile, 4
        self.stamp = store.verify(manifest, cancel)
        self.path = store.directory(manifest)/manifest.model_file.name
        with (self.path.parent/manifest.csv_file.name).open(encoding='utf-8-sig', newline='') as stream:
            self.labels = tuple((r['name'], int(r['category'])) for r in csv.DictReader(stream))
        check_cancel(cancel)
        if config.provider == 'auto':
            device = device_probe()
            self.profile = profile or matching_profile(device, manifest.digest, self.runtime_version)
            if self._has_memory(device):
                try:
                    self.session = session_factory(manifest, self.path, 'cuda')
                    self.provider = 'cuda'
                except Exception:
                    self.fallback_reason = 'tagger:cuda_unavailable'
            else:
                self.fallback_reason = 'tagger:low_memory'
        if self.provider == 'cpu':
            self.session = session_factory(manifest, self.path, 'cpu')
        check_cancel(cancel)

    def _cpu(self, reason):
        if self.store.footprint(self.manifest) != self.stamp:
            raise ServiceError(503, 'tagger:model_changed')
        self.session = None  # release CUDA allocations before creating CPU session
        self.session = self.session_factory(self.manifest, self.path, 'cpu')
        self.provider, self.fallback_reason = 'cpu', reason

    def predict_one(self, content, cancel):
        result = self.predict_many([content], cancel)[0]
        if isinstance(result, ServiceError):
            raise result
        return result

    def batch_capacity(self):
        if self.provider == 'cpu':
            return 1
        device = {**self.device_probe(), 'provider': 'cuda', 'runtime_version': self.runtime_version}
        return plan_batch(device, self.manifest, min(self.batch_limit, self.config.batch_size), profile=self.profile)

    def _has_memory(self, device):
        free, reserved = device.get('free_bytes'), device.get('reserved_bytes')
        if free is None or reserved is None or free <= reserved:
            return False
        minimum = self.profile['required_bytes']['1'] if self.profile else 0
        return free-reserved >= minimum

    def _check(self, cancel):
        check_cancel(cancel)
        if self.store.footprint(self.manifest) != self.stamp:
            raise ServiceError(503, 'tagger:model_changed')

    def predict_many(self, contents, cancel):
        """At most four inputs; ordered results or per-image errors, no DB effects."""
        import numpy as np
        if not 1 <= len(contents) <= 4:
            raise ValueError('Expected one to four images')
        self._check(cancel)
        results, ready = [None]*len(contents), []
        for i, content in enumerate(contents):
            self._check(cancel)
            try:
                ready.append((i, preprocess(content, self.manifest)))
            except ServiceError as error:
                results[i] = error
        while ready:
            self._check(cancel)
            if self.provider == 'cuda':
                device = self.device_probe()
                if not self._has_memory(device):
                    self._cpu('tagger:low_memory')
            size = min(len(ready), self.batch_capacity())
            group = ready[:size]
            value = np.concatenate([v for _, v in group], axis=0)
            try:
                output = self.session.run([self.manifest.tensor_schema.output_name],
                    {self.manifest.tensor_schema.input_name: value})[0]
            except Exception as error:
                self._check(cancel)
                if self.provider == 'cuda' and is_oom(error):
                    if size > 1:
                        self.batch_limit = max(1, size//2)
                        self.fallback_reason = 'tagger:oom'
                        continue
                    self._cpu('tagger:oom')
                    continue  # Exactly one CPU attempt per image, then per-image failure.
                failure = ServiceError(503, 'tagger:oom' if is_oom(error) else 'tagger:inference_failed')
                for i, _ in group:
                    results[i] = failure
            else:
                self._check(cancel)
                if np.asarray(output).shape != (size, len(self.labels)):
                    for i, _ in group:
                        results[i] = ServiceError(503, 'tagger:invalid_output')
                else:
                    for j, (i, _) in enumerate(group):
                        try:
                            results[i] = self._result(output[j:j+1], size)
                        except ServiceError as error:
                            results[i] = error
            del ready[:size]
        self._check(cancel)
        return tuple(results)

    def _result(self, output, batch):
        tags, evidence, ratings = decode_scores(output, self.labels, self.config)
        actual = cache_parameters(self.manifest, self.config, self.runtime_version, self.provider)
        return TaggerResult(tags, model_name(self.manifest), {'schema_version': 1,
            'requested': self.config.model_dump(), 'actual': {**actual, 'batch_size': batch},
            'tag_mapping': evidence, 'rating_scores': ratings, 'fallback_reason': self.fallback_reason})

    def close(self):
        self.session = None
