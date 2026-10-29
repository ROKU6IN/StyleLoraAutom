from fastapi import Request

from ..backends.nai_direct.service import TokenInput
from .contracts import Empty


def install_routes(app, runtime, dispatch, accepted):
    from .app import json_response, match, query
    service = runtime.nai_direct

    @app.get('/api/nai-direct')
    async def state(request: Request):
        query(request, ())
        return json_response(await dispatch(request, service.view))

    @app.put('/api/nai-direct/token')
    async def token(value: TokenInput, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.save_token(value.value, match(request))))

    @app.delete('/api/nai-direct/token')
    async def remove_token(request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.save_token(None, match(request))))

    @app.post('/api/nai-direct/account/probe')
    async def probe(value: Empty, request: Request):
        query(request, ())
        return accepted(request, await dispatch(request, service.probe, {}, keyed=True))
