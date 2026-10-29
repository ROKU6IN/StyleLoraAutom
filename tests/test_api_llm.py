import json
import threading

import httpx

from .api_support import APITest
from .test_llm import reply


class LlmAPITests(APITest):
    def create_endpoint(self, **values):
        return self.request('POST', '/api/llm/endpoints', {'base_url': 'http://127.0.0.1:19283/v1',
            'protocol': 'openai_chat', 'models': [{'name': 'example/model'}], **values}, status=201)

    def with_token(self, ep):
        path = '/api/llm/endpoints/' + ep['endpoint_id']
        token = self.request('POST', path + '/tokens', {'value': 'api-test-secret'}, status=201)
        return path, token, self.request('GET', path)

    def test_full_config_http_cas_noop_roles_and_delete(self):
        ep = self.create_endpoint()
        path, token, ep = self.with_token(ep)
        self.assertNotIn('api-test-secret', json.dumps(ep))
        self.assertTrue(ep['tokens'][0]['effective_available'])
        roles = self.request('GET', '/api/llm/roles')
        value = {'roles': {'prompt': {'endpoint_id': ep['endpoint_id'], 'model': 'example/model'}}}
        self.request('PUT', '/api/llm/roles', value, status=428)
        bound = self.request('PUT', '/api/llm/roles', value, version=roles['version'])
        self.assertTrue(bound['availability']['prompt'])
        self.request('DELETE', path, version=ep['version'], status=409)
        self.request('PUT', '/api/llm/roles', {'roles': {}}, version=bound['version'])
        self.request('DELETE', path + '/tokens/' + token['token_ref'], version=ep['version'])
        fresh = self.request('GET', path)
        self.request('DELETE', path, version=fresh['version'], status=204)

    def test_auth_errors_and_strict_secret_input_never_echo(self):
        ep = self.create_endpoint()
        path, token, ep = self.with_token(ep)
        self.runtime.llm.transport = httpx.MockTransport(lambda r: httpx.Response(401, text='api-test-secret'))
        task = self.request('POST', path + '/models/probe', {}, version=ep['version'], status=202)
        result = self.wait_operation(task, expected='failed')
        self.assertEqual(result['error']['code'], 'llm:unauthorized')
        self.assertNotIn('api-test-secret', json.dumps(result))
        self.assertTrue(self.request('GET', '/api/session')['capabilities']['llm_config'])
        self.request('POST', path + '/models/probe', {'unexpected': True}, version=ep['version'], status=422)
        error = self.request('POST', path + '/tokens', {'value': 'private\nheader'}, status=422)
        self.assertNotIn('private', json.dumps(error))
        self.assertEqual(self.client.get('/api/llm/roles', headers={'Authorization': ''}).status_code, 401)

    def test_models_probe_preserves_manual_and_uses_explicit_modalities(self):
        ep = self.create_endpoint(models=[{'name': 'example/model', 'supports_images': True}])
        path, _, ep = self.with_token(ep)
        self.runtime.llm.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [
            {'id': 'example/model'}, {'id': 'vision-in-name'}, {'id': 'explicit', 'input_modalities': ['text', 'image']}]}))
        task = self.request('POST', path + '/models/probe', {}, version=ep['version'], status=202)
        self.wait_operation(task)
        models = {m['name']: m for m in self.request('GET', path)['models']}
        self.assertTrue(models['example/model']['supports_images'])
        self.assertEqual(models['example/model']['capability_source'], 'manual')
        self.assertFalse(models['vision-in-name']['supports_images'])
        self.assertTrue(models['explicit']['supports_images'])

    def test_image_probe_requires_features_not_200(self):
        ep = self.create_endpoint()
        path, _, ep = self.with_token(ep)
        self.runtime.llm.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=reply(text=json.dumps(
            {'left_color': 'green', 'left_shape': 'square', 'right_color': 'blue', 'right_shape': 'circle'}))))
        task = self.request('POST', path + '/models/test-image', {'model': 'example/model'}, version=ep['version'], status=202)
        result = self.wait_operation(task, expected='failed')
        self.assertEqual(result['error']['code'], 'llm:image_test_failed')
        self.assertEqual(self.request('GET', path), ep)
        self.runtime.llm.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=reply(text=json.dumps(
            {'left_color': 'red', 'left_shape': 'square', 'right_color': 'blue', 'right_shape': 'circle'}))))
        task = self.request('POST', path + '/models/test-image', {'model': 'example/model'}, version=ep['version'], status=202)
        self.wait_operation(task)
        model = self.request('GET', path)['models'][0]
        self.assertEqual(model['capability_source'], 'tested')
        self.assertTrue(model['supports_images'])

    def test_slow_probe_does_not_overwrite_newer_config(self):
        ep = self.create_endpoint()
        path, _, ep = self.with_token(ep)
        started, release = threading.Event(), threading.Event()
        def handler(r):
            started.set()
            release.wait(5)
            return httpx.Response(200, json={'data': [{'id': 'new-from-probe'}]})
        self.runtime.llm.transport = httpx.MockTransport(handler)
        task = self.request('POST', path + '/models/probe', {}, version=ep['version'], status=202)
        self.assertTrue(started.wait(5))
        try:
            updated = self.request('PUT', path, {'base_url': ep['base_url'], 'protocol': 'openai_responses', 'models': []}, version=ep['version'])
        finally:
            release.set()
        result = self.wait_operation(task, expected='failed')
        self.assertEqual(result['error']['code'], 'concurrency:version_conflict')
        self.assertEqual(self.request('GET', path), updated)

    def test_paging_stale_and_post_idempotency(self):
        from uuid import uuid4
        key = str(uuid4())
        body = {'base_url': 'http://127.0.0.1/v1', 'protocol': 'openai_chat'}
        a = self.request('POST', '/api/llm/endpoints', body, status=201, key=key)
        b = self.request('POST', '/api/llm/endpoints', body, status=201, key=key)
        self.assertEqual(a, b)
        self.create_endpoint()
        first = self.request('GET', '/api/llm/endpoints?limit=1')
        self.assertTrue(first['has_more'])
        self.create_endpoint()
        self.request('GET', '/api/llm/endpoints?limit=1&cursor=' + first['next_cursor'], status=409)

    def test_templates_and_unimplemented_vision_not_claimed(self):
        value = self.request('GET', '/api/llm/templates')
        self.assertTrue(value['items'][0]['available'])
        self.assertTrue(next(t for t in value['items'] if t['id'] == 'R2')['available'])
        self.assertFalse(self.request('GET', '/api/session')['capabilities']['llm_vision'])

    def test_client_cannot_forge_test_provenance(self):
        sub, _ = self.runtime.events.subscribe(topics=('llm',))
        self.addCleanup(lambda: self.runtime.events.unsubscribe(sub))
        value = self.create_endpoint(models=[{'name': 'fake', 'supports_images': True, 'capability_source': 'tested', 'tested_at': 'forged'}])
        self.assertEqual(value['models'][0]['capability_source'], 'manual')
        self.assertIsNone(value['models'][0]['tested_at'])
        event = sub.queue.get(timeout=1)
        self.assertEqual(event['type'], 'llm.changed')
        self.assertEqual(set(event['data']['payload']), {'revision'})
