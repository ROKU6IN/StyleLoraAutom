from fastapi import Request

from ..generation.types import Draft, HandoffPreview, HandoffStart, Projection, CostPolicyPatch, CostConfirmation, RecoveryAction, ManualStopConfirmation
from .prompt_production import key
from .contracts import Empty


def install_routes(app, runtime, dispatch, accepted):
    from .app import json_response, match, query, paging
    service = runtime.generation_sources
    P = '/api/projects/{project_id}/sources'

    @app.get(P + '/{source_id}/unknown-disposition')
    async def unknown_disposition(project_id: str, source_id: int, request: Request):
        from ..generation import manual_review
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: manual_review.view(runtime, ctx, source_id), project_id=project_id))

    @app.post(P + '/{source_id}/unknown-disposition')
    async def stop_unknown_source(project_id: str, source_id: int, value: ManualStopConfirmation, request: Request):
        from ..generation import manual_review
        query(request, ())
        expected = match(request)
        return json_response(await dispatch(request, lambda ctx: manual_review.stop(runtime, ctx, source_id, expected),
            {**value.model_dump(), 'expected': expected}, project_id=project_id, keyed=True))

    @app.get(P + '/{source_id}/launcher-check')
    async def launcher_check_status(project_id: str, source_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.launcher_checks.view(ctx, source_id), project_id=project_id))

    @app.post(P + '/{source_id}/launcher-check')
    async def launcher_check(project_id: str, source_id: int, value: Empty, request: Request):
        query(request, ())
        expected = match(request)
        operation = await dispatch(request, lambda ctx: runtime.launcher_checks.start(ctx, source_id, expected),
                                   {'expected': expected}, project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.get(P + '/{source_id}/recovery')
    async def recovery_items(project_id: str, source_id: int, request: Request):
        page = paging(request, ('status',))
        if len(request.query_params.getlist('status')) > 1:
            from ..runtime.errors import ServiceError
            raise ServiceError(422, 'input:invalid')
        return json_response(await dispatch(request, lambda ctx: runtime.generation_recovery.list(
            ctx, source_id, **page, status=request.query_params.get('status')), project_id=project_id))

    @app.get(P + '/{source_id}/recovery/{item_id}')
    async def recovery_item(project_id: str, source_id: int, item_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.generation_recovery.view(ctx, source_id, item_id), project_id=project_id))

    @app.post(P + '/{source_id}/recovery')
    async def recover(project_id: str, source_id: int, value: RecoveryAction, request: Request):
        query(request, ())
        operation = await dispatch(request, lambda ctx: runtime.generation_recovery.start(ctx, source_id, value.model_dump(), match(request)),
                                   value.model_dump(), project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.get(P + '/{source_id}/cost-state')
    async def cost_state(project_id: str, source_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.generation_costs.state(ctx, source_id), project_id=project_id))

    @app.patch(P + '/{source_id}')
    async def cost_policy(project_id: str, source_id: int, value: CostPolicyPatch, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.generation_costs.set_policy(ctx, source_id, value.model_dump(), match(request)),
            value.model_dump(), project_id=project_id, keyed=True))

    @app.post(P + '/{source_id}/confirm-cost')
    async def confirm_cost(project_id: str, source_id: int, value: CostConfirmation, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.generation_costs.confirm(ctx, source_id, value.model_dump(), match(request)),
            value.model_dump(), project_id=project_id, keyed=True))

    @app.get('/api/generation/capabilities')
    async def capabilities(request: Request):
        query(request, ())
        return json_response(service.capabilities())

    @app.post(P)
    async def create(project_id: str, value: Draft, request: Request):
        query(request, ())
        body = value.model_dump(exclude_unset=True)
        return json_response(await dispatch(request, lambda ctx: service.create(ctx, body),
            body, project_id=project_id, keyed=True), 201)

    @app.put(P + '/{source_id}')
    async def update(project_id: str, source_id: int, value: Draft, request: Request):
        query(request, ())
        expected = match(request)
        return json_response(await dispatch(request, lambda ctx: service.update(ctx, source_id, value.model_dump(), expected),
            {'draft': value.model_dump(), 'expected': expected}, project_id=project_id,
            keyed='idempotency-key' in request.headers))

    @app.post(P + '/{source_id}/handoff-preview')
    async def preview(project_id: str, source_id: int, value: HandoffPreview, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: service.preview(ctx, source_id, value.model_dump(), match(request)), project_id=project_id))

    @app.post(P + '/{source_id}/start')
    async def start(project_id: str, source_id: int, value: HandoffStart, request: Request):
        query(request, ())
        key(request)
        return json_response(await dispatch(request, lambda ctx: service.start(ctx, source_id, value.model_dump(), match(request)), project_id=project_id))

    @app.post(P + '/{source_id}/reconcile-start')
    async def reconcile(project_id: str, source_id: int, value: Projection, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: service.reconcile(ctx, source_id, value.model_dump()), project_id=project_id))
