from fastapi import Request

from .contracts import Empty
from ..services.export_recovery import RecoveryAction


def install_routes(app, runtime, dispatch, accepted):
    from .app import match, query, json_response
    base = '/api/projects/{project_id}/exports/recovery'

    @app.post(base+'/scan')
    async def scan(project_id: str, value: Empty, request: Request):
        query(request, ())
        operation = await dispatch(request, lambda ctx: runtime.export_recovery.scan(ctx), {},
                                   project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.post(base+'/actions')
    async def act(project_id: str, value: RecoveryAction, request: Request):
        query(request, ())
        body, expected = value.model_dump(), match(request)
        operation = await dispatch(request, lambda ctx: runtime.export_recovery.act(ctx, body, expected),
                                   {'body': body, 'expected': expected}, project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.get(base+'/history')
    async def history(project_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: {'items': runtime.export_recovery.history(ctx)}, project_id=project_id))
