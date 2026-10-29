from fastapi import Request

from ..runtime.errors import ServiceError
from .contracts import Empty


def install_routes(app, runtime, dispatch, accepted):
    from .app import json_response, query, positive

    @app.get('/api/backends')
    async def backends(request: Request):
        query(request, ())
        return json_response(runtime.backends.list())

    @app.get('/api/backends/{backend_id}/capabilities')
    async def capabilities(backend_id: str, request: Request):
        query(request, ())
        return json_response(runtime.backends.capabilities(backend_id))

    @app.get('/api/backends/{backend_id}/settings-lease')
    async def settings_lease(backend_id: str, request: Request):
        query(request, ())
        runtime.backends.require(backend_id)
        return json_response(await dispatch(request, runtime.settings_leases.status))

    @app.post('/api/backends/{backend_id}/probe')
    async def probe(backend_id: str, value: Empty, request: Request):
        query(request, ())
        operation = await dispatch(request, lambda: runtime.backends.probe(backend_id), {}, keyed=True)
        return accepted(request, operation)

    @app.post('/api/backends/{backend_id}/inspect')
    async def inspect(backend_id: str, value: Empty, request: Request):
        query(request, ())
        operation = await dispatch(request, lambda: runtime.backends.inspect(backend_id), {}, keyed=True)
        return accepted(request, operation)

    @app.get('/api/backends/{backend_id}/account-status')
    async def account_status(backend_id: str, request: Request):
        query(request, ('project_id', 'source_id'))
        if backend_id != 'nai_direct':
            runtime.backends.require(backend_id)
        params = request.query_params
        if any(len(params.getlist(k)) > 1 for k in ('project_id', 'source_id')):
            raise ServiceError(422, 'input:invalid')
        pid, sid = params.get('project_id'), positive(params.get('source_id'))
        if sid is not None and not pid:
            raise ServiceError(422, 'input:invalid')
        if pid:
            def check(context):
                if sid is not None:
                    source = runtime.dataset.get_source(context, sid)
                    if source['type'] != 'nai' or source['config_json'].get('backend', 'nai_launcher') != backend_id:
                        raise ServiceError(422, 'source:backend_mismatch')
                    return runtime.generation_costs.summary(context, sid)
            result = await dispatch(request, check, project_id=pid)
            return json_response(result)
        # Without a project/source there is no scoped local estimate; no remote account query.
        return json_response(runtime.nai_direct.view()['account'] if backend_id == 'nai_direct' else None)
