from fastapi import Request
from starlette.responses import Response

from ..runtime.contracts import uuid_string
from .contracts import TaggerInstall


def install_routes(app, runtime, dispatch):
    from .app import json_response, match, paging, query

    @app.get('/api/tagger/capabilities')
    async def capabilities(request: Request):
        query(request, ())
        return json_response(await dispatch(request, runtime.tagger.capabilities))

    @app.get('/api/tagger/models')
    async def models(request: Request):
        options = paging(request)
        return json_response(await dispatch(request, lambda: runtime.tagger.models(**options)))

    @app.post('/api/tagger/models/install')
    async def install(value: TaggerInstall, request: Request):
        query(request, ())
        body = value.model_dump()
        operation = await dispatch(request, lambda: runtime.tagger.install(value.manifest_id), body, keyed=True)
        oid = operation['installation_id']
        url = f'/api/tagger/installations/{oid}'
        return json_response({'installation_id': oid, 'status_url': url,
            'task': {'kind': 'operation', 'id': oid, 'project_id': None},
            'operation_url': f'/api/operations/{oid}', 'events_url': '/api/events',
            'request_id': request.scope['request_id']}, 202, {'Location': url, 'Retry-After': '1'})

    @app.get('/api/tagger/installations/{installation_id}')
    async def installation(installation_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: runtime.tagger.get(uuid_string(installation_id))))

    @app.delete('/api/tagger/installations/{installation_id}')
    async def cancel(installation_id: str, request: Request):
        query(request, ())
        await dispatch(request, lambda: runtime.tagger.cancel(uuid_string(installation_id), match(request)))
        return Response(status_code=204)
