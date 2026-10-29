from fastapi import Request
from starlette.responses import Response

from ..runtime.contracts import uuid_string
from .contracts import DuplicateScanInput


def install_routes(app, runtime, dispatch):
    from .app import json_response, match, paging, query
    P = '/api/projects/{project_id}/duplicates/scans'

    @app.get(P)
    async def scans(project_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.duplicates.list(ctx), project_id=project_id))

    @app.post(P)
    async def start(project_id: str, value: DuplicateScanInput, request: Request):
        query(request, ())
        body = value.model_dump()
        result = await dispatch(request, lambda ctx: runtime.duplicates.start(ctx, body), body, project_id=project_id, keyed=True)
        operation = result['operation']
        return json_response({'scan_id': result['scan_id'], 'status_url': result['status_url'],
            'task': {'kind': 'operation', 'id': operation['operation_id'], 'project_id': project_id},
            'operation_url': f"/api/operations/{operation['operation_id']}", 'events_url': '/api/events',
            'request_id': request.scope['request_id']}, 202, {'Location': result['status_url'], 'Retry-After': '1'})

    @app.get(P + '/{scan_id}')
    async def page(project_id: str, scan_id: str, request: Request):
        options = paging(request)
        return json_response(await dispatch(request, lambda ctx: runtime.duplicates.page(ctx, uuid_string(scan_id), **options), project_id=project_id))

    @app.delete(P + '/{scan_id}')
    async def delete(project_id: str, scan_id: str, request: Request):
        query(request, ())
        await dispatch(request, lambda ctx: runtime.duplicates.delete(ctx, uuid_string(scan_id), match(request)), project_id=project_id)
        return Response(status_code=204)
