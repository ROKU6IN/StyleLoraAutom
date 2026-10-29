import unittest
from types import SimpleNamespace
from uuid import uuid4

from stylelora.project_db.types import Tag
from stylelora.rules.engine import normalize_rule, evaluate_rule
from stylelora.runtime.lifecycle import Runtime
from .support import DatabaseTest
from .api_support import APITest


def rule(delete=None, write=None, condition=None):
    return {'schema_version': 1, 'condition': condition, 'delete': delete, 'write': write}


def group(*tags, mode='any'):
    return {'mode': mode, 'tags': list(tags)}


class RuleEngineTests(unittest.TestCase):
    def test_original_normalization_empty_and_missing_are_distinct(self):
        r = normalize_rule(rule(write=group('new'), condition=group('BLUE_HAIR')))
        self.assertTrue(evaluate_rule(r, [], [Tag('blue_hair')]).matched)
        self.assertEqual(evaluate_rule(r, [], []).reason, 'condition:not_matched')
        self.assertEqual(evaluate_rule(r, [], None).reason, 'condition:missing_original')

    def test_delete_any_removes_all_hits_and_delete_failure_gates_write(self):
        r = normalize_rule(rule(group('a', 'b', 'c'), group('d')))
        self.assertEqual([t.tag for t in evaluate_rule(r, [Tag('a'), Tag('b')], []).after], ['d'])
        self.assertEqual(evaluate_rule(r, [Tag('x')], []).after, (Tag('x'),))
        r['delete']['mode'] = 'all'
        self.assertFalse(evaluate_rule(r, [Tag('a'), Tag('b')], []).matched)

    def test_write_all_gate_preserves_successful_delete_and_overlap_is_noop(self):
        before = [Tag('a', .8), Tag('b', .7)]
        r = normalize_rule(rule(group('a'), group('b', 'c', mode='all')))
        self.assertEqual(evaluate_rule(r, before, []).after, (Tag('b', .7),))
        self.assertFalse(evaluate_rule(normalize_rule(rule(write=group('b','c',mode='all'))), before, []).matched)
        r = normalize_rule(rule(group('a'), group('a')))
        self.assertEqual(evaluate_rule(r, before, []).after, tuple(before))

    def test_schema_normalizes_duplicates_but_rejects_empty_and_extra(self):
        r = normalize_rule(rule(group(), group(' BLUE_HAIR ', 'blue hair')))
        self.assertIsNone(r['delete'])
        self.assertEqual(r['write']['tags'], ['blue hair'])
        for invalid in (rule(), {**r, 'extra': True}, {**r, 'schema_version': True}):
            with self.assertRaises(ValueError):
                normalize_rule(invalid)


class CleaningAPITests(APITest):
    def ids(self, count=2):
        return [i['image_id'] for i in self.import_inputs(count)['items']]

    def view(self, i):
        return self.request('GET', self.p+f'/annotations/{i}')

    def edit(self, i, tags):
        v = self.view(i)
        return self.request('PUT', self.p+f'/annotations/{i}/manual',
                            {'tags': [{'tag': t} for t in tags], 'read_set': v['refs']}, version=v['version'])

    def preview(self, r=None, scope=None):
        return self.request('POST', self.p+'/rules/preview', {'rule': r or rule(group('smile'), group('curated'), group('blue_hair')),
                                                             'scope': scope or {'filter': {}}})

    def apply(self, preview, status=200, path='/rules/apply'):
        return self.request('POST', self.p+path, {'preview_id': preview['preview_id'], 'digest': preview['digest']},
                            version=preview['version'], status=status)

    def test_rule_apply_log_undo_and_export(self):
        ids = self.ids()
        preview = self.preview()
        self.assertEqual((preview['total'], preview['changed_count']), (2, 2))
        applied = self.apply(preview)
        self.assertEqual(applied['affected_count'], 2)
        self.assertEqual(self.apply(preview), applied)
        log = self.request('GET', self.p+'/operation-log')['items']
        self.assertEqual(len(log), 1)
        detail = self.request('GET', self.p+f"/operation-log/{log[0]['op_id']}?limit=1")
        self.assertTrue(detail['has_more'])
        self.assertEqual(len(self.request('GET', self.p+f"/operation-log/{log[0]['op_id']}?cursor={detail['next_cursor']}")['items']), 1)
        self.keep(ids)
        out = self.run_export(self.export_preview())
        report = self.request('GET', out['report_url'])
        self.assertEqual(next((self.root/report['directory']).glob('*.txt')).read_text(encoding='utf-8'), 'http style, blue hair, curated\n')
        undone = self.request('POST', self.p+f"/operation-log/{log[0]['op_id']}/undo", {}, version=log[0]['version'])
        self.assertEqual(undone['status'], 'undone')
        self.assertEqual([t['tag'] for t in self.view(ids[0])['tags']], ['blue hair', 'smile'])

    def test_unmatched_candidates_are_in_full_read_set(self):
        ids = self.ids()
        self.edit(ids[1], ['unmatched'])
        preview = self.preview()
        self.assertEqual(preview['changed_count'], 1)
        self.edit(ids[1], ['unmatched', 'later'])
        self.apply(preview, status=409)
        self.assertEqual(self.request('GET', self.p+'/operation-log')['items'], [])
        self.assertEqual([t['tag'] for t in self.view(ids[0])['tags']], ['blue hair', 'smile'])

    def test_manual_condition_does_not_replace_original(self):
        ids = self.ids(1)
        self.edit(ids[0], ['manual condition', 'smile'])
        p = self.preview(rule(group('smile'), group('new'), group('manual condition')))
        self.assertEqual(p['changed_count'], 0)
        self.assertEqual(self.apply(p)['affected_count'], 0)
        self.assertEqual(self.request('GET', self.p+'/operation-log')['items'], [])

    def test_default_rule_scope_excludes_reject_explicit_scope_includes_it(self):
        ids = self.ids()
        v = self.request('GET', self.p+f'/images/{ids[1]}')
        self.request('POST', self.p+'/images/review', {'ids':[ids[1]], 'target':'reject','expected_versions':{str(ids[1]):v['version']}})
        self.assertEqual(self.preview()['total'], 1)
        self.assertEqual(self.preview(scope={'selected_ids':ids})['total'], 2)
        self.request('POST', self.p+'/rules/preview', {'rule':rule(write=group('new')),'scope':{'selected_ids':ids,'filter':{}}}, status=422)

    def test_later_manual_prevents_undo_and_latest_first(self):
        ids = self.ids(1)
        a = self.apply(self.preview())
        b = self.apply(self.preview(rule(write=group('second'))))
        logs = self.request('GET', self.p+'/operation-log')['items']
        self.request('POST', self.p+f"/operation-log/{a['op_id']}/undo", {}, version=logs[0]['version'], status=409)
        self.request('POST', self.p+f"/operation-log/{b['op_id']}/undo", {}, version=logs[1]['version'])
        self.edit(ids[0], ['later'])
        error=self.request('POST', self.p+f"/operation-log/{a['op_id']}/undo", {}, version=logs[0]['version'], status=409)
        self.assertEqual(error['error']['details']['image_id'],ids[0])
        self.assertEqual(error['error']['details']['source'],'manual')

    def test_rule_batch_rollback_on_second_write(self):
        ids = self.ids()
        p = self.preview()
        count = 0
        def fault(stage):
            nonlocal count
            if stage == 'rule_after_annotation':
                count += 1
                if count == 2:
                    raise OSError('injected')
        self.runtime.context.handle._fault_hook = fault
        self.apply(p, status=503)
        self.runtime.context.handle._fault_hook = None
        self.assertEqual(self.request('GET', self.p+'/operation-log')['items'], [])
        self.assertTrue(all(self.view(i)['layers']['rule/tags'] is None for i in ids))

    def test_bulk_preview_stale_atomic_and_retry(self):
        ids = self.ids()
        body = {'scope':{'filter':{}}, 'add':['NEW_TAG'], 'remove':['smile']}
        p = self.request('POST', self.p+'/annotations/manual/bulk/preview', body)
        self.edit(ids[1], ['later'])
        self.apply(p, path='/annotations/manual/bulk/apply', status=409)
        self.assertIsNone(self.view(ids[0])['layers']['manual/tags'])
        p = self.request('POST', self.p+'/annotations/manual/bulk/preview', body)
        result = self.apply(p, path='/annotations/manual/bulk/apply')
        self.assertEqual(self.apply(p,path='/annotations/manual/bulk/apply'), result)
        self.assertEqual(result['total'], 2)
        stats = self.request('POST', self.p+'/annotations/stats', {'scope':{'filter':{}}})
        self.assertEqual(stats['items'][0]['tag'], 'new tag')
        self.assertEqual(stats['items'][0]['count'], 2)

    def test_catalog_change_invalidates_rule_preview(self):
        self.ids(1)
        p = self.preview()
        version = self.runtime.catalog.status()['version']
        self.runtime.catalog.put_translation({'tag':'blue_hair','value':'蓝发'},version,str(uuid4()))
        self.apply(p, status=409)

    def test_frozen_scope_excludes_later_images_and_preview_expires(self):
        ids = self.ids(1)
        p = self.preview()
        self.make_input('later.png', color='green')
        imp = self.import_preview()
        self.wait_operation(self.request('POST', self.p+'/imports', {'preview_id':imp['preview_id'],'digest':imp['digest']},status=202))
        self.assertEqual(self.apply(p)['affected_count'], 1)
        items=self.request('GET',self.p+'/images')['items']
        later=next(i['image_id'] for i in items if i['image_id'] not in ids)
        self.assertIsNone(self.view(later)['layers']['rule/tags'])
        p=self.preview()
        entry=self.runtime.previews.values[p['preview_id']]
        self.runtime.previews.values[p['preview_id']]=(0,*entry[1:])
        self.apply(p,status=410)

    def test_empty_batch_and_oversize_scope_never_write(self):
        p=self.preview(scope={'selected_ids':[]})
        self.assertEqual(self.apply(p)['affected_count'],0)
        self.request('POST',self.p+'/rules/preview',{'rule':rule(write=group('new')),'scope':{'selected_ids':list(range(1,5002))}},status=413)


class CleaningServiceTests(DatabaseTest):
    def setUp(self):
        super().setUp()
        self.runtime=Runtime()
        self.addCleanup(self.runtime.shutdown)
        self.context=SimpleNamespace(handle=self.project,project_id=str(uuid4()))

    def test_empty_original_does_not_fall_back_but_missing_original_does(self):
        empty=self.image(tag_values=())
        missing=self.image(originals=())
        neither=self.image(originals=())
        for image in (empty,missing):
            self.model_result(image.image_id, ('blue_hair',))
        p=self.runtime.cleaning.preview(self.context,{'rule':rule(write=group('new'),condition=group('blue_hair')),
            'scope':{'selected_ids':[empty.image_id,missing.image_id,neither.image_id]}})
        self.assertEqual(p['changed_count'],1)
        self.assertEqual(p['wd_fallback_count'],1)
        self.assertEqual(p['missing_original_count'],2)
        self.assertEqual([x['condition_source'] for x in p['examples']],['original','wd',None])
        self.assertEqual(p['examples'][0]['reason'],'condition:not_matched')

    def test_model_evidence_survives_unrelated_rule_and_undo_b_then_a(self):
        image=self.image(tag_values=('smile',))
        self.model_result(image.image_id,(Tag('blue hair',.31),))
        def apply(r):
            p=self.runtime.cleaning.preview(self.context,{'rule':r,'scope':{'selected_ids':[image.image_id]}})
            return self.runtime.cleaning.apply(self.context,p,p['version'])
        a=apply(rule(write=group('first'))); b=apply(rule(write=group('second')))
        from stylelora.project_db.merge import merge_caption
        # A rule snapshot must not turn low-confidence model tags into manual evidence.
        from stylelora.project_db.types import ExportSchemeInput
        config={**ExportSchemeInput('test').caption_config_json,'wd_min_confidence':.5}
        merged=self.read(merge_caption,image.image_id,config=config)
        self.assertNotIn('blue hair',merged.tags)
        for op in (b,a):
            row=self.runtime.cleaning.detail(self.context,op['op_id'])
            self.assertEqual(self.runtime.cleaning.undo(self.context,op['op_id'],row['version'])['status'],'undone')

