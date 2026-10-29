"""Strict exchange configuration schemas and compatibility with v1 project defaults."""
from pathlib import PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Layer = Literal['original','wd','llm','manual','rule']
SchemaVersion = Annotated[int, Field(strict=True, ge=1, le=1)]


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ImportParams(Config):
    schema_version: SchemaVersion = 1
    encoding: Literal['utf-8-sig','utf-8','gbk','auto'] = 'utf-8-sig'
    recursive: bool = True
    layout: Literal['first_line_blank_nl'] = 'first_line_blank_nl'
    tokenizer_version: Literal['literal-tags-v1','comma-v1'] = 'comma-v1'
    on_unverifiable: Literal['preview_error'] = 'preview_error'


class ExportFilter(Config):
    schema_version: SchemaVersion = 1
    review_statuses: list[Literal['keep','unreviewed']] = Field(default_factory=lambda:['keep'])
    source_ids: list[Annotated[int,Field(gt=0)]] | None = None
    include_missing: Literal[False] = False

    @field_validator('include_missing',mode='before')
    @classmethod
    def missing_is_boolean(cls,value):
        if value is not False:
            raise ValueError('Missing files cannot be exported')
        return value

    @model_validator(mode='after')
    def unique(self):
        for values in (self.review_statuses,self.source_ids):
            if values is not None and len(values)!=len(set(values)):
                raise ValueError('Duplicate filter value')
        return self


class CaptionConfig(Config):
    schema_version: SchemaVersion = 1
    tag_layers: list[Layer] = Field(default_factory=lambda:['original','wd','llm','manual','rule'])
    layer_priority: list[Layer] = Field(default_factory=lambda:['manual','rule','llm','wd','original'])
    wd_min_confidence: float = Field(default=0.0, ge=0, le=1)
    wd_null_confidence: Literal['include','exclude'] = 'exclude'
    include_nl: bool = False
    nl_priority: list[Literal['manual','llm','original']] = Field(default_factory=lambda:['manual','llm','original'])
    tag_sort: Literal['layer_first','prompt_pyramid'] = 'layer_first'
    blocklist_mode: Literal['warn','remove'] = 'warn'
    separator: Literal[', '] = ', '
    trigger_position: Literal['first'] = 'first'

    @model_validator(mode='after')
    def priorities(self):
        if len(set(self.tag_layers))!=len(self.tag_layers) or len(self.layer_priority)!=5 or set(self.layer_priority)!={'original','wd','llm','manual','rule'}:
            raise ValueError('Layer priority must include each layer once')
        if self.nl_priority!=['manual','llm','original']:
            raise ValueError('Unsupported NL priority')
        return self


class OutputConfig(Config):
    schema_version: SchemaVersion = 1
    image_mode: Literal['copy','hardlink'] = 'copy'
    filename: Literal['{image_id}{ext}','{stem}-{image_id}{ext}'] = '{image_id}{ext}'
    caption_encoding: Literal['utf-8'] = 'utf-8'
    include_json: bool = False
    require_nonempty_tags: bool = False
    subdirectory: str = Field(default='', max_length=512)
    overwrite: Literal['error','skip'] = 'error'

    @field_validator('subdirectory')
    @classmethod
    def safe_directory(cls, value):
        if value and ('\\' in value or any(c in value for c in ':<>"|?*') or any(ord(c)<32 for c in value)
                      or PureWindowsPath(value).drive or PureWindowsPath(value).root or any(
                          p in ('','.','..') or p.endswith((' ','.')) or p.startswith('.') or PureWindowsPath(p).is_reserved() for p in value.split('/'))):
            raise ValueError('Unsafe output subdirectory')
        return value


class PipelineStep(Config):
    processor: Literal['metadata_strip','local_upscale']
    params: dict = Field(default_factory=dict)


class ImportScheme(Config):
    name: str = Field(min_length=1,max_length=512)
    reader: Literal['txt_sidecar'] = 'txt_sidecar'
    params_json: ImportParams = Field(default_factory=ImportParams)
    is_default: bool = False


class ExportScheme(Config):
    name: str = Field(min_length=1,max_length=512)
    exporter: Literal['image_txt'] = 'image_txt'
    filter_json: ExportFilter = Field(default_factory=ExportFilter)
    caption_config_json: CaptionConfig = Field(default_factory=CaptionConfig)
    output_config_json: OutputConfig = Field(default_factory=OutputConfig)
    pipeline_json: list[PipelineStep] = Field(default_factory=list,max_length=32)


def export_configuration(scheme):
    """Read old schema-v1 aliases without migrating or changing the saved row."""
    caption = dict(scheme.caption_config_json)
    if 'trigger_first' in caption:
        if caption.pop('trigger_first') is not True:
            raise ValueError('Trigger must be first')
    output = dict(scheme.output_config_json)
    legacy = 'mode' in output or 'new_timestamp_directory' in output or 'separator' in output
    if legacy:
        if output.pop('new_timestamp_directory',True) is not True or output.pop('separator',', ')!=', ':
            raise ValueError('Unsupported output configuration')
        output['image_mode'] = output.pop('mode','copy')
        caption.setdefault('blocklist_mode','remove')
    filters = dict(scheme.filter_json)
    if legacy and isinstance(filters.get('review_statuses'),list):
        filters['review_statuses']=[s for s in filters['review_statuses'] if s!='reject']
    result = ExportScheme.model_validate({'name':scheme.name,'exporter':scheme.exporter,
        'filter_json':filters,'caption_config_json':caption,'output_config_json':output,
        'pipeline_json':list(scheme.pipeline_json)}).model_dump()
    return result, legacy
