import hashlib

from fastapi import Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser, MultiPartException

from ..runtime.contracts import uuid_string
from ..runtime.errors import ServiceError
from . import contracts as C


def upload_schema(*, rules=False):
    properties = {'file': {'type':'string','format':'binary'},
                  'format': {'type':'string','enum':['json','tagcleaner'] if rules else ['json','tsv']}}
    if not rules:
        properties['kind'] = {'type':'string','enum':['catalog','translations']}
    else:
        properties['kind'] = {'type':'string','enum':['rules']}
    return {'requestBody': {'required':True,'content':{'multipart/form-data':{'schema':{
        'type':'object','additionalProperties':False,'required':['file'],'properties':properties}}}}}


def install_routes(app, runtime, dispatch):
    from .app import match, json_response, paging, query
    P = '/api/projects/{project_id}'

    def key(request):
        keys=request.headers.getlist('idempotency-key')
        if not keys:
            raise ServiceError(428,'idempotency:key_required')
        if len(keys)!=1:
            raise ServiceError(422,'idempotency:invalid_key')
        return uuid_string(keys[0])

    @app.post(P+'/rules/preview')
    async def rule_preview(project_id: str, value:C.RulePreviewInput, request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.preview(ctx,body),body,project_id=project_id,keyed=True))

    @app.post(P+'/rules/apply')
    async def rule_apply(project_id:str,value:C.ApplyPreview,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.apply(ctx,body,match(request)),body,project_id=project_id,keyed=True))

    @app.get(P+'/operation-log')
    async def rule_log(project_id:str,request:Request):
        page=paging(request,('status',))
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.log(ctx,**page,status=request.query_params.get('status')),project_id=project_id))

    @app.get(P+'/operation-log/{op_id}')
    async def rule_detail(project_id:str,op_id:int,request:Request):
        page=paging(request)
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.detail(ctx,op_id,**page),project_id=project_id))

    @app.post(P+'/operation-log/{op_id}/undo')
    async def rule_undo(project_id:str,op_id:int,value:C.Empty,request:Request):
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.undo(ctx,op_id,match(request)),{},project_id=project_id,keyed=True))

    @app.post(P+'/annotations/manual/bulk/preview')
    async def bulk_preview(project_id:str,value:C.BulkPreviewInput,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.bulk_preview(ctx,body),body,project_id=project_id,keyed=True))

    @app.post(P+'/annotations/manual/bulk/apply')
    async def bulk_apply(project_id:str,value:C.ApplyPreview,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.bulk_apply(ctx,body,match(request)),body,project_id=project_id,keyed=True))

    @app.post(P+'/annotations/stats')
    async def annotation_stats(project_id:str,value:C.StatsInput,request:Request):
        return json_response(await dispatch(request,lambda ctx:runtime.cleaning.stats(ctx,value.model_dump()),project_id=project_id))

    @app.get('/api/tags/catalog-status')
    async def catalog_status(request:Request):
        query(request,())
        return json_response(await dispatch(request,runtime.catalog.status))

    @app.get('/api/tags/search')
    async def tag_search(request:Request):
        page=paging(request,('q','provider'))
        if request.query_params.get('provider','local')!='local':
            raise ServiceError(501,'catalog:provider_unavailable')
        return json_response(await dispatch(request,lambda:runtime.catalog.search(request.query_params.get('q',''),**page)))

    @app.post('/api/tags/translations/lookup')
    async def translations(value:C.TranslationLookup,request:Request):
        def lookup():
            catalog=runtime.catalog.snapshot()
            return {'items':[catalog.hit(tag) for tag in value.tags],'catalog_revision':catalog.revision,'available':catalog.available}
        return json_response(await dispatch(request,lookup))

    @app.put('/api/translations')
    async def translation_update(value:C.TranslationInput,request:Request):
        return json_response(await dispatch(request,lambda:runtime.catalog.put_translation(value.model_dump(),match(request),key(request))))

    @app.get('/api/rules')
    async def library(request:Request):
        query(request,())
        return json_response(await dispatch(request,runtime.catalog.rules))

    @app.put('/api/rules')
    async def library_update(value:C.RuleLibraryInput,request:Request):
        return json_response(await dispatch(request,lambda:runtime.catalog.put_rules(value.model_dump(),match(request),key(request))))

    async def upload(request, rule=False):
        key(request)
        # The security layer has already bounded the whole body to 11 MiB. Keep
        # these small metadata files in memory, never in a client-named disk path.
        class BoundedParser(MultiPartParser):
            spool_max_size=11*1024*1024
        try:
            form=await BoundedParser(request.headers,request.stream(),max_files=1,max_fields=2,max_part_size=10*1024*1024).parse()
        except MultiPartException as exc:
            raise ServiceError(422,'catalog:invalid_upload') from exc
        try:
            if set(form)-{'file','kind','format'} or len(form.multi_items())!=len(form):
                raise ServiceError(422,'catalog:invalid_upload')
            file=form.get('file')
            if not isinstance(file,UploadFile):
                raise ServiceError(422,'catalog:invalid_upload')
            data=await file.read(10*1024*1024+1)
            if len(data)>10*1024*1024:
                raise ServiceError(413,'catalog:too_large')
            kind='rules' if rule else form.get('kind','catalog')
            format=form.get('format','tagcleaner' if rule else 'json')
            if not isinstance(kind,str) or not isinstance(format,str) or not rule and kind not in ('catalog','translations') or format not in (('json','tagcleaner') if rule else ('json','tsv')) or rule and form.get('kind','rules')!='rules':
                raise ServiceError(422,'catalog:invalid_upload')
            signature={'kind':kind,'format':format,'sha256':hashlib.sha256(data).hexdigest()}
            return json_response(await dispatch(request,lambda:runtime.catalog.preview(kind,data,format),signature,keyed=True))
        finally:
            await form.close()

    @app.post('/api/tags/imports/preview',openapi_extra=upload_schema())
    async def import_catalog(request:Request):
        return await upload(request)

    @app.post('/api/rules/imports/preview',openapi_extra=upload_schema(rules=True))
    async def import_rules(request:Request):
        return await upload(request,True)

    @app.post('/api/tags/imports/commit')
    @app.post('/api/rules/imports/commit')
    async def commit_catalog(value:C.ApplyPreview,request:Request):
        return json_response(await dispatch(request,lambda:runtime.catalog.commit(value.model_dump(),match(request),key(request))))
