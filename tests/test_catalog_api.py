import json
from unittest.mock import patch
from uuid import uuid4

from stylelora.tagging.library import CatalogLibrary
from stylelora.runtime.errors import ServiceError
from .api_support import APITest


class CatalogAPITests(APITest):
    def upload(self, value, kind='catalog', format='json', status=200, raw=False):
        path = '/api/rules/imports/preview' if kind == 'rules' else '/api/tags/imports/preview'
        result = self.client.post(path, files={'file':('local.json', value if raw else json.dumps(value).encode(), 'application/json')},
                                  data={'kind':kind,'format':format},headers={'Idempotency-Key':str(uuid4())})
        self.assertEqual(result.status_code,status,result.text)
        return result.json()

    def commit(self, p, status=200, key=None):
        return self.request('POST','/api/tags/imports/commit', {'preview_id':p['preview_id'],'digest':p['digest']},version=p['version'],status=status,key=key)

    def catalog(self):
        return {'schema_version':1,'kind':'tag_catalog','tags':[{'tag':'blue_hair','aliases':['azure_hair']},{'tag':'smile'}]}

    def lookup(self,*tags):
        return self.request('POST','/api/tags/translations/lookup', {'tags':list(tags)})['items']

    def test_import_search_alias_translation_and_untranslated_known(self):
        self.assertEqual(self.lookup('blue hair')[0]['match'],'unverifiable')
        p=self.upload(self.catalog())
        self.assertFalse(self.runtime.catalog.status()['available'])
        self.commit(p)
        self.commit(self.upload({'azure_hair':'蓝发'},kind='translations'))
        hits=self.lookup('azure hair','smile','unlisted')
        self.assertEqual([(h['match'],h['canonical'],h['translation']) for h in hits],
                         [('alias','blue hair','蓝发'),('known','smile',None),('unknown',None,None)])
        search=self.request('GET','/api/tags/search?q=蓝发')
        self.assertEqual(search['items'][0]['canonical'],'blue hair')
        self.assertEqual(self.runtime.catalog.status()['coverage'],'partial')

    def test_empty_import_preserves_translation_explicit_empty_deletes(self):
        self.commit(self.upload({'blue_hair':'蓝发'},kind='translations'))
        p=self.upload({'blue hair':''},kind='translations')
        self.assertEqual(p['skipped'],1)
        self.commit(p)
        self.assertEqual(self.lookup('blue hair')[0]['translation'],'蓝发')
        self.request('PUT','/api/translations',{'tag':'blue_hair','value':''},version=self.runtime.catalog.status()['version'],headers={'Idempotency-Key':str(uuid4())})
        self.assertIsNone(self.lookup('blue hair')[0]['translation'])

    def test_alias_and_normalized_translation_conflicts_reject_whole_import(self):
        self.commit(self.upload(self.catalog()))
        self.upload({'azure_hair':'甲','blue hair':'乙'},kind='translations',status=422)
        self.upload({'blue_hair':'甲','BLUE HAIR':'乙'},kind='translations',status=422)
        invalid=self.catalog();invalid['tags'][1]['aliases']=['azure_hair']
        self.upload(invalid,status=422)
        self.assertEqual(self.runtime.catalog.status()['translation_count'],0)

    def test_strict_json_multipart_and_file_limits(self):
        self.upload(b'{"x":"a","x":"b"}',kind='translations',raw=True,status=400)
        self.upload(b'x'*(10*1024*1024+1),raw=True,status=413)
        result=self.client.post('/api/tags/imports/preview',files=[('file',('a',b'{}')),('file',('b',b'{}'))],headers={'Idempotency-Key':str(uuid4())})
        self.assertEqual(result.status_code,422)
        result=self.client.post('/api/tags/imports/preview',files={'file':('a',b'{}')},headers={'Authorization':'Bearer bad'})
        self.assertEqual(result.status_code,401)

    def test_import_cas_and_persistent_replay_after_reload(self):
        self.runtime.catalog=CatalogLibrary(self.runtime,self.base/'product')
        p=self.upload(self.catalog()); other=self.upload({'smile':'微笑'},kind='translations')
        key=str(uuid4());result=self.commit(p,key=key)
        self.commit(other,status=409)
        self.runtime.catalog=CatalogLibrary(self.runtime,self.base/'product')
        self.runtime.previews.values.clear()
        self.assertEqual(self.commit(p,key=key),result)
        self.assertEqual(self.runtime.catalog.status()['tag_count'],2)
        self.request('POST','/api/tags/imports/commit',{'preview_id':p['preview_id'],'digest':'0'*64},version=p['version'],key=key,status=409)

    def test_atomic_file_failure_preserves_previous_state_and_can_retry(self):
        self.runtime.catalog=CatalogLibrary(self.runtime,self.base/'product')
        self.commit(self.upload(self.catalog()))
        before=self.runtime.catalog.path.read_bytes()
        p=self.upload({'smile':'微笑'},kind='translations')
        with patch('stylelora.tagging.library.atomic_json',side_effect=OSError('injected')):
            self.commit(p,status=503)
        self.assertEqual(self.runtime.catalog.path.read_bytes(),before)
        self.assertIsNone(self.lookup('smile')[0]['translation'])
        self.commit(p)
        self.assertEqual(self.lookup('smile')[0]['translation'],'微笑')

    def test_tagcleaner_migration_normalization_and_rule_library_cas(self):
        p=self.upload([{'id':3,'name':'旧规则','condition':'Blue_Hair','delete':['smile'],'write':'curated, detailed'}],kind='rules',format='tagcleaner')
        self.assertEqual(p['issues'][0]['code'],'migration:rule_semantics_changed')
        self.commit(p)
        library=self.request('GET','/api/rules')
        row=library['rules'][0]
        self.assertEqual(row['rule']['condition'],{'mode':'any','tags':['blue hair']})
        self.assertNotEqual(row['id'],'3')
        body={'schema_version':1,'kind':'replacement_rules','rules':[]}
        self.request('PUT','/api/rules',body,version=library['version'],status=428)
        self.request('PUT','/api/rules',body,version=library['version'],headers={'Idempotency-Key':str(uuid4())})
        self.request('PUT','/api/rules',body,version=library['version'],headers={'Idempotency-Key':str(uuid4())},status=409)

    def test_catalog_does_not_rewrite_annotations_and_search_cursor_is_versioned(self):
        image_id=self.import_inputs()['items'][0]['image_id']
        before=self.request('GET',self.p+f'/annotations/{image_id}')
        self.commit(self.upload(self.catalog()))
        page=self.request('GET','/api/tags/search?limit=1')
        self.assertTrue(page['has_more'])
        self.commit(self.upload({'blue_hair':'蓝发'},kind='translations'))
        self.request('GET',f"/api/tags/search?limit=1&cursor={page['next_cursor']}",status=409)
        after=self.request('GET',self.p+f'/annotations/{image_id}')
        self.assertEqual(after,before)

    def test_corrupt_product_library_fails_closed(self):
        directory=self.base/'product';directory.mkdir()
        path=directory/'tagging-library.json';path.write_text('{broken',encoding='utf-8')
        with self.assertRaises((ServiceError,ValueError)):
            CatalogLibrary(self.runtime,directory)
        self.assertEqual(path.read_text(),'{broken')
