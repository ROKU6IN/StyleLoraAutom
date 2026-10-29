from fastapi import Request
from starlette.responses import Response

from ..generation.presets import PresetInput
from ..runtime.errors import ServiceError


def install_routes(app, runtime, dispatch):
    from .app import json_response, match, paging, query
    store = runtime.presets

    @app.get('/api/presets')
    async def presets(request: Request):
        params = paging(request)
        return json_response(await dispatch(request, lambda: store.list(**params)))

    @app.get('/api/presets/{identifier}')
    async def preset(identifier: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: store.get(identifier)))

    @app.post('/api/presets')
    async def create_preset(value: PresetInput, request: Request):
        query(request, ())
        body = value.model_dump()
        return json_response(await dispatch(request, lambda: store.create(body), body, keyed=True), 201)

    @app.put('/api/presets/{identifier}')
    async def update_preset(identifier: str, value: PresetInput, request: Request):
        query(request, ())
        body, expected = value.model_dump(), match(request)
        return json_response(await dispatch(request, lambda: store.update(identifier, body, expected),
                                            {'body': body, 'version': expected}, keyed=True))

    @app.delete('/api/presets/{identifier}')
    async def delete_preset(identifier: str, request: Request):
        query(request, ())
        if await request.body():
            raise ServiceError(422, 'input:invalid')
        expected = match(request)
        await dispatch(request, lambda: store.delete(identifier, expected), {'version': expected}, keyed=True)
        return Response(status_code=204)
