from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from stylelora.backends.nai_direct.form import form_capabilities
from stylelora.backends.nai_direct.profile import DEFAULT_CONFIG, capabilities, CONTRACT
from stylelora.generation.profile import validate_capabilities
from .api_support import APITest


class GenerationParameterTests(APITest):
    def draft(self):
        return {'name': 'Parameter form', 'config': deepcopy(DEFAULT_CONFIG), 'style_prompt': '',
                'negative_prompt': '', 'target_count': 1, 'cost_policy': 'ask'}

    def test_form_is_local_and_does_not_change_receipt_contract(self):
        with patch.object(self.runtime.nai_direct, 'available', side_effect=AssertionError('No credential read')):
            result = self.request('GET', '/api/backends/nai_direct/capabilities')
        self.assertEqual(result['form']['default_config'], DEFAULT_CONFIG)
        self.assertEqual(result['contract_digest'], CONTRACT)
        core = {key: value for key, value in result.items() if key != 'form'}
        self.assertEqual(core, capabilities())
        self.assertEqual(validate_capabilities(DEFAULT_CONFIG, '', core), validate_capabilities(DEFAULT_CONFIG, '', capabilities()))
        result['form']['default_config']['profile']['steps'] = 1
        self.assertEqual(form_capabilities()['form']['default_config']['profile']['steps'], 23)

    def test_browser_fixture_matches_published_capabilities(self):
        fixture = Path(__file__).resolve().parents[1] / 'web/tests/fixtures/nai-direct-capabilities.json'
        self.assertEqual(json.loads(fixture.read_text(encoding='utf-8')), form_capabilities())

    def test_create_rejects_unsupported_values_with_safe_field_issues(self):
        body = self.draft()
        body['config']['profile'].update(steps=51, sampler='private-submitted-value')
        error = self.request('POST', self.p + '/sources', body, status=422)['error']
        self.assertEqual(error['code'], 'generation:unsupported_configuration')
        self.assertEqual({issue['field'] for issue in error['issues']}, {'config.profile.steps', 'config.profile.sampler'})
        self.assertNotIn('private-submitted-value', json.dumps(error))
        self.assertEqual(self.request('GET', self.p + '/sources')['items'], [])

    def test_update_rejection_preserves_source_and_cas(self):
        body = self.draft()
        source = self.request('POST', self.p + '/sources', body, status=201)
        body['config']['profile']['scale'] = 11
        url = self.p + '/sources/' + str(source['source_id'])
        self.request('PUT', url, body, version=source['version'], status=422)
        current = self.request('GET', url)
        self.assertEqual(current['config_json'], source['config_json'])
        self.assertEqual(current['version'], source['version'])
        body['config']['profile']['scale'] = 6
        updated = self.request('PUT', url, body, version=source['version'])
        self.assertNotEqual(updated['version'], source['version'])
        self.request('PUT', url, body, version=source['version'], status=409)

    def test_valid_models_and_numeric_boundaries_save_without_a_token(self):
        with patch.object(self.runtime.nai_direct, 'available', side_effect=AssertionError('No credential read')):
            for model in ('nai-diffusion-4-5-full', 'nai-diffusion-5-full'):
                body = self.draft()
                body['config']['profile'].update(model=model, steps=50, scale=1, cfg_rescale=0)
                body['config']['sizes']['square'] = {'width': 1536, 'height': 1536}
                source = self.request('POST', self.p + '/sources', body, status=201)
                self.assertEqual(source['config_json'], body['config'])

    def test_dimension_range_and_multiple_are_both_enforced(self):
        for width, field in [(1600, 'config.sizes.landscape.width'), (777, 'body.config.sizes.landscape.width')]:
            body = self.draft()
            body['config']['sizes']['landscape']['width'] = width
            error = self.request('POST', self.p + '/sources', body, status=422)['error']
            self.assertIn(field, {issue['field'] for issue in error['issues']})

    def test_legacy_launcher_draft_is_not_converted_or_validated_as_direct(self):
        body = self.draft()
        body['config'].update(backend='nai_launcher')
        body['config']['profile'].update(model='legacy-model', steps=60)
        source = self.request('POST', self.p + '/sources', body, status=201)
        self.assertEqual(source['config_json'], body['config'])
