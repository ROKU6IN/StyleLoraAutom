"""F05 local completion boundary. Does not establish external request ownership."""
from dataclasses import dataclass
from pathlib import Path

from ..exchange.common import project_operation
from ..project_db import commands
from ..project_db.artifacts import prepare_artifact
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.types import ImageArtifact, OriginalInput
from ..project_db.repositories import sources
from ..project_db.validation import digest, integer, relative_path
from ..runtime.errors import ServiceError
from ..tagging.tokenizer import parse_original_prompt, VERSION
from .metadata import ArtifactInspection, inspect_png, inspection_summary
from .profile import build_request, locked_projection


@dataclass(frozen=True)
class PreparedArtifact:
    artifact: ImageArtifact
    originals: tuple[OriginalInput, ...]
    inspection: ArtifactInspection


def _originals(prompt, catalog, summary):
    params = {'schema_version': 1, 'parser': VERSION, 'catalog_revision': getattr(catalog, 'revision', None),
              'generation_validation': summary}
    if not prompt.strip():
        return ()
    if catalog is None or not catalog.entries:
        return (OriginalInput('nl', prompt, params={**params, 'warnings': ['original:catalog_missing']}),)
    parsed = parse_original_prompt(prompt, catalog=catalog)
    if parsed.unverifiable:
        return (OriginalInput('nl', prompt, params={**params, 'warnings': ['original:unsupported_syntax']}),)
    values = []
    if parsed.tags:
        values.append(OriginalInput('tags', parsed.tags, params=params))
    if parsed.natural_language:
        values.append(OriginalInput('nl', parsed.natural_language, params=params))
    return tuple(values)


def _inspect_and_prepare(project, *, item_id, attempt, saved_path, metadata_stripped=None, catalog=None, owned_sha256=None):
    """Caller must first prove the returned path belongs to this dispatched request.

    Same filename/prompt alone is never permission to submit, recover or retry.
    """
    integer(item_id, 'item ID', 1)
    integer(attempt, 'attempt', 1)
    with read_snapshot(project) as db:
        item = sources.get_item(db, item_id)
        source = sources.get_source(db, item.source_id)
    if source.type != 'nai' or not source.locked or item.status != 'sent' or item.attempts != attempt:
        raise ServiceError(409, 'artifact:state_conflict')
    relative = f'images/.pending/{item_id}/{attempt}/out.png'
    path = relative_path(project.root, relative)
    candidate = Path(saved_path)
    if not candidate.is_absolute() or '..' in candidate.parts or candidate.absolute() != path.absolute():
        raise ServiceError(422, 'artifact:unexpected_path')
    request = build_request(source, item)
    inspection = inspect_png(path, style_prompt=source.style_prompt, sent_prompt=request['positive'],
        metadata_stripped=metadata_stripped, expected_width=request['width'], expected_height=request['height'])
    if owned_sha256 is not None and inspection.sha256 != owned_sha256:
        raise ServiceError(409, 'artifact:ownership_conflict')
    summary = {**inspection_summary(inspection, metadata_stripped=metadata_stripped),
        'source_id': source.source_id, 'request_digest': digest(request), 'config_digest': digest(locked_projection(source))}
    originals = _originals(item.prompt, catalog, summary)
    artifact = prepare_artifact(project, item_id=item_id, attempt=attempt, relative_staging_path=relative,
        width=inspection.width, height=inspection.height, phash=inspection.phash, png_meta=inspection.png_meta,
        originals=originals, registration_check={'schema_version': 1,
            'review_status': 'reject' if inspection.validation == 'mismatch' else 'unreviewed', 'summary': summary},
        expected_sha256=inspection.sha256, expected_file_size=inspection.file_size)
    return PreparedArtifact(artifact, originals, inspection)


def inspect_and_prepare(project, *, item_id, attempt, saved_path, metadata_stripped=None, catalog=None, owned_sha256=None):
    """Inspect an adapter-owned caller file while holding the project file-operation lease."""
    with project_operation(project):
        return _inspect_and_prepare(project, item_id=item_id, attempt=attempt, saved_path=saved_path,
                                    metadata_stripped=metadata_stripped, catalog=catalog, owned_sha256=owned_sha256)


def register_artifact(project, prepared):
    with project_operation(project):
        return submit_write(project, lambda db: commands.complete_item(db, prepared.artifact, prepared.originals)).result()
