"""Reviewed, immutable model contracts. HTTP clients cannot register trusted manifests."""
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..runtime.errors import ServiceError

Hex = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Name = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, allow_inf_nan=False)


class ModelFile(Contract):
    name: Name
    sha256: Hex
    size: Annotated[int, Field(gt=0, le=2*1024**3)]

    @model_validator(mode='after')
    def safe_name(self):
        if self.name.endswith('.') or self.name.split('.')[0].upper() in {
            'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))
        }:
            raise ValueError('Unsafe filename')
        return self


class TensorSchema(Contract):
    input_name: Annotated[str, Field(min_length=1, max_length=128)]
    output_name: Annotated[str, Field(min_length=1, max_length=128)]
    size: Annotated[int, Field(ge=1, le=2048)]
    layout: Literal['NHWC', 'NCHW']
    dtype: Literal['float32']
    batch: Literal['dynamic', 'one']
    color_order: Literal['RGB', 'BGR']
    resize: Literal['pillow-bicubic']
    normalization: Literal['0_255', '0_1', 'minus1_1']
    alpha: Literal['white']
    aspect: Literal['pad_square']
    output_count: Annotated[int, Field(gt=0, le=100000)]


class CsvSchema(Contract):
    columns: tuple[str, ...]
    name_column: Literal['name']
    category_column: Literal['category']
    general: Literal[0]
    character: Literal[4]
    rating: Literal[9]

    @model_validator(mode='after')
    def columns_valid(self):
        if (not 2 <= len(self.columns) <= 16 or len(set(self.columns)) != len(self.columns)
                or not {'name', 'category'}.issubset(self.columns)
                or any(not re.fullmatch(r'[a-z_]{1,32}', c) for c in self.columns)):
            raise ValueError('Invalid CSV schema')
        return self


class ModelManifest(Contract):
    model_id: Name
    repo_id: Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')]
    revision: Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
    files: tuple[ModelFile, ModelFile]
    tensor_schema: TensorSchema
    preprocess_version: Literal['wd-square-v1']
    csv_schema: CsvSchema
    digest: Hex

    @model_validator(mode='after')
    def consistent(self):
        ModelFile(name=self.model_id, size=1, sha256='0'*64)
        names = [file.name.lower() for file in self.files]
        if (len(set(names)) != 2 or sum(n.endswith('.onnx') for n in names) != 1
                or sum(n.endswith('.csv') for n in names) != 1):
            raise ValueError('One ONNX model and one CSV required')
        if self.csv_file.size > 16*1024**2:
            raise ValueError('CSV too large')
        if self.digest != manifest_digest(self.model_dump(exclude={'digest'})):
            raise ValueError('Manifest digest mismatch')
        return self

    @property
    def model_file(self):
        return next(f for f in self.files if f.name.lower().endswith('.onnx'))

    @property
    def csv_file(self):
        return next(f for f in self.files if f.name.lower().endswith('.csv'))


def manifest_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def parse_manifest(data: bytes) -> ModelManifest:
    if len(data) > 32768:
        raise ServiceError(422, 'tagger:manifest_invalid')
    try:
        # Reject duplicate keys before validating the frozen JSON contract.
        from ..runtime.contracts import strict_json
        strict_json(data)
        return ModelManifest.model_validate_json(data)
    except (ValueError, ServiceError):
        raise ServiceError(422, 'tagger:manifest_invalid') from None


# Only developer-reviewed immutable facts enter this registry. HTTP cannot add
# manifests. CSV provenance follows the explicitly approved Git-identity route.
from .reviewed_wd import EVA02, EVA02_PROVENANCE

REVIEWED_MANIFESTS: tuple[ModelManifest, ...] = (
    parse_manifest(json.dumps({**EVA02, 'digest': manifest_digest(EVA02)}).encode()),
)


def reviewed_provenance(manifest):
    if manifest.digest == REVIEWED_MANIFESTS[0].digest:
        return dict(EVA02_PROVENANCE)
    return None
