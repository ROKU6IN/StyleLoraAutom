from copy import deepcopy
import json
import random
import unittest
from unittest.mock import patch
from uuid import uuid4

from stylelora.prompt_production.sampling import cell_id, quotas
from . import test_prompt_production as support


class CoverageEditorTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait

    def editor(self, **plan):
        return self.request('POST', '/coverage-plans/editor', {'format': 'json', 'content': json.dumps({
            'name': '表单测试', 'plan': {'target_count': 12, 'random_seed': 42, **plan}})})

    def preview(self, form, status=200):
        return self.request('POST', '/coverage-plans/editor/preview', form, status=status)

    def test_lossless_numeric_roundtrip_and_zero_bounds(self):
        value = self.editor(random_seed=2**64-1, constraints=[{'dimension': 'count', 'tag': 'no humans',
                            'minimum_ratio': .15, 'maximum_count': 0}])
        self.assertEqual(value['form']['plan']['random_seed'], '18446744073709551615')
        self.assertEqual(value['form']['plan']['constraints'][0]['maximum_count'], '0')
        self.assertEqual(value['form']['plan']['constraints'][0]['maximum_ratio'], '')
        self.assertEqual(self.preview(value['form']), value)
        self.assertEqual(json.loads(value['template'])['plan']['random_seed'], 2**64-1)
        self.assertIn('coverage:contradictory_constraints', value['layout']['issues'])
        self.assertEqual(value['layout']['constraint_bounds']['no humans'], [2, 0])

    def test_quota_preview_matches_actual_sampling_and_does_not_mutate_snapshots(self):
        key = cell_id(['scene', 'lighting'], ['forest', 'sunlight'])
        view = self.editor(cell_weights={key: 7}, target_count=31)
        form = deepcopy(view['form'])
        form['plan']['cell_weights'][key] = '3'
        form['plan']['constraints'] = [{'dimension': 'view', 'tag': 'wide shot', 'minimum_ratio': '0.1',
                                     'maximum_ratio': '1', 'minimum_count': '4', 'maximum_count': '20'}]
        changed = self.preview(form)
        plan = json.loads(changed['template'])['plan']
        expected, valid = quotas(plan, random.Random(42))
        self.assertTrue(valid)
        self.assertEqual(changed['layout']['cell_targets'], expected)
        self.assertEqual(sum(expected.values()), 31)
        self.assertEqual(changed['layout']['constraint_bounds']['wide shot'], [4, 20])
        request = self.sampling(self.dictionary(), plan=plan)
        preview = self.request('POST', '/coverage/preview', request)
        self.assertEqual(preview['cell_targets'], expected)
        task, _ = self.start(request)
        self.wait(task['task_id'])
        self.assertEqual(self.service.load(task['task_id'])['snapshot']['request']['plan'], plan)
        form['plan']['target_count'] = '40'
        self.preview(form)
        self.assertEqual(self.service.load(task['task_id'])['snapshot']['request']['plan'], plan)

    def test_no_seed_no_sampling_no_writes_or_receipts(self):
        with patch('stylelora.prompt_production.tasks.sample', side_effect=AssertionError('must not sample')):
            value = self.editor(random_seed=None)
            self.assertIsNone(value['layout']['cell_targets'])
            self.assertTrue(value['layout']['seed_required'])
            self.assertEqual(value['form']['plan']['random_seed'], '')
            self.assertEqual(self.preview(value['form']), value)
        self.assertEqual(self.service.files.ids('coverage'), [])
        self.assertEqual(self.service.list(), [])

    def test_exclusions_use_primary_tags_only_and_keep_invalid_weight_explicit(self):
        key = cell_id(['scene', 'lighting'], ['forest', 'sunlight'])
        value = self.editor(exclude=[{'all_tags': ['forest', 'sunlight']}], cell_weights={key: 2})
        self.assertNotIn(key, value['layout']['active_cells'])
        self.assertIn('coverage:excluded_cell_weight', value['layout']['issues'])
        self.assertIsNone(value['layout']['cell_targets'])
        form = value['form']
        form['plan']['cell_weights'] = {}
        changed = self.preview(form)
        self.assertEqual(len(changed['layout']['cell_targets']), 5)
        self.assertEqual(sum(changed['layout']['cell_targets'].values()), 12)
        external = self.editor(exclude=[{'all_tags': ['1girl'], 'none_tags': ['forest']}])
        self.assertEqual(len(external['layout']['active_cells']), 6)
        self.assertEqual(external['form']['plan']['exclude'][0]['none_tags'], ['forest'])

    def test_layout_impossibilities_are_reported_without_relaxation(self):
        for patch_plan, issue in (({'target_count': 1}, 'coverage:target_below_cells'),
                                  ({'exclude': [{}]}, 'coverage:all_cells_excluded')):
            view = self.editor(**patch_plan)
            self.assertIn(issue, view['layout']['issues'])
            self.assertIsNone(view['layout']['cell_targets'])
            self.assertEqual(self.preview(view['form']), view)

    def test_reject_invalid_numeric_text_instead_of_coercing_or_rounding(self):
        base = self.editor()['form']
        for field, values in {'random_seed': ['-1', '18446744073709551616', '1.0', '1e3', ' 42 ', '１２'],
                              'target_count': ['', '0', '10001', '12.0', 'true']}.items():
            for value in values:
                form = deepcopy(base)
                form['plan'][field] = value
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.preview(form, 422)['error']['code'], 'coverage:invalid_editor')
        for value in ('NaN', 'Infinity', '-0.1', '1.1', '1e500', 'abc'):
            form = deepcopy(base)
            form['plan']['constraints'] = [{'dimension': 'view', 'tag': 'wide shot', 'minimum_ratio': value}]
            self.preview(form, 422)
        for value in (True, 42, None):
            form = deepcopy(base)
            form['plan']['random_seed'] = value
            self.preview(form, 422)

    def test_structural_errors_do_not_drop_fields_or_obsolete_weights(self):
        base = self.editor()['form']
        key = cell_id(['scene', 'lighting'], ['forest', 'sunlight'])
        patches = [{'cell_weights': {key: '0'}}, {'cell_weights': {'stale': '7'}},
                   {'primary_dimensions': ['scene', 'scene']}, {'no_humans_excludes': ['scene']},
                   {'cell_values': {'scene': ['forest', 'Forest'], 'lighting': ['night']}},
                   {'cell_values': {'scene': [str(i) for i in range(101)], 'lighting': [str(i) for i in range(100)]}},
                   {'unknown': 'preserve'}]
        for changes in patches:
            form = deepcopy(base)
            form['plan'].update(changes)
            self.preview(form, 422)
        form = deepcopy(base)
        form['unknown'] = True
        self.preview(form, 422)
        self.assertFalse(self.service.files.ids('coverage'))

    def test_strict_source_formats_and_authentication(self):
        for format, content in [('json', '{"name":"a","name":"b","plan":{}}'),
                                ('yaml', 'name: &n a\nplan: {x: *n}'),
                                ('yaml', 'name: a\nplan: {target_count: 1, target_count: 2}')]:
            self.request('POST', '/coverage-plans/editor', {'format': format, 'content': content}, status=422)
        body = {'format': 'yaml', 'content': 'name: YAML 精确\nplan:\n  random_seed: 18446744073709551615\n'}
        value = self.request('POST', '/coverage-plans/editor', body)
        self.assertEqual(value['form']['plan']['random_seed'], '18446744073709551615')
        for path, payload in [('/coverage-plans/editor', body), ('/coverage-plans/editor/preview', value['form'])]:
            self.request('POST', path + '?unknown=1', payload, status=422)
            self.assertEqual(self.client.post(self.p + path, json=payload).status_code, 401)
            headers = {**self.headers, 'Origin': 'https://example.invalid'}
            self.assertEqual(self.client.post(self.p + path, json=payload, headers=headers).status_code, 403)

    def test_three_dimensions_normalization_and_special_keys(self):
        names = ['场景', '__proto__', 'constructor']
        key = cell_id(names, ['林地', 'bright', 'far'])
        value = self.editor(primary_dimensions=names, cell_values={'场景': ['林地'], '__proto__': ['bright', 'dark'], 'constructor': ['far', 'near']},
                            cell_weights={key: 7})
        form = value['form']
        form['name'] = '  三维  '
        changed = self.preview(form)
        self.assertEqual(changed['name'], '三维')
        self.assertEqual(len(changed['layout']['cell_targets']), 4)
        self.assertEqual(changed['form']['plan']['cell_weights'], {key: '7'})

    def test_form_save_export_reopen_and_cas_receipts(self):
        view = self.editor(random_seed=2**64-1)
        form = view['form']
        form['name'] = '表单保存'
        form['plan']['target_count'] = '30'
        edited = self.preview(form)
        identifier, key = str(uuid4()), str(uuid4())
        document = {'format': 'json', 'content': edited['template']}
        body = {**document, 'coverage_id': identifier}
        created = self.request('POST', '/coverage-plans', body, key=key, status=201)
        self.assertEqual(self.request('POST', '/coverage-plans', body, key=key, status=201), created)
        path = '/coverage-plans/' + identifier
        exported = self.request('GET', path + '/export?format=yaml')
        reopened = self.request('POST', '/coverage-plans/editor', {'format': 'yaml', 'content': exported['content']})
        self.assertEqual(reopened['form'], edited['form'])
        form['plan']['random_seed'] = ''
        next_document = {'format': 'json', 'content': self.preview(form)['template']}
        self.request('PUT', path, next_document, version=created['version'])
        self.request('PUT', path, document, version=created['version'], status=409)
        self.assertIsNone(self.request('GET', path)['random_seed'])
