"""v4.3.1 source-derived schemas and synthetic values; NOT live acceptance.

Provenance: docs/57-F02-Launcher官方接口与只读接入验收.md.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from uuid import uuid4

import httpx

from stylelora.backends.nai_launcher.client import McpClient
from stylelora.backends.nai_launcher.compatibility import REVIEWED_CONTRACTS
from stylelora.backends.nai_launcher.readonly import inspect
from stylelora.backends.types import BackendError
from .api_support import APITest
from .mcp_support import MockMcp, TOKEN, info

# Input objects transcribed from generation_tool_definitions.dart at 60b21293.
# Runtime values below are invented for testing, not captured from Launcher.
SCHEMAS = [{'name': name, 'inputSchema': {'type': 'object', 'properties': {}, 'required': []}}
           for name in ('get_generation_settings', 'get_generation_status')]
SETTINGS = dict(model='nai-diffusion-4-5-full', effort=None, model_mode='anime',
                available_models=[{'id': 'nai-diffusion-4-5-full', 'name': 'Display title'}],
                sampler='k_euler_ancestral', steps=28, scale=5.0, cfg_rescale=0.0,
                noise_schedule='native', uc_preset=3, quality_toggle=False,
                variety_plus=False, decrisp=False, smea=False, smea_dyn=False,
                width=832, height=1216, seed=123, n_samples=1,
                prompt='private prompt', private_path='C:/private/test.png', api_key=TOKEN)
STATUS = {'generation': {'status': 'idle', 'progress': 0, 'image': '0/1',
                         'error': TOKEN, 'recent_images': [{'path': 'C:/private/test.png'}]},
          'queue': {'pending': 0, 'completed': 2, 'failed': 1, 'execution': 'idle', 'session_progress': 0}}


class SourceFixture(MockMcp):
    def __init__(self):
        super().__init__()
        self.tools = deepcopy(SCHEMAS)
        self.settings, self.status = deepcopy(SETTINGS), deepcopy(STATUS)
        self.version, self.name = '4.3.1', 'nai-launcher'
        self.protocol = '2025-11-25'
        self.tool_handler = self.reply
        self.text = False

    async def reply(self, request, body):
        payload = self.settings if body['params']['name'] == 'get_generation_settings' else self.status
        result = {'content': [{'type': 'text', 'text': json.dumps(payload)}]} if self.text else {'structuredContent': payload}
        return self.result(body, result)

    async def __call__(self, request):
        if request.method != 'DELETE' and json.loads(request.content)['method'] == 'initialize':
            body = json.loads(request.content)
            self.requests.append((request, body))
            self.initializations += 1
            return self.result(body, {'protocolVersion': self.protocol, 'capabilities': {'tools': {}},
                                     'serverInfo': {'name': self.name, 'version': self.version}},
                               headers={'Mcp-Session-Id': 'session-' + str(self.initializations)})
        return await super().__call__(request)

    def client(self, **kwargs):
        return McpClient(discovery=lambda: replace(info(), app_version='4.3.1'),
                         transport=httpx.MockTransport(self), backoff=0, **kwargs)

    def calls(self):
        return [b['params']['name'] for _, b in self.requests if b['method'] == 'tools/call']


class ReadonlyClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock = SourceFixture()
        self.client = self.mock.client()
        self.addAsyncCleanup(self.client.close)

    async def test_two_reads_without_production_write_authorization(self):
        value = await inspect(self.client)
        self.assertEqual(self.mock.calls(), ['get_generation_settings', 'get_generation_status'])
        self.assertEqual(value['settings']['uc_preset'], 3)
        self.assertEqual(value['activity']['queue']['completed'], 2)
        self.assertFalse(value['generation_enabled'])
        self.assertFalse(self.client.compatibility.accepted)
        self.assertEqual(REVIEWED_CONTRACTS, ())
        for name in ('submit_generation', 'update_generation_settings', 'get_generation_settings'):
            with self.assertRaisesRegex(BackendError, 'incompatible_schema'):
                await self.client.call_tool(name, {})
        self.assertEqual(len(self.mock.calls()), 2)

    async def test_readonly_entry_rejects_every_other_tool_before_connect(self):
        for name in ('submit_generation', 'update_generation_settings', 'prepare_generation', 'get_generation_history', 'arbitrary'):
            with self.assertRaisesRegex(BackendError, 'unsupported_tool'):
                await self.client.call_readonly(name)
        self.assertEqual(self.mock.requests, [])

    async def test_unreviewed_server_version_denied(self):
        self.mock.version = '4.3.2'
        with self.assertRaisesRegex(BackendError, 'readonly_version_unreviewed'):
            await inspect(self.client)
        self.assertEqual(self.mock.calls(), [])

    async def test_unreviewed_discovery_version_denied(self):
        self.client.discovery = info
        with self.assertRaisesRegex(BackendError, 'readonly_version_unreviewed'):
            await inspect(self.client)
        self.assertEqual(self.mock.calls(), [])

    async def test_other_server_name_denied(self):
        self.mock.name = 'other'
        with self.assertRaisesRegex(BackendError, 'readonly_version_unreviewed'):
            await inspect(self.client)
        self.assertEqual(self.mock.calls(), [])

    async def test_older_protocol_denied(self):
        self.mock.protocol = '2025-06-18'
        with self.assertRaisesRegex(BackendError, 'readonly_version_unreviewed'):
            await inspect(self.client)
        self.assertEqual(self.mock.calls(), [])

    async def test_both_schemas_checked_before_any_read(self):
        self.mock.tools[1]['inputSchema']['properties']['extra'] = {'type': 'string'}
        with self.assertRaisesRegex(BackendError, 'readonly_schema_mismatch'):
            await inspect(self.client)
        self.assertEqual(self.mock.calls(), [])

    async def test_output_schema_change_denied(self):
        self.mock.tools[0]['outputSchema'] = {'type': 'object'}
        with self.assertRaisesRegex(BackendError, 'readonly_schema_mismatch'):
            await inspect(self.client)

    async def test_missing_tool_denied(self):
        self.mock.tools.pop()
        with self.assertRaisesRegex(BackendError, 'readonly_schema_mismatch'):
            await inspect(self.client)
        self.assertEqual(self.mock.calls(), [])

    async def test_unrelated_tools_do_not_authorize_writes(self):
        self.mock.tools.append({'name': 'submit_generation', 'inputSchema': {'type': 'object'}})
        await inspect(self.client)
        with self.assertRaisesRegex(BackendError, 'incompatible_schema'):
            await self.client.call_tool('submit_generation', {})
        self.assertEqual(len(self.mock.calls()), 2)

    async def test_only_approved_fields_projected(self):
        value = await inspect(self.client)
        serialized = json.dumps(value)
        for excluded in (TOKEN, 'private', 'seed', 'recent_images', 'Display title', 'n_samples'):
            self.assertNotIn(excluded, serialized)

    async def test_text_result_supported(self):
        self.mock.text = True
        self.assertEqual((await inspect(self.client))['status'], 'ready')

    async def test_effort_models_and_locks_supported(self):
        self.mock.settings.update(effort='high', locked_by_effort=['steps', 'negative_prompt'],
                                  available_models=[{'id': 'nai-diffusion-5', 'efforts': ['medium', 'high']}])
        self.assertEqual((await inspect(self.client))['settings']['effort'], 'high')

    async def test_invalid_settings_never_published(self):
        for key, invalid in (('steps', True), ('width', -1), ('effort', 'unknown'), ('model', '../secret'),
                             ('uc_preset', None), ('available_models', []), ('smea', 0),
                             ('locked_by_effort', ['api_key']), ('scale', 1001)):
            with self.subTest(key=key):
                self.mock.settings = {**deepcopy(SETTINGS), key: invalid}
                with self.assertRaisesRegex(BackendError, 'readonly_invalid_response'):
                    await inspect(self.client)

    async def test_invalid_status_never_published(self):
        self.mock.status['generation']['progress'] = 101
        with self.assertRaisesRegex(BackendError, 'readonly_invalid_response'):
            await inspect(self.client)

    async def test_token_in_allowed_field_is_rejected(self):
        self.mock.settings['model'] = TOKEN
        with self.assertRaisesRegex(BackendError, 'readonly_invalid_response'):
            await inspect(self.client)

    async def test_reconnect_checks_new_contract_before_retry(self):
        async def expire(request, body):
            self.mock.version = '4.3.2'
            return httpx.Response(404)
        self.mock.tool_handler = expire
        with self.assertRaisesRegex(BackendError, 'readonly_version_unreviewed'):
            await inspect(self.client)
        self.assertEqual(len(self.mock.calls()), 1)
        self.assertEqual(self.mock.initializations, 2)

    async def test_mixed_sessions_are_discarded(self):
        async def once(request, body):
            self.mock.tool_handler = self.mock.reply
            return httpx.Response(404)
        self.mock.tool_handler = once
        with self.assertRaisesRegex(BackendError, 'inspection_session_changed'):
            await inspect(self.client)
        self.assertEqual(self.mock.initializations, 2)

    async def test_read_retry_is_bounded(self):
        async def unavailable(request, body):
            return httpx.Response(503)
        self.mock.tool_handler = unavailable
        with self.assertRaisesRegex(BackendError, 'mcp:http_error'):
            await inspect(self.client)
        self.assertEqual(len(self.mock.calls()), 4)


class ReadonlyAPITests(APITest):
    def setUp(self):
        super().setUp()
        self.mock = SourceFixture()
        self.runtime.backends.client_factory = self.mock.client

    def run_inspection(self, **kwargs):
        return self.wait_operation(self.request('POST', '/api/backends/nai_launcher/inspect', {}, status=202, **kwargs))['result']

    def test_explicit_inspection_cached_and_idempotent(self):
        self.assertIsNone(self.request('GET', '/api/backends')['inspection'])
        self.assertEqual(self.mock.requests, [])
        key = str(uuid4())
        value = self.run_inspection(key=key)
        self.assertEqual(value['status'], 'ready')
        self.assertEqual(self.run_inspection(key=key), value)
        self.assertEqual(len(self.mock.calls()), 2)
        state = self.request('GET', '/api/backends')
        self.assertEqual(state['inspection'], value)
        self.assertEqual(state['items'][0]['status'], 'not_checked')
        self.assertFalse(state['items'][0]['available'])
        self.request('GET', '/api/backends/nai_launcher/capabilities', status=503)

    def test_strict_body_query_backend_and_auth(self):
        endpoint = '/api/backends/nai_launcher/inspect'
        self.request('POST', endpoint, {'name': 'submit_generation'}, status=422)
        self.request('POST', endpoint + '?token=secret', {}, status=422)
        self.request('POST', '/api/backends/other/inspect', {}, status=404)
        self.assertEqual(self.client.post(endpoint, json={}).status_code, 428)
        self.assertEqual(self.client.post(endpoint, json={}, headers={'Authorization': 'Bearer wrong'}).status_code, 401)
        self.assertEqual(self.mock.requests, [])

    def test_failure_replaces_stale_success_without_secret(self):
        self.run_inspection()
        self.mock.settings['steps'] = False
        value = self.run_inspection()
        self.assertEqual(value['reason'], 'backend:readonly_invalid_response')
        self.assertNotIn('settings', value)
        self.assertNotIn(TOKEN, json.dumps(self.request('GET', '/api/backends')))
        self.assertNotIn(TOKEN, json.dumps(list(self.runtime.events._events)))

    def test_cancel_and_singleflight_keep_previous_snapshot(self):
        previous = self.run_inspection()
        started = threading.Event()
        async def slow(request, body):
            started.set()
            await asyncio.sleep(10)
            return await self.mock.reply(request, body)
        self.mock.tool_handler = slow
        accepted = self.request('POST', '/api/backends/nai_launcher/inspect', {}, status=202)
        self.assertTrue(started.wait(2))
        second = self.request('POST', '/api/backends/nai_launcher/inspect', {}, status=202)
        self.assertEqual(second['status_url'], accepted['status_url'])
        current = self.request('GET', accepted['status_url'])
        self.request('POST', accepted['status_url'] + '/cancel', {}, version=current['version'])
        self.wait_operation(accepted, expected='canceled')
        self.assertEqual(self.request('GET', '/api/backends')['inspection'], previous)
        self.assertEqual(self.mock.deletes, 2)

    def test_loopback_http_uses_only_two_reads_and_closes_session(self):
        fixture = self.mock
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                content = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                request = httpx.Request(self.command, 'http://127.0.0.1/mcp', content=content, headers=dict(self.headers))
                if request.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    self.send_error(401)
                    return
                reply = asyncio.run(fixture(request))
                self.send_response(reply.status_code)
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(reply.content)
            do_POST = do_DELETE = handle_request
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        self.runtime.backends.client_factory = partial(McpClient, discovery=lambda: replace(info(server.server_port), app_version='4.3.1'))
        self.assertEqual(self.run_inspection()['status'], 'ready')
        self.assertEqual(fixture.calls(), ['get_generation_settings', 'get_generation_status'])
        self.assertEqual(fixture.deletes, 1)
