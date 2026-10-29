from typing import Literal
from fastapi import Request
from pydantic import Field
from .contracts import StrictModel, Empty, Identifier, Digest


class CleanupPreview(StrictModel):
    kind: Literal['thumbnails', 'exports', 'rejected_files']


class CleanupConfirmation(StrictModel):
    preview_id: Identifier
    digest: Digest
    confirm: bool


class KnownScan(StrictModel):
    known_ids: list[Identifier] | None = Field(default=None, min_length=1, max_length=200)


def install_routes(app, runtime, dispatch, accepted):
    from .app import query, json_response, paging, match
    from ..services.product_config import ProductPatch
    from ..services.storage_alerts import summary as storage_summary
    from ..runtime.contracts import uuid_string
    from ..runtime.errors import ServiceError
    P = '/api/projects/{project_id}/storage'

    @app.get('/api/config')
    async def config(request: Request):
        query(request, ())
        return json_response(await dispatch(request, runtime.product_config.get))

    @app.patch('/api/config')
    async def update_config(value: ProductPatch, request: Request):
        query(request, ())
        body = value.model_dump(exclude_unset=True)
        return json_response(await dispatch(request, lambda: runtime.product_config.update(body, match(request)), body, keyed=True))

    @app.get('/api/storage/summary')
    async def product_summary(request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: storage_summary(runtime)))

    @app.post('/api/storage/scans')
    async def known_scan(value: KnownScan, request: Request):
        query(request, ())
        return accepted(request, await dispatch(request, lambda: runtime.known_storage.start(value.known_ids), value.model_dump(), keyed=True))

    @app.get('/api/storage/scans/{scan_id}')
    async def known_results(scan_id: str, request: Request):
        options = paging(request, ('sort',))
        if len(request.query_params.getlist('sort')) > 1:
            raise ServiceError(422, 'input:invalid')
        return json_response(await dispatch(request, lambda: runtime.known_storage.get(uuid_string(scan_id),
                             **options, sort=request.query_params.get('sort', '-total_bytes,known_id'))))

    @app.get(P+'/summary')
    async def summary(project_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, runtime.storage.summary, project_id=project_id))

    @app.post(P+'/scans')
    async def scan(project_id: str, value: Empty, request: Request):
        query(request, ())
        return accepted(request, await dispatch(request, runtime.storage.scan, project_id=project_id, keyed=True))

    @app.post(P+'/cleanup/preview')
    async def preview(project_id: str, value: CleanupPreview, request: Request):
        query(request, ())
        return accepted(request, await dispatch(request, lambda ctx: runtime.storage.preview(ctx, value.kind),
                                               value.model_dump(), project_id=project_id, keyed=True))

    @app.post(P+'/cleanup')
    async def cleanup(project_id: str, value: CleanupConfirmation, request: Request):
        query(request, ())
        return accepted(request, await dispatch(request, lambda ctx: runtime.storage.cleanup(ctx, value.model_dump()),
                                               value.model_dump(), project_id=project_id, keyed=True))
