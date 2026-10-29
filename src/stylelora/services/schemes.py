from ..exchange.schemes import ExportScheme, ImportParams, ImportScheme, export_configuration
from ..exchange.importer import scheme_options
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import schemes
from ..project_db.types import ExportSchemeInput, ImportSchemeInput
from ..project_db.validation import primitive
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError
from ..caption.sorters import pyramid_capability


def configuration(row, importing):
    if not importing:
        return export_configuration(row)[0]
    params = dict(row.params_json)
    if 'tokenizer_version' not in params:
        opts = scheme_options(row)
        params = ImportParams(encoding=opts.encoding,recursive=opts.recursive,
                              tokenizer_version='comma-v1' if opts.parser=='catalog-comma-v1' else 'literal-tags-v1').model_dump()
    return ImportScheme(name=row.name,reader=row.reader,params_json=ImportParams.model_validate(params),is_default=row.is_default).model_dump()


def view(runtime, context, row, importing):
    kind = 'import-schemes' if importing else 'export-schemes'
    result = primitive(row)
    result['version'] = runtime.versions.get(runtime.dataset.key(context,kind,row.scheme_id),primitive(row))
    config = configuration(row,importing)
    reasons = []
    if not importing:
        if config['pipeline_json']: reasons.append('pipeline:unsupported')
        if config['caption_config_json']['tag_sort']=='prompt_pyramid' and not pyramid_capability()['available']:
            reasons.append('sorter:unsupported')
    result.update(configuration=config,executable=not reasons,unavailable_reasons=reasons)
    return result


class SchemeService:
    def __init__(self,runtime):
        self.runtime=runtime

    def _get(self,db,identifier,importing):
        return (schemes.get_import_scheme if importing else schemes.get_export_scheme)(db,identifier)

    def get(self,context,identifier,importing):
        with read_snapshot(context.handle) as db:
            return view(self.runtime,context,self._get(db,identifier,importing),importing)

    def _expected(self,context,rows,expected):
        wanted={str(row.scheme_id) for row in rows}
        if expected is None:
            raise ServiceError(428,'concurrency:precondition_required')
        if set(expected)!=wanted:
            raise ServiceError(422,'concurrency:invalid_precondition')
        for row in rows:
            Versions.require(expected[str(row.scheme_id)],view(self.runtime,context,row,True)['version'])

    def _emit(self,context,importing):
        self.runtime.emit('scheme.changed',{'kind':'import' if importing else 'export'},project_id=context.project_id)

    def save(self,context,value,importing,identifier=None,expected=None):
        body=(ImportScheme if importing else ExportScheme).model_validate(value).model_dump()
        if not importing:
            body['pipeline_json']=tuple(body['pipeline_json'])
        item=(ImportSchemeInput if importing else ExportSchemeInput)(**body)
        def write(db):
            if identifier is None:
                if importing and item.is_default:
                    raise ServiceError(422,'scheme:use_default_action')
                row=(schemes.create_import_scheme if importing else schemes.create_export_scheme)(db,item)
            else:
                old=self._get(db,identifier,importing)
                Versions.require(expected,view(self.runtime,context,old,importing)['version'])
                if importing and item.is_default!=old.is_default:
                    raise ServiceError(422,'scheme:use_default_action')
                row=(schemes.update_import_scheme if importing else schemes.update_export_scheme)(db,identifier,item,expected_updated_at=old.updated_at).value
            return view(self.runtime,context,row,importing)
        result=submit_write(context.handle,write).result()
        self._emit(context,importing)
        return result

    def set_default(self,context,identifier,expected):
        def write(db):
            row=schemes.get_import_scheme(db,identifier)
            old=schemes.get_default_import_scheme(db)
            self._expected(context,[row,old],expected)
            return view(self.runtime,context,schemes.set_default_import_scheme(db,identifier).value,True)
        result=submit_write(context.handle,write).result()
        self._emit(context,True)
        return result

    def delete(self,context,identifier,importing,expected=None,replacement=None,versions=None):
        def write(db):
            row=self._get(db,identifier,importing)
            if importing:
                rows=[row]
                if row.is_default:
                    if replacement is None or replacement==identifier:
                        raise ServiceError(409,'scheme:last_or_default')
                    rows.append(schemes.get_import_scheme(db,replacement))
                elif replacement is not None:
                    raise ServiceError(422,'scheme:invalid_replacement')
                self._expected(context,rows,versions)
                if row.is_default:
                    schemes.set_default_import_scheme(db,replacement)
                schemes.delete_import_scheme(db,identifier)
            else:
                Versions.require(expected,view(self.runtime,context,row,False)['version'])
                if db.execute('SELECT count(*) FROM export_schemes').fetchone()[0]<=1:
                    raise ServiceError(409,'scheme:last_scheme')
                schemes.delete_export_scheme(db,identifier)
        submit_write(context.handle,write).result()
        self._emit(context,importing)
