import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import queue

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from ..project_db.validation import primitive
from ..runtime.contracts import uuid_string
from ..runtime.errors import ServiceError
from . import contracts as C
from .security import SecurityMiddleware


def match(request):
    values = request.headers.getlist("if-match")
    if not values:
        return None
    value = values[0]
    if len(values) != 1 or len(value) < 3 or value[0] != '"' or value[-1] != '"' or '"' in value[1:-1]:
        raise ServiceError(422, "concurrency:invalid_precondition")
    return value[1:-1]


def json_response(value, status=200, headers=None):
    headers = dict(headers or {})
    if isinstance(value, dict) and value.get("version"):
        headers["ETag"] = '"' + value["version"] + '"'
    return JSONResponse(primitive(value), status_code=status, headers=headers)


def positive(value, default=None):
    if value is None:
        return default
    if not value.isdecimal() or int(value) < 1:
        raise ServiceError(422, "input:invalid")
    return int(value)


def query(request, allowed):
    if set(request.query_params) - set(allowed):
        raise ServiceError(422, "input:unknown_query")


def paging(request, extra=()):
    query(request, {"limit", "cursor", *extra})
    if len(request.query_params.getlist("limit")) > 1 or len(request.query_params.getlist("cursor")) > 1:
        raise ServiceError(422, "input:invalid")
    limit = positive(request.query_params.get("limit"), 50)
    if limit > 1000:
        raise ServiceError(422, "input:invalid")
    return {"limit": limit, "cursor": request.query_params.get("cursor")}


IMAGE_FILTERS = ("source_id", "review_status", "rating", "file_missing", "q")


def image_query(request):
    params = request.query_params
    statuses, ratings = params.getlist("review_status"), params.getlist("rating")
    if set(statuses)-{"keep", "reject", "unreviewed"} or set(ratings)-{"general", "sensitive", "questionable", "explicit"}:
        raise ServiceError(422, "input:invalid_filter")
    missing = params.get("file_missing")
    if missing not in (None, "true", "false") or len(params.get("q", "")) > 4096:
        raise ServiceError(422, "input:invalid_filter")
    return {"source_ids": [positive(value) for value in params.getlist("source_id")], "review_statuses": statuses,
            "rating": ratings, "file_missing": None if missing is None else missing == "true", "q": params.get("q")}


def create_app(runtime, *, manage_lifecycle=True):
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if manage_lifecycle:
                await asyncio.to_thread(runtime.shutdown)

    app = FastAPI(title="StyleLora local core API", version="1", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(SecurityMiddleware, runtime=runtime)

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return JSONResponse({"error": error.body(request.scope["request_id"])}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        value = ServiceError(422, "schema:invalid").body(request.scope["request_id"])
        value["issues"] = [{"code": issue["type"], "message": "Invalid field", "severity": "error",
                            "field": ".".join(map(str, issue["loc"])), "row": None} for issue in error.errors()[:50]]
        return JSONResponse({"error": value}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return await service_error(request, ServiceError(error.status_code, "resource:not_found" if error.status_code == 404 else "request:method_not_allowed"))

    def invoke(request, fn, body=None, *, project_id=None, keyed=False):
        def run(context=None):
            return fn(context) if project_id else fn()
        identity = request.method + " " + request.url.path
        def deduplicated(context=None):
            if keyed:
                keys = request.headers.getlist("idempotency-key")
                if len(keys) > 1:
                    raise ServiceError(422, "idempotency:invalid_key")
                return runtime.idempotency.run(identity, keys[0] if keys else None, body or {}, lambda: run(context))
            return run(context)
        if project_id:
            uuid_string(project_id)
            with runtime.lease(project_id, readonly=request.method in ("GET", "HEAD")) as context:
                return deduplicated(context)
        return deduplicated()

    async def dispatch(request, fn, body=None, *, project_id=None, keyed=False):
        return await runtime.call(invoke, request, fn, body, project_id=project_id, keyed=keyed)

    def accepted(request, operation):
        url = f"/api/operations/{operation['operation_id']}"
        value = {"task": {"kind": "operation", "id": operation["operation_id"], "project_id": operation["project_id"]},
                 "status_url": url, "events_url": "/api/events", "request_id": request.scope["request_id"]}
        return json_response(value, 202, {"Location": url, "Retry-After": "1"})

    @app.get("/", include_in_schema=False)
    async def landing():
        compiled = Path(__file__).parent / "web_dist/index.html"
        path = compiled if compiled.is_file() else Path(__file__).parent / "assets/index.html"
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/assets/session.js", include_in_schema=False)
    async def session_script():
        return Response((Path(__file__).parent / "assets/session.js").read_bytes(), media_type="application/javascript")

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    async def compiled_asset(asset_path: str):
        root = (Path(__file__).parent / "web_dist/assets").resolve()
        if "\\" in asset_path or any(part in ("", ".", "..") for part in asset_path.split("/")):
            raise ServiceError(404, "resource:not_found")
        path = (root / asset_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ServiceError(404, "resource:not_found")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/openapi.json")
    async def schema():
        value = app.openapi()
        value.setdefault("components", {}).setdefault("securitySchemes", {})["LocalBearer"] = {"type": "http", "scheme": "bearer"}
        value["security"] = [{"LocalBearer": []}]
        value["paths"]["/api/session/bootstrap"]["post"]["security"] = []
        return json_response(value)

    @app.get("/api/session")
    async def session(request: Request):
        query(request, ())
        return json_response(runtime.session())

    @app.post("/api/session/bootstrap")
    async def bootstrap(value: C.Bootstrap, request: Request):
        return json_response(runtime.redeem(value.code, request.headers.get("origin")))

    @app.post("/api/session/shutdown")
    async def shutdown(value: C.Shutdown, request: Request):
        operation = await dispatch(request, lambda: runtime.request_shutdown(match(request), value.grace_seconds), value.model_dump(), keyed=True)
        return accepted(request, operation)

    @app.post("/api/filesystem/grants")
    async def picker(value: C.PickerRequest, request: Request):
        operation = await dispatch(request, lambda: runtime.picker.select(value.purpose, value.project_id), value.model_dump(), keyed=True)
        return accepted(request, operation)

    @app.delete("/api/filesystem/grants/{grant_id}")
    async def revoke(grant_id: str, request: Request):
        await dispatch(request, lambda: runtime.grants.revoke(uuid_string(grant_id), match(request)))
        return Response(status_code=204)

    @app.get("/api/operations")
    async def operation_list(request: Request):
        page = paging(request, ("project_id",))
        pid = request.query_params.get("project_id")
        return json_response(runtime.supervisor.list(uuid_string(pid) if pid else None, **page))

    @app.get("/api/operations/{operation_id}")
    async def operation(operation_id: str, request: Request):
        query(request, ())
        return json_response(runtime.supervisor.get(uuid_string(operation_id)))

    @app.post("/api/operations/{operation_id}/cancel")
    async def cancel_operation(operation_id: str, value: C.Empty, request: Request):
        result = await dispatch(request, lambda: runtime.supervisor.cancel_task(uuid_string(operation_id), match(request)), {}, keyed=True)
        return json_response(result)

    @app.get("/api/events")
    async def events(request: Request):
        query(request, ("topics", "project_id", "resource_id"))
        pid = request.query_params.get("project_id")
        if pid:
            runtime.require(uuid_string(pid))
        topics = tuple(part for entry in request.query_params.getlist("topics") for part in entry.split(",") if part)
        sub, initial = runtime.events.subscribe(after=request.headers.get("last-event-id"), topics=topics, project_id=pid, resource_id=request.query_params.get("resource_id"))
        async def stream():
            try:
                for frame in initial:
                    yield runtime.events.encode(frame)
                while not runtime.closed:
                    if sub.closed and sub.queue.empty():
                        break
                    if await request.is_disconnected():
                        break
                    try:
                        frame = await asyncio.to_thread(sub.queue.get, True, 0.5)
                        yield runtime.events.encode(frame)
                    except queue.Empty:
                        # Poll closure frequently, while limiting on-wire heartbeats.
                        import time
                        if time.monotonic() - getattr(sub, "heartbeat", 0) >= 15:
                            sub.heartbeat = time.monotonic()
                            yield b": heartbeat\n\n"
            finally:
                runtime.events.unsubscribe(sub)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/projects")
    async def recent_projects(request: Request):
        options = paging(request)
        return json_response(await dispatch(request, lambda: runtime.recent.list(**options)))

    @app.get("/api/projects/current")
    async def current(request: Request):
        query(request, ())
        return json_response(runtime.current())

    @app.post("/api/projects")
    async def create(value: C.CreateProject, request: Request):
        operation = await dispatch(request, lambda: runtime.project_action("create", **value.model_dump()), value.model_dump(), keyed=True)
        return accepted(request, operation)

    @app.post("/api/projects/open")
    async def open_existing(value: C.OpenProject, request: Request):
        operation = await dispatch(request, lambda: runtime.project_action("open", **value.model_dump()), value.model_dump(), keyed=True)
        return accepted(request, operation)

    P = "/api/projects/{project_id}"

    @app.get(P)
    async def get_project(project_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, runtime.project_view, project_id=project_id))

    @app.patch(P)
    async def update_project(project_id: str, value: C.ProjectPatch, request: Request):
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.update_project(ctx, value.model_dump(), match(request)), project_id=project_id))

    @app.post(P + "/close")
    async def close(project_id: str, value: C.Empty, request: Request):
        operation = await dispatch(request, lambda: runtime.project_action("close", project_id=uuid_string(project_id), expected=match(request)), {}, keyed=True)
        return accepted(request, operation)

    @app.get(P + "/recovery")
    async def recovery(project_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: ctx.handle.recovery_report, project_id=project_id))

    @app.get(P + "/images")
    async def list_images(project_id: str, request: Request):
        page = paging(request, (*IMAGE_FILTERS, "sort", "around_id"))
        params = request.query_params
        filters = image_query(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.list_images(ctx, filters, **page, sort=params.get("sort", "id"), around_id=positive(params.get("around_id"))), project_id=project_id))

    @app.get(P + "/images/stats")
    async def image_stats(project_id: str, request: Request):
        query(request, IMAGE_FILTERS)
        filters = image_query(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.image_stats(ctx, filters), project_id=project_id))

    @app.post(P + "/images/review/preview")
    async def review_preview(project_id: str, value: C.ReviewPreview, request: Request):
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.preview_review(ctx, value.query.model_dump()), value.model_dump(), project_id=project_id, keyed=True))

    @app.post(P + "/images/review")
    async def review(project_id: str, value: C.ReviewInput, request: Request):
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.review(ctx, value.model_dump()), value.model_dump(), project_id=project_id, keyed=True))

    @app.get(P + "/images/{image_id}")
    async def get_image(project_id: str, image_id: int, request: Request):
        query(request, ("include",))
        if request.query_params.get("include") not in (None, "metadata"):
            raise ServiceError(422, "input:invalid")
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.get_image(ctx, image_id, request.query_params.get("include") == "metadata"), project_id=project_id))

    async def media_response(project_id, image_id, request, size):
        data, content_type, etag = await dispatch(request, lambda ctx: runtime.dataset.media(ctx, image_id, size), project_id=project_id)
        tags = [tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")]
        headers = {"ETag": '"' + etag + '"', "Cache-Control": "private, max-age=0, must-revalidate", "Vary": "Authorization", "Accept-Ranges": "none", "Content-Disposition": "inline"}
        if "*" in tags or headers["ETag"] in tags:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type=content_type, headers=headers)

    @app.get(P + "/images/{image_id}/content")
    async def content(project_id: str, image_id: int, request: Request):
        query(request, ())
        return await media_response(project_id, image_id, request, None)

    @app.post(P + "/images/{image_id}/verify-file")
    async def verify_file(project_id: str, image_id: int, value: C.Empty, request: Request):
        from ..services.image_verification import start
        query(request, ())
        expected = match(request)
        return accepted(request, await dispatch(request, lambda ctx: start(runtime, ctx, image_id, expected),
                                               {'expected': expected}, project_id=project_id, keyed=True))

    @app.get(P + "/images/{image_id}/thumbnail")
    async def thumbnail(project_id: str, image_id: int, request: Request):
        query(request, ("size",))
        return await media_response(project_id, image_id, request, positive(request.query_params.get("size"), 256))

    @app.get(P + "/annotations/{image_id}")
    async def get_annotations(project_id: str, image_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.get_annotations(ctx, image_id), project_id=project_id))

    @app.put(P + "/annotations/{image_id}/manual")
    async def manual(project_id: str, image_id: int, value: C.ManualEdit, request: Request):
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.edit(ctx, image_id, value.model_dump(), match(request)), project_id=project_id))

    @app.post(P + "/annotations/manual/bulk")
    async def bulk(project_id: str, value: C.BulkEdit, request: Request):
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.bulk_edit(ctx, value.model_dump()), value.model_dump(), project_id=project_id, keyed=True))

    @app.get(P + "/sources")
    async def list_sources(project_id: str, request: Request):
        page = paging(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.list_resource(ctx, "sources", **page), project_id=project_id))

    @app.get(P + "/sources/{source_id}")
    async def get_source(project_id: str, source_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.get_source(ctx, source_id), project_id=project_id))

    @app.get(P + "/sources/{source_id}/items")
    async def source_items(project_id: str, source_id: int, request: Request):
        page = paging(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.list_resource(ctx, "items", source_id=source_id, **page), project_id=project_id))

    @app.post(P + "/sources/{source_id}/control")
    async def source_control(project_id: str, source_id: int, value: C.SourceControl, request: Request):
        def control(ctx):
            service = runtime.generation_sources if runtime.dataset.get_source(ctx, source_id)['type'] == 'nai' else runtime.dataset.exchange
            method = service.control if service is runtime.generation_sources else service.control_source
            return method(ctx, source_id, value.action, match(request))
        result = await dispatch(request, control, value.model_dump(), project_id=project_id, keyed=True)
        return accepted(request, result) if "operation_id" in result else json_response(result)

    @app.get(P + '/source-items/{item_id}')
    async def source_item(project_id: str, item_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.get_item(ctx, item_id), project_id=project_id))

    @app.post(P + '/source-items/{item_id}/retry')
    async def retry_source_item(project_id: str, item_id: int, value: C.ItemRetry, request: Request):
        query(request, ())
        expected = match(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.retry_item(ctx, item_id, value.expected_attempt, expected),
            {**value.model_dump(), 'expected': expected}, project_id=project_id, keyed=True))

    @app.post(P + "/sources/{source_id}/import-root")
    async def import_root(project_id: str, source_id: int, value: C.RootInput, request: Request):
        operation = await dispatch(request, lambda ctx: runtime.dataset.exchange.set_import_root(ctx, source_id, value.root_handle, match(request)), value.model_dump(), project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.get(P + "/imports/{source_id}/summary")
    async def import_summary(project_id: str, source_id: int, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.exchange.import_summary(ctx, source_id), project_id=project_id))

    @app.get(P + "/import-schemes")
    async def import_schemes(project_id: str, request: Request):
        page = paging(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.list_resource(ctx, "import-schemes", **page), project_id=project_id))

    @app.get(P + "/export-schemes")
    async def export_schemes(project_id: str, request: Request):
        page = paging(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.list_resource(ctx, "export-schemes", **page), project_id=project_id))

    @app.get("/api/exchange/capabilities")
    async def capabilities():
        from ..exchange.schemes import ImportScheme,ExportScheme
        from ..caption.sorters import pyramid_capability
        pyramid = pyramid_capability()
        return json_response({"readers": ["txt_sidecar"], "parsers": ["literal-tags-v1","catalog-comma-v1"], "encodings": ["utf-8-sig", "utf-8", "gbk", "auto"],
                              "auto_encoding_policy": "utf8_then_gbk",
                              "exporters": ["image_txt"], "sorters": ["layer_first", *(['prompt_pyramid'] if pyramid['available'] else [])],
                              "sorter_details": {'prompt_pyramid': pyramid}, "output_modes": ["copy", "hardlink"], "pipeline": False,
                              "hardlink_requires_confirmation": True,
                              "catalog_validation": "local_snapshot",'default_import':ImportScheme(name='新导入方案').model_dump(),
                              'default_export':ExportScheme(name='新导出方案').model_dump()})

    @app.post(P + "/imports/preview")
    async def preview_import(project_id: str, value: C.ImportPreview, request: Request):
        body=value.model_dump()
        body['options']=value.options.model_dump(exclude_unset=True) if value.options else None
        operation = await dispatch(request, lambda ctx: runtime.dataset.exchange.preview_import(ctx, body), body, project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.post(P + "/imports")
    async def start_import(project_id: str, value: C.ImportStart, request: Request):
        operation = await dispatch(request, lambda ctx: runtime.dataset.exchange.start_import(ctx, value.model_dump()), value.model_dump(), project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.post(P + "/exports/preview")
    async def export_preview(project_id: str, value: C.ExportPreview, request: Request):
        operation = await dispatch(request, lambda ctx: runtime.dataset.exchange.preview_export(ctx, value.model_dump()), value.model_dump(), project_id=project_id, keyed=True)
        return accepted(request, operation)

    @app.post(P + "/exports")
    async def export_start(project_id: str, value: C.ExportStart, request: Request):
        view = await dispatch(request, lambda ctx: runtime.dataset.exchange.start_export(ctx, value.model_dump()), value.model_dump(), project_id=project_id, keyed=True)
        url = f"/api/projects/{project_id}/exports/{view['export_id']}"
        return json_response({**view, "status_url": url}, 202, {"Location": url, "Retry-After": "1"})

    @app.get(P + "/exports")
    async def export_list(project_id: str, request: Request):
        page = paging(request)
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.exchange.list_exports(ctx, **page), project_id=project_id))

    @app.get(P + "/exports/{export_id}")
    async def export_get(project_id: str, export_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.exchange.get_export(ctx, uuid_string(export_id)), project_id=project_id))

    @app.get(P + "/exports/{export_id}/report")
    async def export_report(project_id: str, export_id: str, request: Request):
        query(request, ())
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.exchange.report(ctx, uuid_string(export_id)), project_id=project_id))

    @app.post(P + "/exports/{export_id}/cancel")
    async def export_cancel(project_id: str, export_id: str, value: C.Empty, request: Request):
        return json_response(await dispatch(request, lambda ctx: runtime.dataset.exchange.cancel_export(ctx, uuid_string(export_id), match(request)), {}, project_id=project_id, keyed=True))

    from .storage import install_routes as install_storage
    install_storage(app, runtime, dispatch, accepted)
    from .export_recovery import install_routes as install_export_recovery
    install_export_recovery(app, runtime, dispatch, accepted)
    from .cleaning import install_routes
    install_routes(app, runtime, dispatch)
    from .schemes import install_routes as install_schemes
    install_schemes(app,runtime,dispatch)
    from .backends import install_routes as install_backends
    install_backends(app, runtime, dispatch, accepted)
    from .nai_direct import install_routes as install_nai_direct
    install_nai_direct(app, runtime, dispatch, accepted)
    from .duplicates import install_routes as install_duplicates
    install_duplicates(app, runtime, dispatch)
    from .tagger import install_routes as install_tagger
    install_tagger(app, runtime, dispatch)
    from .llm import install_routes as install_llm
    install_llm(app, runtime, dispatch)
    from .executors import install_routes as install_executors
    install_executors(app, runtime, dispatch)
    from .prompt_production import install_routes as install_prompt_production
    install_prompt_production(app, runtime, dispatch)
    from .generation import install_routes as install_generation
    install_generation(app, runtime, dispatch, accepted)
    from .presets import install_routes as install_presets
    install_presets(app, runtime, dispatch)
    return app
