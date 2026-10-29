from fastapi import Request
from starlette.responses import Response

from ..llm.types import EndpointInput, RolesInput, SecretInput, ImageTestInput
from ..runtime.contracts import uuid_string
from .contracts import Empty
from ..vision.policy import PolicyInput


def install_routes(app, runtime, dispatch):
    from .app import json_response, match, paging, query
    config = runtime.llm.config

    @app.get('/api/llm/vision-policy')
    async def policy(request: Request):
        query(request, ())
        return json_response(await dispatch(request, runtime.vision.policy.view))

    @app.put('/api/llm/vision-policy')
    async def update_policy(value: PolicyInput, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: runtime.vision.policy.save(value, match(request))))

    @app.get('/api/llm/templates')
    async def templates(request: Request):
        query(request, ())
        return json_response({'items': runtime.llm.templates.views()})

    @app.get('/api/llm/endpoints')
    async def endpoints(request: Request):
        options = paging(request)
        return json_response(await dispatch(request, lambda: config.list(**options)))

    @app.post('/api/llm/endpoints')
    async def create(value: EndpointInput, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: config.save(value), value.model_dump(), keyed=True), 201)

    @app.get('/api/llm/endpoints/{endpoint_id}')
    async def endpoint(endpoint_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: config.view(uuid_string(endpoint_id))))

    @app.put('/api/llm/endpoints/{endpoint_id}')
    async def update(endpoint_id: str, value: EndpointInput, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: config.save(value, uuid_string(endpoint_id), match(request))))

    @app.delete('/api/llm/endpoints/{endpoint_id}')
    async def delete(endpoint_id: str, request: Request):
        query(request, ())
        await dispatch(request, lambda: config.delete(uuid_string(endpoint_id), match(request)))
        return Response(status_code=204)

    @app.post('/api/llm/endpoints/{endpoint_id}/tokens')
    async def add_token(endpoint_id: str, value: SecretInput, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: config.token(uuid_string(endpoint_id), value.value),
            value.model_dump(), keyed=True), 201)

    @app.put('/api/llm/endpoints/{endpoint_id}/tokens/{token_ref}')
    async def update_token(endpoint_id: str, token_ref: str, value: SecretInput, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: config.token(uuid_string(endpoint_id), value.value,
            match(request), uuid_string(token_ref))))

    @app.delete('/api/llm/endpoints/{endpoint_id}/tokens/{token_ref}')
    async def delete_token(endpoint_id: str, token_ref: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: config.token(uuid_string(endpoint_id), None,
            match(request), uuid_string(token_ref))))

    @app.get('/api/llm/roles')
    async def roles(request: Request):
        query(request, ())
        return json_response(await dispatch(request, config.roles))

    @app.put('/api/llm/roles')
    async def save_roles(value: RolesInput, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: config.save_roles(value, match(request))))

    def accepted(operation):
        oid = operation['operation_id']
        url = '/api/operations/' + oid
        return json_response({'task': {'kind': 'operation', 'id': oid, 'project_id': None},
            'status_url': url, 'events_url': '/api/events'}, 202, {'Location': url, 'Retry-After': '1'})

    @app.post('/api/llm/endpoints/{endpoint_id}/models/probe')
    async def probe(endpoint_id: str, request: Request, value: Empty = Empty()):
        query(request, ())
        return accepted(await dispatch(request, lambda: runtime.llm.probe(uuid_string(endpoint_id), match(request)),
            {'version': match(request)}, keyed=True))

    @app.post('/api/llm/endpoints/{endpoint_id}/models/test-image')
    async def image_test(endpoint_id: str, value: ImageTestInput, request: Request):
        query(request, ())
        return accepted(await dispatch(request, lambda: runtime.llm.probe(uuid_string(endpoint_id), match(request), value.model),
            {**value.model_dump(), 'version': match(request)}, keyed=True))
