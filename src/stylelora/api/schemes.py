from fastapi import Request
from starlette.responses import Response

from ..exchange.schemes import ImportScheme, ExportScheme
from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError
from ..services.schemes import SchemeService
from . import contracts as C


def install_routes(app,runtime,dispatch):
    from .app import json_response,match,positive,query
    service=SchemeService(runtime)
    P='/api/projects/{project_id}'

    def version_header(request):
        values=request.headers.getlist('x-expected-versions')
        if not values:
            return None
        if len(values)!=1 or len(values[0])>2048:
            raise ServiceError(422,'concurrency:invalid_precondition')
        value=strict_json(values[0])
        if not isinstance(value,dict) or len(value)>2 or any(not k.isdecimal() or str(int(k))!=k or int(k)<1 or not isinstance(v,str) or len(v)>256 for k,v in value.items()):
            raise ServiceError(422,'concurrency:invalid_precondition')
        return value

    @app.post(P+'/import-schemes')
    async def create_import(project_id:str,value:ImportScheme,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:service.save(ctx,body,True),body,project_id=project_id,keyed=True),201)

    @app.post(P+'/export-schemes')
    async def create_export(project_id:str,value:ExportScheme,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:service.save(ctx,body,False),body,project_id=project_id,keyed=True),201)

    @app.get(P+'/import-schemes/{scheme_id}')
    async def get_import(project_id:str,scheme_id:int,request:Request):
        query(request,())
        return json_response(await dispatch(request,lambda ctx:service.get(ctx,scheme_id,True),project_id=project_id))

    @app.get(P+'/export-schemes/{scheme_id}')
    async def get_export(project_id:str,scheme_id:int,request:Request):
        query(request,())
        return json_response(await dispatch(request,lambda ctx:service.get(ctx,scheme_id,False),project_id=project_id))

    @app.put(P+'/import-schemes/{scheme_id}')
    async def update_import(project_id:str,scheme_id:int,value:ImportScheme,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:service.save(ctx,body,True,scheme_id,match(request)),body,project_id=project_id,keyed=True))

    @app.put(P+'/export-schemes/{scheme_id}')
    async def update_export(project_id:str,scheme_id:int,value:ExportScheme,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:service.save(ctx,body,False,scheme_id,match(request)),body,project_id=project_id,keyed=True))

    @app.post(P+'/import-schemes/{scheme_id}/set-default')
    async def set_default(project_id:str,scheme_id:int,value:C.DefaultScheme,request:Request):
        body=value.model_dump()
        return json_response(await dispatch(request,lambda ctx:service.set_default(ctx,scheme_id,value.expected_versions),body,project_id=project_id,keyed=True))

    @app.delete(P+'/import-schemes/{scheme_id}')
    async def delete_import(project_id:str,scheme_id:int,request:Request):
        query(request,('replacement_default_id',))
        if len(request.query_params.getlist('replacement_default_id'))>1:
            raise ServiceError(422,'input:invalid')
        replacement=positive(request.query_params.get('replacement_default_id'))
        versions=version_header(request)
        await dispatch(request,lambda ctx:service.delete(ctx,scheme_id,True,replacement=replacement,versions=versions),{'replacement':replacement,'versions':versions},project_id=project_id,keyed=True)
        return Response(status_code=204)

    @app.delete(P+'/export-schemes/{scheme_id}')
    async def delete_export(project_id:str,scheme_id:int,request:Request):
        query(request,())
        await dispatch(request,lambda ctx:service.delete(ctx,scheme_id,False,expected=match(request)),{},project_id=project_id,keyed=True)
        return Response(status_code=204)

    @app.post('/api/validation/trigger')
    async def check_trigger(value:C.TriggerValidation,request:Request):
        body=value.model_dump()
        def check(ctx=None):
            return runtime.dataset.exchange.validate_trigger(ctx,body)
        return json_response(await dispatch(request,check,body,project_id=value.project_id))
