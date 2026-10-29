import asyncio
from functools import partial
import json
import threading
import time

import httpx

from stylelora.backends.nai_launcher.client import McpClient
from stylelora.backends.nai_launcher.discovery import discover
from .api_support import APITest
from .mcp_support import TOKEN, LocalMcpServer, MockMcp, info


class BackendAPITests(APITest):
    def test_cached_list_never_connects_and_gates_unverified_features(self):
        def must_not_connect():
            self.fail('GET must not create a network client')
        self.runtime.backends.client_factory = must_not_connect
        value = self.request('GET', '/api/backends')
        self.assertEqual(value['items'][0]['status'], 'not_checked')
        self.assertFalse(value['items'][0]['available'])
        failure = self.request('GET', '/api/backends/nai_launcher/capabilities', status=503)
        self.assertEqual(failure['error']['code'], 'backend:incompatible_schema')
        self.assertIsNone(self.request('GET', '/api/backends/nai_launcher/account-status'))
        self.request('GET', '/api/backends/arbitrary/capabilities', status=404)
        self.request('POST', '/api/backends/nai_launcher/probe', {'endpoint': 'http://example.com', 'token': 'secret'}, status=422)
        self.request('POST', '/api/backends/nai_launcher/tools/call', {'name': 'submit_generation'}, status=404)
        self.request('GET', '/api/backends?token=secret', status=422)

    def test_missing_discovery_returns_diagnostic_and_idempotent_operation(self):
        self.runtime.backends.client_factory = partial(McpClient, discovery=partial(discover, self.base / 'missing', attempts=1))
        from uuid import uuid4
        key = str(uuid4())
        accepted = self.request('POST', '/api/backends/nai_launcher/probe', {}, key=key, status=202)
        result = self.wait_operation(accepted)['result']
        self.assertEqual(result['reason'], 'discovery:not_running')
        self.assertFalse(result['reachable'])
        replay = self.request('POST', '/api/backends/nai_launcher/probe', {}, key=key, status=202)
        self.assertEqual(replay['status_url'], accepted['status_url'])
        self.assertEqual(self.request('GET', '/api/backends')['items'][0], result)

    def test_real_loopback_json_sse_paging_and_no_tool_calls(self):
        server = LocalMcpServer(self.base / 'launcher').start()
        self.addCleanup(server.close)
        self.runtime.backends.client_factory = partial(McpClient, discovery=partial(discover, server.appdata, environ={}))
        accepted = self.request('POST', '/api/backends/nai_launcher/probe', {}, status=202)
        view = self.wait_operation(accepted)['result']
        self.assertTrue(view['reachable'])
        self.assertFalse(view['transport_verified'])
        self.assertEqual(view['reason'], 'backend:unreviewed_schema')
        self.assertEqual(view['tool_count'], 8)
        self.assertEqual(len(view['schema_hash']), 64)
        self.assertEqual(server.methods, ['initialize', 'notifications/initialized', 'tools/list', 'tools/list', 'DELETE'])
        self.assertNotIn(TOKEN, json.dumps(view))
        self.assertNotIn(TOKEN, json.dumps(list(self.runtime.events._events)))
        self.assertEqual(self.request('GET', '/api/backends/nai_launcher/capabilities', status=503)['error']['code'], 'backend:incompatible_schema')
        server.mode = 'unauthorized'
        second = self.request('POST', '/api/backends/nai_launcher/probe', {}, status=202)
        failed = self.wait_operation(second)['result']
        self.assertEqual(failed['reason'], 'mcp:unauthorized')
        self.assertIsNone(failed['schema_hash'])
        self.assertFalse(failed['reachable'])

    def test_probe_singleflight_and_cancel_keeps_previous_status(self):
        started = threading.Event()
        mock = MockMcp()
        async def slow(request):
            started.set()
            await asyncio.sleep(10)
            return await mock(request)
        self.runtime.backends.client_factory = partial(McpClient, discovery=info, transport=httpx.MockTransport(slow))
        accepted = self.request('POST', '/api/backends/nai_launcher/probe', {}, status=202)
        self.assertTrue(started.wait(2))
        second = self.request('POST', '/api/backends/nai_launcher/probe', {}, status=202)
        self.assertEqual(accepted['status_url'], second['status_url'])
        current = self.request('GET', accepted['status_url'])
        self.request('POST', accepted['status_url'] + '/cancel', {}, version=current['version'])
        start = time.monotonic()
        self.wait_operation(accepted, expected='canceled')
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.request('GET', '/api/backends')['items'][0]['status'], 'not_checked')

    def test_account_context_does_not_invent_balance(self):
        self.assertIsNone(self.request('GET', '/api/backends/nai_launcher/account-status?project_id=' + self.pid))
        self.request('GET', '/api/backends/nai_launcher/account-status?source_id=1', status=422)
        self.import_inputs()
        source = self.request('GET', self.p + '/sources')['items'][0]
        self.request('GET', '/api/backends/nai_launcher/account-status?project_id=' + self.pid + '&source_id=' + str(source['source_id']), status=422)

    def test_backend_events_are_subscribable_and_credentials_not_persisted(self):
        subscriber, initial = self.runtime.events.subscribe(topics=('backend',))
        self.addCleanup(self.runtime.events.unsubscribe, subscriber)
        mock = MockMcp()
        self.runtime.backends.client_factory = partial(McpClient, discovery=info, transport=httpx.MockTransport(mock))
        self.wait_operation(self.request('POST', '/api/backends/nai_launcher/probe', {}, status=202))
        event = subscriber.queue.get(timeout=2)
        self.assertEqual(event['type'], 'backend.changed')
        self.assertNotIn(TOKEN, json.dumps(event))
        for path in self.root.rglob('*'):
            if path.is_file() and path.name != '.project.lock':
                self.assertNotIn(TOKEN.encode(), path.read_bytes())
