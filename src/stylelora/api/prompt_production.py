import hashlib

from fastapi import Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import Response

from ..prompt_production import exchange
from ..prompt_production import coverage as coverage_templates
from ..prompt_production import coverage_editor
from ..prompt_production.review import ReviewPreview, ReviewCommit
from ..prompt_production.types import AppendCommit, AppendSelection, Control, DictionaryCreate, DictionaryInput, Freeze, ImportCommit, SamplingRequest, TaskCreate
from ..prompt_production.types import Cleanup, ResultsExport, ResultsPreview, ImportSelection
from ..project_db.validation import digest
from ..runtime.contracts import uuid_string, Versions
from ..runtime.errors import ServiceError


def key(request):
    values = request.headers.getlist('idempotency-key')
    if len(values) != 1:
        raise ServiceError(422, 'idempotency:invalid_key')
    return uuid_string(values[0])


def install_routes(app, runtime, dispatch):
    from .app import json_response, match, paging, query
    service = runtime.prompts
    P = '/api/prompt-production'

    def page(request, values, identity, extra=()):
        params = paging(request, extra)
        limit = params['limit'] if 'limit' in request.query_params else 200
        version = digest(values)
        cursor = runtime.cursors.decode(params['cursor'], identity) if params['cursor'] else None
        if cursor and cursor['version'] != version:
            raise ServiceError(409, 'page:stale')
        start = cursor['offset'] if cursor else 0
        more = start + limit < len(values)
        return {'items': values[start:start + limit], 'total': len(values), 'limit': limit, 'has_more': more,
                'version': version, 'next_cursor': runtime.cursors.encode({'identity': identity, 'version': version, 'offset': start + limit}) if more else None}

    @app.get(P + '/capabilities')
    async def capabilities(request: Request):
        query(request, ())
        return json_response(service.capabilities())

    @app.get(P + '/launcher/capabilities')
    async def launcher(request: Request):
        query(request, ())
        return json_response({'available': False, 'reason': 'launcher:unsupported_schema'})

    @app.get(P + '/dictionaries')
    async def dictionaries(request: Request):
        def read():
            kind = request.query_params.get('kind')
            if kind not in (None, 'features', 'prompts'):
                raise ServiceError(422, 'schema:invalid')
            values = [row for row in service.dictionaries.list() if kind is None or row['kind'] == kind]
            return page(request, values, 'prompt-dictionaries:' + str(kind), ('kind',))
        return json_response(await dispatch(request, read))

    @app.get(P + '/dictionaries/deletions')
    async def pending_dictionary_deletions(request: Request):
        return json_response(await dispatch(request, lambda: page(request, service.dictionaries.deletions.pending(), 'dictionary-deletions')))

    @app.delete(P + '/dictionaries/{identifier}')
    async def delete_dictionary(identifier: str, request: Request):
        query(request, ())
        if await request.body():
            raise ServiceError(422, 'input:invalid')
        return json_response(await dispatch(request, lambda: service.dictionaries.deletions.delete(identifier, match(request), key(request))))

    @app.get(P + '/dictionaries/{identifier}')
    async def dictionary(identifier: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.dictionaries.get(identifier)))

    @app.post(P + '/dictionaries')
    async def create_dictionary(value: DictionaryCreate, request: Request):
        body = value.model_dump()
        identifier = body.pop('dictionary_id')
        return json_response(await dispatch(request, lambda: service.dictionaries.save(identifier, body, None, key(request), create=True)), 201)

    @app.put(P + '/dictionaries/{identifier}')
    async def update_dictionary(identifier: str, value: DictionaryInput, request: Request):
        return json_response(await dispatch(request, lambda: service.dictionaries.save(identifier, value.model_dump(), match(request), key(request))))

    @app.get(P + '/dictionaries/{identifier}/export')
    async def export_dictionary(identifier: str, request: Request):
        query(request, ('format', 'acknowledge_lossy'))
        format = request.query_params.get('format', 'json')
        ack = request.query_params.get('acknowledge_lossy', 'false')
        if ack not in ('true', 'false'):
            raise ServiceError(422, 'schema:invalid')
        def export():
            value = service.dictionaries.get(identifier)
            if format == 'txt':
                if ack != 'true':
                    raise ServiceError(422, 'prompt:lossy_ack_required')
                Versions.require(match(request), value['version'])
            elif 'if-match' in request.headers:
                Versions.require(match(request), value['version'])
            return exchange.export(value, format)
        data, media, losses = await dispatch(request, export)
        extension = 'zip' if media == 'application/zip' else format
        return Response(data, media_type=media, headers={'Content-Disposition': f'attachment; filename="dictionary-{uuid_string(identifier)}.{extension}"',
                        'X-Prompt-Lossy-Fields': ','.join(losses)})

    @app.post(P + '/imports/preview')
    async def import_preview(request: Request):
        key(request)
        class BoundedParser(MultiPartParser):
            spool_max_size = 11 * 1024 * 1024
        try:
            form = await BoundedParser(request.headers, request.stream(), max_files=1, max_fields=3, max_part_size=exchange.MAX_IMPORT).parse()
        except MultiPartException:
            raise ServiceError(422, 'prompt:invalid_upload') from None
        try:
            if set(form) - {'file', 'format', 'kind', 'dimension'} or len(form.multi_items()) != len(form):
                raise ServiceError(422, 'prompt:invalid_upload')
            file = form.get('file')
            if not isinstance(file, UploadFile):
                raise ServiceError(422, 'prompt:invalid_upload')
            data = await file.read(exchange.MAX_IMPORT + 1)
            format, kind, dimension = form.get('format', 'json'), form.get('kind'), form.get('dimension')
            if any(value is not None and not isinstance(value, str) for value in (format, kind, dimension)):
                raise ServiceError(422, 'prompt:invalid_upload')
            def preview():
                payload, report = exchange.parse(data, format, kind, dimension, runtime.catalog.snapshot(), with_rows=True)
                return runtime.previews.add(None, 'prompt-import', report, payload)
            signature = {'format': format, 'kind': kind, 'dimension': dimension, 'digest': hashlib.sha256(data).hexdigest()}
            return json_response(await dispatch(request, preview, signature, keyed=True))
        finally:
            await form.close()

    @app.get(P + '/imports/{identifier}/rows')
    async def import_rows(identifier: str, request: Request):
        def read():
            supplied = request.query_params.get('digest', '')
            if len(supplied) != 64 or any(c not in '0123456789abcdef' for c in supplied):
                raise ServiceError(422, 'input:invalid')
            report, private = runtime.previews.get(None, 'prompt-import', uuid_string(identifier), supplied)
            return page(request, private['rows'], 'prompt-import:' + identifier + ':' + report['digest'], ('digest',))
        return json_response(await dispatch(request, read))

    @app.post(P + '/imports/select')
    async def select_import(value: ImportSelection, request: Request):
        query(request, ())
        def preview():
            report, private = runtime.previews.get(None, 'prompt-import', value.preview_id, value.digest)
            payload, result = exchange.select_rows(report, private, value.rows)
            return runtime.previews.add(None, 'prompt-import', result, payload)
        return json_response(await dispatch(request, preview, value.model_dump(), keyed=True))

    @app.post(P + '/dictionaries/{identifier}/imports')
    async def commit_import(identifier: str, value: ImportCommit, request: Request):
        def commit():
            signature = digest(['prompt-import', identifier, value.preview_id, value.digest])
            previous = service.dictionaries.replay(identifier, key(request), signature)
            if previous is not None:
                return previous
            report, private = runtime.previews.get(None, 'prompt-import', value.preview_id, value.digest)
            if not report['valid']:
                raise ServiceError(422, 'prompt:import_has_errors')
            # Parsed contents and aliases are already frozen in the preview.
            return service.dictionaries.save(identifier, private['payload'], match(request), key(request), append=True, signature=signature)
        return json_response(await dispatch(request, commit))

    @app.get(P + '/coverage-plans')
    async def coverage_plans(request: Request):
        return json_response(await dispatch(request, lambda: page(request, service.coverage_plans.list(), 'coverage-plans')))

    @app.post(P + '/coverage-plans/preview')
    async def preview_template(value: coverage_templates.Document, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: coverage_templates.view(coverage_templates.parse(value.model_dump()))))

    @app.post(P + '/coverage-plans/editor')
    async def open_coverage_editor(value: coverage_templates.Document, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: coverage_editor.open_editor(value.model_dump())))

    @app.post(P + '/coverage-plans/editor/preview')
    async def preview_coverage_editor(value: coverage_editor.EditorTemplate, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: coverage_editor.preview_editor(value.model_dump())))

    @app.post(P + '/coverage-plans')
    async def create_coverage(value: coverage_templates.Create, request: Request):
        query(request, ())
        body = value.model_dump()
        identifier = body.pop('coverage_id')
        return json_response(await dispatch(request, lambda: service.coverage_plans.save(identifier, body, None, key(request), create=True)), 201)

    @app.get(P + '/coverage-plans/deletions')
    async def pending_coverage_deletions(request: Request):
        return json_response(await dispatch(request, lambda: page(request, service.coverage_plans.deletions.pending(), 'coverage-deletions')))

    @app.delete(P + '/coverage-plans/{identifier}')
    async def delete_coverage(identifier: str, request: Request):
        query(request, ())
        if await request.body():
            raise ServiceError(422, 'input:invalid')
        return json_response(await dispatch(request, lambda: service.coverage_plans.deletions.delete(identifier, match(request), key(request))))

    @app.get(P + '/coverage-plans/{identifier}')
    async def get_coverage(identifier: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.coverage_plans.get(identifier)))

    @app.put(P + '/coverage-plans/{identifier}')
    async def update_coverage(identifier: str, value: coverage_templates.Document, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.coverage_plans.save(identifier, value.model_dump(), match(request), key(request))))

    @app.get(P + '/coverage-plans/{identifier}/export')
    async def export_coverage(identifier: str, request: Request):
        query(request, ('format',))
        return json_response(await dispatch(request, lambda: service.coverage_plans.export(identifier, request.query_params.get('format', 'json'))))

    @app.post(P + '/coverage/preview')
    async def coverage(value: SamplingRequest, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.preview(value.model_dump())))

    def detail_query(request, *, task=False, release=False):
        names = ('expected_revision', 'cell') if task else ('digest',) if release else ('digest', 'cell')
        query(request, (*names, *(() if release else ('limit', 'cursor'))))
        if any(len(request.query_params.getlist(name)) != 1 for name in names):
            raise ServiceError(422, 'input:invalid')
        values = {name: request.query_params[name] for name in names}
        if task:
            raw = values['expected_revision']
            if not raw.isascii() or not raw.isdigit() or len(raw) > 16 or int(raw) < 1:
                raise ServiceError(422, 'input:invalid')
            values['expected_revision'] = int(raw)
        elif len(values['digest']) != 64 or any(c not in '0123456789abcdef' for c in values['digest']):
            raise ServiceError(422, 'input:invalid')
        if 'cell' in values and len(values['cell']) > 4096:
            raise ServiceError(422, 'coverage:invalid_cell')
        return values

    @app.get(P + '/coverage/previews/{identifier}/entries')
    async def preview_cell_entries(identifier: str, request: Request):
        params = detail_query(request)
        def read():
            rows = service.coverage_views.get(identifier, params['digest'], params['cell'])
            identity = 'coverage-preview:' + digest([identifier, params])
            return page(request, rows, identity, ('digest', 'cell'))
        return json_response(await dispatch(request, read))

    @app.delete(P + '/coverage/previews/{identifier}')
    async def release_coverage(identifier: str, request: Request):
        params = detail_query(request, release=True)
        return json_response(await dispatch(request, lambda: service.coverage_views.release(identifier, params['digest'])))

    @app.get(P + '/tasks/{identifier}/coverage/entries')
    async def task_cell_entries(identifier: str, request: Request):
        params = detail_query(request, task=True)
        def read():
            rows = service.matrix_entries(identifier, params['expected_revision'], params['cell'])
            identity = 'coverage-task:' + digest([identifier, params])
            return page(request, rows, identity, ('expected_revision', 'cell'))
        return json_response(await dispatch(request, read))

    @app.post(P + '/tasks')
    async def create_task(value: TaskCreate, request: Request):
        result = await dispatch(request, lambda: service.create(value.model_dump(), key(request)))
        url = P + '/tasks/' + result['task_id']
        return json_response({**result, 'status_url': url, 'events_url': '/api/events?topics=prompt&resource_id=' + result['task_id']}, 202,
                             {'Location': url, 'Retry-After': '1'})

    @app.get(P + '/tasks')
    async def tasks(request: Request):
        return json_response(await dispatch(request, lambda: page(request, service.list(), 'prompt-tasks')))

    @app.get(P + '/tasks/{identifier}')
    async def task(identifier: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.get(identifier)))

    @app.get(P + '/task-storage')
    async def storage(request: Request):
        query(request, ())
        return json_response(await dispatch(request, service.storage_inventory))

    @app.post(P + '/task-cleanup')
    async def cleanup(value: Cleanup, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.cleanup(value.model_dump(), key(request))))

    @app.post(P + '/tasks/{identifier}/results-preview')
    async def results_preview(identifier: str, value: ResultsPreview, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.results_preview(identifier, value.model_dump())))

    @app.post(P + '/tasks/{identifier}/results-export')
    async def results_export(identifier: str, value: ResultsExport, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.results_export(identifier, value.model_dump())))

    @app.get(P + '/tasks/{identifier}/reviews/{index}')
    async def review_detail(identifier: str, index: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.review_detail(identifier, index)))

    @app.post(P + '/tasks/{identifier}/reviews/preview')
    async def review_preview(identifier: str, value: ReviewPreview, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.review_preview(identifier, value.model_dump())))

    @app.post(P + '/tasks/{identifier}/reviews')
    async def review_commit(identifier: str, value: ReviewCommit, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda: service.review_commit(identifier, value.model_dump(), key(request))))

    @app.get(P + '/tasks/{identifier}/outcomes')
    async def outcomes(identifier: str, request: Request):
        return json_response(await dispatch(request, lambda: page(request, service.outcomes(identifier), 'prompt-outcomes:' + identifier)))

    @app.post(P + '/tasks/{identifier}/control')
    async def control(identifier: str, value: Control, request: Request):
        return json_response(await dispatch(request, lambda: service.control(identifier, value.model_dump()), value.model_dump(), keyed=True))

    @app.post(P + '/tasks/{identifier}/seeds')
    async def seeds(identifier: str, value: Freeze, request: Request):
        return json_response(await dispatch(request, lambda: service.seeds(identifier, value.expected_revision)))

    @app.post(P + '/tasks/{identifier}/append-preview')
    async def append_preview(identifier: str, value: AppendSelection, request: Request):
        return json_response(await dispatch(request, lambda: service.append_preview(identifier, value.model_dump())))

    @app.post(P + '/tasks/{identifier}/append')
    async def append(identifier: str, value: AppendCommit, request: Request):
        key(request)
        return json_response(await dispatch(request, lambda: service.append(identifier, value.model_dump())))
