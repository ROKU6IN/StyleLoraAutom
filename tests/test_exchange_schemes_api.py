import json
from uuid import uuid4

from stylelora.project_db.connection import read_snapshot
from stylelora.project_db.repositories import annotations, sources
from stylelora.tagging.library import CatalogLibrary
from stylelora.runtime.errors import ServiceError
from stylelora.exchange.importer import import_images
from .api_support import APITest


class ExchangeSchemeTests(APITest):
    def mutation(self, method, path, body=None, **kwargs):
        return self.request(method, path, body, headers={'Idempotency-Key':str(uuid4())}, **kwargs)

    def create_scheme(self, importing=True, **values):
        return self.request('POST',self.p+('/import-schemes' if importing else '/export-schemes'),{'name':'测试方案',**values},status=201)

    def get_scheme(self, row, importing=True):
        return self.request('GET',self.p+('/import-schemes/' if importing else '/export-schemes/')+str(row['scheme_id']))

    def catalog(self, tags=None, complete=True):
        value={'schema_version':1,'kind':'tag_catalog','complete':complete,'tags':tags or [{'tag':'blue_hair','aliases':['azure_hair']},{'tag':'smile'}]}
        response=self.client.post('/api/tags/imports/preview',files={'file':('catalog.json',json.dumps(value).encode(),'application/json')},data={'kind':'catalog','format':'json'},headers={'Idempotency-Key':str(uuid4())})
        self.assertEqual(response.status_code,200,response.text)
        p=response.json()
        return self.request('POST','/api/tags/imports/commit',{'preview_id':p['preview_id'],'digest':p['digest']},version=p['version'])

    def preview(self, scheme):
        grant=self.runtime.grant(self.inputs,'import_root',self.pid)
        job=self.request('POST',self.p+'/imports/preview',{'root_handle':grant['grant_id'],'scheme_id':scheme['scheme_id']},status=202)
        return self.wait_operation(job)['result']

    def run_import(self,p):
        return self.request('POST',self.p+'/imports',{'preview_id':p['preview_id'],'digest':p['digest']},status=202)

    def test_crud_cas_noop_idempotency_and_last_scheme(self):
        row=self.create_scheme(False)
        path=self.p+'/export-schemes/'+str(row['scheme_id'])
        self.mutation('PUT',path,row['configuration'],status=428)
        body={**row['configuration'],'name':'updated'}
        updated=self.mutation('PUT',path,body,version=row['version'])
        self.assertNotEqual(updated['version'],row['version'])
        self.mutation('PUT',path,body,version=row['version'],status=409)
        self.assertEqual(self.mutation('PUT',path,body,version=updated['version'])['version'],updated['version'])
        self.mutation('DELETE',path,version=updated['version'],status=204)
        last=self.request('GET',self.p+'/export-schemes')['items'][0]
        self.mutation('DELETE',self.p+'/export-schemes/'+str(last['scheme_id']),version=last['version'],status=409)
        key=str(uuid4())
        a=self.request('POST',self.p+'/export-schemes',{'name':'once'},key=key,status=201)
        b=self.request('POST',self.p+'/export-schemes',{'name':'once'},key=key,status=201)
        self.assertEqual(a,b)

    def test_default_switch_and_delete_atomic_preconditions(self):
        old=self.request('GET',self.p+'/import-schemes')['items'][0]
        new=self.create_scheme()
        path=self.p+'/import-schemes/'+str(new['scheme_id'])+'/set-default'
        versions={str(r['scheme_id']):r['version'] for r in (old,new)}
        self.request('POST',path,{'expected_versions':{**versions,str(old['scheme_id']):'stale'}},status=409)
        self.assertEqual(self.get_scheme(old)['version'],old['version'])
        active=self.request('POST',path,{'expected_versions':versions})
        old=self.get_scheme(old)
        self.assertTrue(active['is_default'])
        self.assertFalse(old['is_default'])
        delete=self.p+'/import-schemes/'+str(active['scheme_id'])+'?replacement_default_id='+str(old['scheme_id'])
        headers={'Idempotency-Key':str(uuid4()),'X-Expected-Versions':json.dumps({str(r['scheme_id']):r['version'] for r in (old,active)})}
        self.request('DELETE',delete,headers=headers,status=204)
        self.assertTrue(self.get_scheme(old)['is_default'])
        self.mutation('DELETE',self.p+'/import-schemes/'+str(old['scheme_id']),status=409)

    def test_invalid_configs_and_stored_reserved_capabilities(self):
        for output in ({'subdirectory':'../escape'},{'subdirectory':'C:/data'},{'subdirectory':'CON'},{'filename':'{arbitrary}'},{'include_json':'yes'}):
            self.request('POST',self.p+'/export-schemes',{'name':'bad','output_config_json':output},status=422)
        self.request('POST',self.p+'/import-schemes',{'name':'bad','params_json':{'schema_version':True}},status=422)
        self.request('POST',self.p+'/export-schemes',{'name':'bad','filter_json':{'include_missing':0}},status=422)
        for value in ({'pipeline_json':[{'processor':'metadata_strip'}]},):
            row=self.create_scheme(False,**value)
            self.assertFalse(row['executable'])
            job=self.request('POST',self.p+'/exports/preview',{'scheme_id':row['scheme_id']},status=202)
            self.assertEqual(self.wait_operation(job,expected='failed')['error']['code'],'feature:unsupported')
        self.assertFalse(list((self.root/'exports').iterdir()))
        row=self.create_scheme(params_json={'encoding':'auto'})
        self.assertTrue(row['executable'])

    def test_catalog_parser_canonical_tags_nl_and_frozen_evidence(self):
        self.catalog()
        row=self.create_scheme()
        self.make_input(caption='azure_hair, smile, 画面说明\n\n段落。')
        p=self.preview(row)
        self.assertTrue(p['can_import'])
        self.assertEqual(p['blocking_errors'],0)
        self.assertEqual(p['diagnostics'][0]['originals'][0]['content'][0]['tag'],'blue hair')
        report=self.wait_operation(self.run_import(p))['result']
        with read_snapshot(self.runtime.context.handle) as db:
            original=annotations.get_current(db,1,'original','tags')
            nl=annotations.get_current(db,1,'original','nl')
            source=sources.get_source(db,report['source_id'])
        self.assertEqual([t.tag for t in original.content],['blue hair','smile'])
        self.assertEqual(nl.content,'画面说明\n段落。')
        self.assertEqual(original.params_json['segments'][0]['raw'],'azure_hair')
        self.assertEqual(source.config_json['catalog_revision'],p['catalog_revision'])
        self.assertEqual(source.config_json['scheme_snapshot']['scheme_id'],row['scheme_id'])

    def test_parse_failures_block_before_source_or_file_write(self):
        self.catalog()
        row=self.create_scheme()
        for text in ('unknown', '(blue_hair:1.2), smile', r'blue_hair\, smile'):
            self.make_input(caption=text)
            p=self.preview(row)
            self.assertFalse(p['can_import'])
            self.request('POST',self.p+'/imports',{'preview_id':p['preview_id'],'digest':p['digest']},status=422)
        self.assertEqual(self.request('GET',self.p+'/sources')['items'],[])
        self.assertFalse(list((self.root/'images').iterdir()))

    def test_changed_scheme_stales_import_before_source_creation(self):
        row=self.create_scheme(params_json={'tokenizer_version':'literal-tags-v1'})
        self.make_input()
        p=self.preview(row)
        self.mutation('PUT',self.p+'/import-schemes/'+str(row['scheme_id']),{**row['configuration'],'name':'changed'},version=row['version'])
        self.assertEqual(self.wait_operation(self.run_import(p),expected='failed')['error']['code'],'concurrency:version_conflict')
        self.assertEqual(self.request('GET',self.p+'/sources')['items'],[])

    def test_catalog_version_archive_survives_restart_and_rejects_corruption(self):
        self.runtime.catalog=CatalogLibrary(self.runtime,self.base/'product')
        self.catalog()
        first=self.runtime.catalog.freeze()
        self.catalog([{'tag':'other'}])
        latest=self.runtime.catalog.freeze()
        self.assertNotEqual(first.revision,latest.revision)
        reopened=CatalogLibrary(self.runtime,self.base/'product')
        self.assertEqual(reopened.load_frozen(first.revision),first)
        target=reopened.path.parent/'catalog-versions'/f'{first.revision}.json'
        target.write_text('{}',encoding='utf-8')
        with self.assertRaises(ServiceError): reopened.load_frozen(first.revision)

    def test_trigger_known_collision_requires_current_digest_not_boolean(self):
        self.import_inputs(); self.keep([1])
        self.catalog([{'tag':'http_style','aliases':['old_style']}])
        checked=self.request('POST','/api/validation/trigger',{'trigger':'old_style','project_id':self.pid})
        self.assertTrue(checked['can_override'])
        p=self.export_preview(allow_existing_trigger=True)
        body={'preview_id':p['preview_id'],'digest':p['digest']}
        self.request('POST',self.p+'/exports',body,status=422)
        self.request('POST',self.p+'/exports',{**body,'trigger_override_digest':'0'*64},status=422)
        result=self.request('POST',self.p+'/exports',{**body,'trigger_override_digest':p['trigger_validation']['override_digest']},status=202)
        self.wait_operation({'operation_id':result['operation_id']})
        report=self.request('GET',self.p+'/exports/'+result['export_id']+'/report')
        self.assertEqual(report['trigger_override_digest'],p['trigger_validation']['digest'])
        self.catalog([{'tag':'unrelated'}])
        self.request('POST',self.p+'/exports',{**body,'trigger_override_digest':p['trigger_validation']['override_digest']},status=409)

    def test_trigger_hard_conflicts_cannot_override_and_unknown_is_explicit(self):
        unavailable=self.request('POST','/api/validation/trigger',{'trigger':'http_style'})
        self.assertEqual(unavailable['status'],'unverifiable')
        self.catalog([{'tag':'http_style'}])
        for text in ('http_style','foo, HTTP STYLE photography'):
            checked=self.request('POST','/api/validation/trigger',{'trigger':'http_style','style_prompts':[text]})
            self.assertEqual(checked['status'],'invalid')
            self.assertFalse(checked['can_override'])
        invalid=self.request('POST','/api/validation/trigger',{'trigger':'(weighted:1.2)'})
        self.assertEqual(invalid['status'],'invalid')
        for trigger in ('_', '\0'):
            self.assertEqual(self.request('POST','/api/validation/trigger',{'trigger':trigger})['status'],'invalid')

    def test_resume_uses_archived_catalog_after_library_and_scheme_changes(self):
        self.runtime.catalog=CatalogLibrary(self.runtime,self.base/'product')
        self.catalog()
        old=self.runtime.catalog.freeze()
        scheme=self.create_scheme()
        self.make_input(caption='azure_hair\n\n原文。')
        report=import_images(self.runtime.context.handle,self.inputs,scheme_id=scheme['scheme_id'],catalog=old,cancelled=lambda:True)
        self.assertEqual(report.status,'paused')
        self.catalog([{'tag':'red_hair','aliases':['azure_hair']}])
        self.runtime.catalog=CatalogLibrary(self.runtime,self.base/'product')
        headers={'Idempotency-Key':str(uuid4()),'X-Expected-Versions':json.dumps({str(scheme['scheme_id']):scheme['version']})}
        self.request('DELETE',self.p+'/import-schemes/'+str(scheme['scheme_id']),headers=headers,status=204)
        path=self.p+'/sources/'+str(report.source_id)
        source=self.request('GET',path)
        grant=self.runtime.grant(self.inputs,'import_root',self.pid)
        self.wait_operation(self.request('POST',path+'/import-root',{'root_handle':grant['grant_id']},version=source['version'],status=202))
        archive=self.runtime.catalog.path.parent/'catalog-versions'/f'{old.revision}.json'
        content=archive.read_bytes();archive.unlink()
        before=self.request('GET',path)
        self.request('POST',path+'/control',{'action':'resume'},version=before['version'],status=409)
        self.assertEqual(self.request('GET',path)['version'],before['version'])
        archive.write_bytes(content)
        result=self.wait_operation(self.request('POST',path+'/control',{'action':'resume'},version=before['version'],status=202))['result']
        self.assertEqual(result['imported'],1)
        self.assertEqual(self.request('GET',self.p+'/annotations/1')['tags'][0]['tag'],'blue hair')

    def test_preflight_validates_errors_after_first_hundred_samples(self):
        scheme=self.create_scheme(params_json={'tokenizer_version':'literal-tags-v1'})
        for index in range(101):
            self.make_input(f'{index:03}.png',caption='safe' if index<100 else '(unsafe:1.1)')
        p=self.preview(scheme)
        self.assertEqual(p['selected'],101)
        self.assertEqual(p['blocking_errors'],1)
        self.assertEqual(len(p['diagnostics']),100)
        self.assertTrue(any(r['path']=='100.png' and r['error'] for r in p['diagnostics']))
        self.request('POST',self.p+'/imports',{'preview_id':p['preview_id'],'digest':p['digest']},status=422)

    def test_export_scheme_change_stales_before_any_output_and_flags_override(self):
        self.import_inputs()
        row=self.create_scheme(False,filter_json={'review_statuses':['unreviewed']})
        def preview(options=None):
            return self.wait_operation(self.request('POST',self.p+'/exports/preview',{'scheme_id':row['scheme_id'],'options':options or {}},status=202))['result']
        self.assertEqual(preview()['images'][0]['status'],'ready')
        self.assertEqual(preview({'include_unreviewed':False})['images'][0]['reason'],'review:filtered')
        p=preview()
        self.mutation('PUT',self.p+'/export-schemes/'+str(row['scheme_id']),{**row['configuration'],'name':'edited'},version=row['version'])
        job=self.request('POST',self.p+'/exports',{'preview_id':p['preview_id'],'digest':p['digest']},status=202)
        self.wait_operation({'operation_id':job['operation_id']},expected='failed')
        self.assertFalse(list((self.root/'exports').iterdir()))

    def test_scheme_output_layout_json_and_nonempty_filter(self):
        self.import_inputs(); self.keep([1])
        row=self.create_scheme(False,caption_config_json={'include_nl':True},output_config_json={'subdirectory':'training/data','filename':'{stem}-{image_id}{ext}','include_json':True})
        p=self.wait_operation(self.request('POST',self.p+'/exports/preview',{'scheme_id':row['scheme_id']},status=202))['result']
        export=self.run_export(p)
        report=self.request('GET',export['report_url'])
        files=report['images'][0]['files']
        self.assertEqual(len(files),3)
        for item in files:
            self.assertTrue(item['path'].startswith('training/data/'))
            self.assertTrue((self.root/report['directory']/item['path']).is_file())
        caption=next(item for item in files if item['path'].endswith('.txt'))
        self.assertIn('原始描述。',(self.root/report['directory']/caption['path']).read_text(encoding='utf-8'))
        row=self.create_scheme(False,caption_config_json={'tag_layers':[]},output_config_json={'require_nonempty_tags':True})
        p=self.wait_operation(self.request('POST',self.p+'/exports/preview',{'scheme_id':row['scheme_id']},status=202))['result']
        self.assertEqual(p['images'][0]['reason'],'caption:empty_content')
