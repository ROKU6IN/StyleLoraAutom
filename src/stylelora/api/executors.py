from fastapi import Request

from ..runtime.contracts import uuid_string
from ..runtime.errors import ServiceError
from .contracts import JobBatch, JobPreview, JobControl, JobRetry, Reclassify, ManualRating


def install_routes(app, runtime, dispatch):
    from .app import json_response, match, paging, query, positive
    P = '/api/projects/{project_id}/jobs'

    @app.patch('/api/projects/{project_id}/images/{image_id}')
    async def rating(project_id: str, image_id: int, value: ManualRating, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.manual_rating(ctx, positive(str(image_id)), value.rating, match(request)), project_id=project_id))

    @app.get('/api/executors/capabilities')
    async def capabilities(request: Request):
        query(request, ())
        return json_response({'wd': {'available': runtime.tagger.can_execute(),
            'default_enabled': runtime.product_config.get()['wd_enabled']}, 'llm_vision': runtime.vision.capabilities()})

    @app.post('/api/projects/{project_id}/ratings/reclassify')
    async def reclassify(project_id: str, value: Reclassify, request: Request):
        query(request, ())
        body = value.model_dump()
        operation = await dispatch(request, lambda ctx: runtime.vision.reclassify(ctx, body), body, project_id=project_id, keyed=True)
        url = '/api/operations/' + operation['operation_id']
        return json_response({'task': {'kind': 'operation', 'id': operation['operation_id'], 'project_id': project_id},
            'status_url': url, 'events_url': '/api/events'}, 202, {'Location': url})

    @app.post(P + '/batches')
    async def create(project_id: str, value: JobBatch, request: Request):
        query(request, ())
        body = value.model_dump()
        values = await dispatch(request, lambda ctx: runtime.jobs.create(ctx, body), body, project_id=project_id, keyed=True)
        return json_response({'items': values, 'batch_id': value.batch_id, 'events_url': '/api/events',
            'batch_url': f'/api/projects/{project_id}/jobs/batches/{value.batch_id}',
            'status_url': values[0]['status_url'], 'task': values[0]['task'], 'request_id': request.scope['request_id']}, 202,
            {'Location': values[0]['status_url'], 'Retry-After': '1'})

    @app.post(P + '/preview')
    async def preview(project_id: str, value: JobPreview, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.batches.preview(ctx, value.model_dump()), project_id=project_id))

    @app.get(P + '/batches')
    async def batches(project_id: str, request: Request):
        options = paging(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.batches.list(ctx, **options), project_id=project_id))

    @app.get(P + '/batches/{batch_id}')
    async def batch(project_id: str, batch_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.batches.get(ctx, uuid_string(batch_id)), project_id=project_id))

    @app.post(P + '/batches/{batch_id}/control')
    async def batch_control(project_id: str, batch_id: str, value: JobControl, request: Request):
        query(request, ())
        body = value.model_dump()
        return json_response(await dispatch(request, lambda ctx: runtime.batches.control(ctx, uuid_string(batch_id), value.action, match(request)),
            {**body, 'expected': match(request)}, project_id=project_id, keyed=True))

    @app.get(P)
    async def listing(project_id: str, request: Request):
        options = paging(request, ('status', 'batch_id', 'executor'))
        for name, choices in (('status', ('pending', 'running', 'paused', 'done', 'failed', 'canceled')),
                              ('executor', ('wd', 'llm_vision'))):
            value = request.query_params.get(name)
            if len(request.query_params.getlist(name)) > 1 or value is not None and value not in choices:
                raise ServiceError(422, 'input:invalid')
            options[name] = value
        if len(request.query_params.getlist('batch_id')) > 1:
            raise ServiceError(422, 'input:invalid')
        batch = request.query_params.get('batch_id')
        options['batch_id'] = uuid_string(batch) if batch else None
        return json_response(await dispatch(request, lambda ctx: runtime.jobs.list(ctx, **options), project_id=project_id))

    @app.get(P + '/{job_id}')
    async def detail(project_id: str, job_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.jobs.get(ctx, positive(str(job_id))), project_id=project_id))

    @app.get(P + '/{job_id}/outcomes')
    async def outcomes(project_id: str, job_id: int, request: Request):
        options = paging(request, ('kind',))
        kind = request.query_params.get('kind')
        if len(request.query_params.getlist('kind')) > 1 or kind is not None and kind not in ('done', 'failed', 'skipped'):
            raise ServiceError(422, 'input:invalid')
        return json_response(await dispatch(request, lambda ctx: runtime.jobs.outcomes(ctx, positive(str(job_id)), kind=kind, **options), project_id=project_id))

    @app.get(P + '/{job_id}/retry-preview')
    async def retry_preview(project_id: str, job_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.batches.retry_preview(ctx, positive(str(job_id))), project_id=project_id))

    @app.post(P + '/{job_id}/retry')
    async def retry(project_id: str, job_id: int, value: JobRetry, request: Request):
        query(request, ())
        body = value.model_dump()
        values = await dispatch(request, lambda ctx: runtime.batches.retry(ctx, positive(str(job_id)), body), body, project_id=project_id, keyed=True)
        url = f'/api/projects/{project_id}/jobs/batches/{value.batch_id}'
        return json_response({'items': values, 'batch_id': value.batch_id, 'batch_url': url,
            'events_url': '/api/events', 'status_url': values[0]['status_url'], 'task': values[0]['task']}, 202, {'Location': url})

    @app.post(P + '/{job_id}/control')
    async def control(project_id: str, job_id: int, value: JobControl, request: Request):
        query(request, ())
        body = value.model_dump()
        return json_response(await dispatch(request, lambda ctx: runtime.jobs.control(ctx, positive(str(job_id)), value.action, match(request)),
            {**body, 'expected': match(request)}, project_id=project_id, keyed=True))
