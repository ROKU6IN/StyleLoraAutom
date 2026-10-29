import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from stylelora.backends.nai_launcher.client import BODY_LIMIT, McpClient, body_bytes
from stylelora.backends.nai_launcher.compatibility import REVIEWED_CONTRACTS, schema_digest, verify_compatibility
from stylelora.backends.nai_launcher.discovery import PROTOCOLS, discover
from stylelora.backends.nai_launcher.sse import SSEDecoder
from stylelora.backends.types import BackendError, ToolSchema
from .mcp_support import TOKEN, TOOLS, MockMcp, Stream, contract, discovery_file, info
from .support import TEMP_ROOT


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_missing_partial_and_oversized(self):
        with self.assertRaisesRegex(BackendError, 'discovery:not_running'):
            discover(self.base, attempts=1)
        path = discovery_file(self.base)
        for raw in ('{', '{"port":1,"port":2}', 'x' * (64 * 1024 + 1)):
            path.write_text(raw)
            with self.assertRaisesRegex(BackendError, 'discovery:invalid_file'):
                discover(self.base, attempts=1)

    def test_token_precedence_and_safe_repr(self):
        discovery_file(self.base)
        value = discover(self.base, environ={})
        self.assertEqual(value.token, TOKEN)
        self.assertNotIn(TOKEN, repr(value))
        self.assertEqual(discover(self.base, environ={'NAI_LAUNCHER_MCP_TOKEN': 'environment-secret'}).token, 'environment-secret')
        self.assertEqual(discover(self.base, explicit_token='explicit-secret', environ={'NAI_LAUNCHER_MCP_TOKEN': 'environment-secret'}).token, 'explicit-secret')
        with self.assertRaisesRegex(BackendError, 'discovery:missing_token'):
            discover(self.base, explicit_token='', environ={})

    def test_reject_non_loopback_and_mismatched_discovery(self):
        for change in ({'endpoint': 'http://localhost:8123/mcp'}, {'endpoint': 'https://127.0.0.1:8123/mcp'},
                       {'endpoint': 'http://127.0.0.1:8123/mcp?token=secret'}, {'endpoint': 'http://127.0.0.1:9000/mcp'}, {'pid': True},
                       {'endpoint': 'http://127.0.0.1:8123/%2e/mcp'}, {'app_version': 'secret text'},
                       {'token': 'x\r\ny'}, {'protocol_versions': [PROTOCOLS[0], PROTOCOLS[0]]}):
            with self.subTest(change=tuple(change)):
                discovery_file(self.base, **change)
                with self.assertRaises(BackendError):
                    discover(self.base, environ={}, attempts=1)

    def test_bounded_retry_can_read_replaced_file(self):
        path = discovery_file(self.base)
        path.write_text('{')
        with patch('stylelora.backends.nai_launcher.discovery.time.sleep', side_effect=lambda _: discovery_file(self.base)) as sleep:
            self.assertEqual(discover(self.base, environ={}).app_version, '4.3.0')
        self.assertEqual(sleep.call_count, 1)

    def test_schema_hash_and_production_fail_closed(self):
        rows = [ToolSchema(t['name'], t['inputSchema'], t['outputSchema']) for t in TOOLS]
        self.assertEqual(schema_digest(rows), schema_digest(reversed(rows)))
        self.assertEqual(REVIEWED_CONTRACTS, ())
        report, _ = verify_compatibility(info(), '4.3.0', PROTOCOLS[0], rows)
        self.assertFalse(report.accepted)
        self.assertTrue(verify_compatibility(info(), '4.3.0', PROTOCOLS[0], rows, reviewed=(contract(),))[0].accepted)
        for changed in (rows[:-1], [*rows, ToolSchema('unknown', {'type': 'object'})],
                        [ToolSchema(rows[0].name, {'type': 'object', 'required': ['new']}), *rows[1:]]):
            self.assertFalse(verify_compatibility(info(), '4.3.0', PROTOCOLS[0], changed, reviewed=(contract(),))[0].accepted)
        self.assertFalse(verify_compatibility(info(), '4.3.1', PROTOCOLS[0], rows, reviewed=(contract(),))[0].accepted)

    def test_utf8_envelope_exact_size(self):
        base = len(body_bytes({'text': ''}))
        self.assertEqual(len(body_bytes({'text': 'x' * (BODY_LIMIT - base)})), BODY_LIMIT)
        with self.assertRaisesRegex(BackendError, 'mcp:body_too_large'):
            body_bytes({'text': 'x' * (BODY_LIMIT - base + 1)})
        with self.assertRaisesRegex(BackendError, 'mcp:body_too_large'):
            body_bytes({'text': '图' * (BODY_LIMIT // 3)})
        with self.assertRaisesRegex(BackendError, 'mcp:invalid_request'):
            body_bytes({'text': float('nan')})

    def test_sse_fragmented_utf8_crlf_multiline(self):
        decoder, events = SSEDecoder(), []
        raw = ': heartbeat\r\ndata: {"text":\r\ndata: "图片"}\r\n\r\n'.encode()
        for byte in raw:
            events.extend(decoder.feed(bytes([byte])))
        self.assertEqual(json.loads(events[0]), {'text': '图片'})
        self.assertEqual(decoder.feed(b'data: incomplete', final=True), [])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock = MockMcp()
        self.client = McpClient(discovery=info, reviewed=(contract(),), transport=httpx.MockTransport(self.mock), backoff=0)
        self.addAsyncCleanup(self.client.close)

    async def test_handshake_paging_headers_and_unique_ids(self):
        self.mock.paged = True
        report = await self.client.connect()
        self.assertTrue(report.accepted)
        self.assertEqual(len(self.client.tools), 8)
        self.assertGreaterEqual(self.client.tool_timeout, 600)
        self.assertEqual([body['method'] for _, body in self.mock.requests], ['initialize', 'notifications/initialized', 'tools/list', 'tools/list'])
        ids = []
        for index, (request, body) in enumerate(self.mock.requests):
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + TOKEN)
            self.assertIn('text/event-stream', request.headers['Accept'])
            if index:
                self.assertEqual(request.headers['MCP-Protocol-Version'], PROTOCOLS[0])
                self.assertEqual(request.headers['Mcp-Session-Id'], 'session-1')
            if 'id' in body:
                ids.append(body['id'])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(TOKEN, repr(self.client))
        await self.client.close()
        self.assertEqual(self.mock.deletes, 1)

    async def test_unknown_schema_never_calls_tool(self):
        self.client.reviewed = ()
        with self.assertRaisesRegex(BackendError, 'backend:incompatible_schema'):
            await self.client.call_tool('submit_generation', {})
        self.assertFalse(any(body['method'] == 'tools/call' for _, body in self.mock.requests))

    async def test_oversized_or_unknown_tool_never_connects(self):
        for name, args in (('anything', {}), ('submit_generation', {'data': 'x' * BODY_LIMIT})):
            with self.assertRaises(BackendError):
                await self.client.call_tool(name, args)
        self.assertEqual(self.mock.requests, [])

    async def test_sse_ignores_progress_and_other_response_ids(self):
        async def handle(request, body):
            frames = [': heartbeat\r\n\r\n', 'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\r\n\r\n',
                      'data: {"jsonrpc":"2.0","id":"other","result":{}}\n\n',
                      'data: ' + json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': {'structuredContent': {'text': '图片'}}}, ensure_ascii=False) + '\r\n\r\n']
            raw = ''.join(frames).encode()
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream([raw[i:i+1] for i in range(len(raw))]))
        self.mock.tool_handler = handle
        self.assertEqual((await self.client.call_tool('get_generation_status', {})).payload['text'], '图片')

    async def test_empty_or_incomplete_sse_is_not_success(self):
        for content in (b'', b': heartbeat\n\n', b'data: {"jsonrpc":"2.0"}'):
            async def handle(request, body):
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream([content]))
            self.mock.tool_handler = handle
            with self.assertRaisesRegex(BackendError, 'mcp:response_incomplete'):
                await self.client.call_tool('get_generation_status', {})

    async def test_malformed_json_rpc_and_domain_errors_are_safe(self):
        for response, code in (({'jsonrpc': '2.0', 'id': 'wrong', 'result': {}}, 'mcp:response_id_mismatch'),
                               ({'isError': True, 'content': [{'text': TOKEN}]}, 'mcp:tool_error'),
                               ({'structuredContent': {'ok': False, 'error': TOKEN}}, 'mcp:tool_error'),
                               ({'content': [{'type': 'text', 'text': 'invalid-' + TOKEN}]}, 'mcp:malformed_response')):
            async def handle(request, body):
                return httpx.Response(200, json=response) if 'jsonrpc' in response else self.mock.result(body, response)
            self.mock.tool_handler = handle
            with self.assertRaises(BackendError) as caught:
                await self.client.call_tool('get_generation_status', {})
            self.assertEqual(str(caught.exception), code)
            self.assertNotIn(TOKEN, repr(caught.exception))

    async def test_concurrent_session_expiry_singleflight_and_rediscovery(self):
        both = asyncio.Event()
        arrivals = 0
        discoveries = []
        def discovery():
            result = info(8123 + len(discoveries), TOKEN + str(len(discoveries)))
            discoveries.append(result)
            return result
        self.client.discovery = discovery
        async def handle(request, body):
            nonlocal arrivals
            if request.headers['Mcp-Session-Id'] == 'session-1':
                arrivals += 1
                if arrivals == 2:
                    both.set()
                await both.wait()
                return httpx.Response(404)
            self.assertEqual(request.url.port, 8124)
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + TOKEN + '1')
            return self.mock.result(body, {'structuredContent': {'ok': True}})
        self.mock.tool_handler = handle
        await asyncio.gather(*(self.client.call_tool('get_generation_status', {}) for _ in range(2)))
        self.assertEqual(self.mock.initializations, 2)
        self.assertEqual(len(discoveries), 2)

    async def test_sent_write_failures_are_never_replayed_and_block_next_write(self):
        for mode in ('404', '502', 'timeout', 'eof', 'domain'):
            mock = MockMcp()
            client = McpClient(discovery=info, reviewed=(contract(),), transport=httpx.MockTransport(mock), backoff=0)
            calls = []
            async def handle(request, body):
                calls.append(body)
                if mode == 'timeout':
                    raise httpx.ReadTimeout(TOKEN)
                if mode == 'eof':
                    return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream([]))
                if mode == 'domain':
                    return mock.result(body, {'isError': True})
                return httpx.Response(int(mode))
            mock.tool_handler = handle
            with self.assertRaisesRegex(BackendError, 'mcp:write_outcome_unknown'):
                await client.call_tool('submit_generation', {})
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(BackendError, 'mcp:write_barrier'):
                await client.call_tool('update_generation_settings', {})
            mock.tool_handler = None
            await client.connect(expected_generation=client.generation)
            self.assertIsNotNone(client.write_barrier)
            self.assertEqual(await client.close(), ('mcp:write_barrier',))
            self.assertEqual(mock.deletes, 0)

    async def test_read_retries_bounded(self):
        async def handle(request, body):
            return httpx.Response(502)
        self.mock.tool_handler = handle
        with self.assertRaisesRegex(BackendError, 'mcp:http_error'):
            await self.client.call_tool('get_generation_settings', {})
        self.assertEqual(sum(body['method'] == 'tools/call' for _, body in self.mock.requests), 4)

    async def test_heartbeat_cannot_extend_total_deadline(self):
        class Heartbeat(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b': heartbeat\n\n'
                    await asyncio.sleep(.005)
        async def handle(request, body):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Heartbeat())
        await self.client.connect()
        self.client.read_timeout = .04
        self.mock.tool_handler = handle
        start = time.monotonic()
        with self.assertRaisesRegex(BackendError, 'mcp:read_timeout'):
            await self.client.call_tool('get_generation_status', {})
        self.assertLess(time.monotonic() - start, .5)

    async def test_cancel_notification_does_not_prove_write_stopped(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def handle(request, body):
            started.set()
            await release.wait()
            return self.mock.result(body, {'structuredContent': {'ok': True}})
        self.mock.tool_handler = handle
        work = asyncio.create_task(self.client.call_tool('submit_generation', {}))
        await started.wait()
        request_id = next(iter(self.client.active))
        await self.client.cancel_request(request_id)
        self.assertFalse(work.done())
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertEqual(self.client.write_barrier, request_id)

    async def test_writes_serialized_reads_max_two(self):
        active = {'read': 0, 'write': 0}
        maximum = {'read': 0, 'write': 0}
        async def handle(request, body):
            kind = 'read' if body['params']['name'].startswith('get_') else 'write'
            active[kind] += 1
            maximum[kind] = max(maximum[kind], active[kind])
            await asyncio.sleep(.015)
            active[kind] -= 1
            return self.mock.result(body, {'structuredContent': {'ok': True}})
        self.mock.tool_handler = handle
        await asyncio.gather(*(self.client.call_tool(name, {}) for name in ['submit_generation'] * 3 + ['get_generation_status'] * 5))
        self.assertEqual(maximum, {'read': 2, 'write': 1})

    async def test_invalid_initialization_protocol_does_not_escape_cleanup(self):
        async def bad(request):
            if request.method == 'DELETE':
                return httpx.Response(404)
            body = json.loads(request.content)
            return self.mock.result(body, {'protocolVersion': [], 'capabilities': {'tools': {}}, 'serverInfo': {'version': '4.3.0'}}, headers={'Mcp-Session-Id': 'invalid-session'})
        client = McpClient(discovery=info, transport=httpx.MockTransport(bad))
        try:
            with self.assertRaisesRegex(BackendError, 'backend:unsupported_protocol'):
                await client.connect()
        finally:
            await client.close()

    async def test_write_total_timeout_sets_barrier_once(self):
        async def handle(request, body):
            await asyncio.sleep(1)
            return self.mock.result(body, {'structuredContent': {'ok': True}})
        await self.client.connect()
        self.client.tool_timeout = .025
        self.mock.tool_handler = handle
        with self.assertRaisesRegex(BackendError, 'mcp:write_outcome_unknown'):
            await self.client.call_tool('submit_generation', {})
        self.assertIsNotNone(self.client.write_barrier)
        self.assertEqual(sum(body['method'] == 'tools/call' for _, body in self.mock.requests), 1)

    async def test_whole_handshake_has_total_deadline(self):
        async def slow(request):
            if request.method == 'POST' and json.loads(request.content)['method'] == 'notifications/initialized':
                await asyncio.sleep(1)
            return await self.mock(request)
        client = McpClient(discovery=info, transport=httpx.MockTransport(slow), read_timeout=.1)
        try:
            with self.assertRaisesRegex(BackendError, 'mcp:read_timeout'):
                await client.connect()
            self.assertFalse(client.connected)
            self.assertEqual(self.mock.deletes, 1)
        finally:
            await client.close()

    async def test_tool_pages_reject_duplicates_and_repeated_cursor(self):
        for mode in ('duplicate', 'cursor'):
            async def broken(request):
                body = json.loads(request.content) if request.method == 'POST' else {}
                if body.get('method') == 'tools/list':
                    value = {'tools': [TOOLS[0], TOOLS[0]]} if mode == 'duplicate' else {'tools': [], 'nextCursor': 'forever'}
                    return self.mock.result(body, value)
                return await self.mock(request)
            client = McpClient(discovery=info, transport=httpx.MockTransport(broken))
            try:
                with self.assertRaises(BackendError) as caught:
                    await client.connect()
                self.assertEqual(caught.exception.code, 'mcp:malformed_response' if mode == 'duplicate' else 'mcp:pagination_invalid')
            finally:
                await client.close()

    async def test_redirect_is_not_followed_and_limits_reject_bad_responses(self):
        for response, code in ((httpx.Response(302, headers={'Location': 'https://example.com'}), 'mcp:http_error'),
                               (httpx.Response(200, headers={'Content-Type': 'application/json', 'Content-Length': str(17 * 1024 * 1024)}, stream=Stream([])), 'mcp:response_too_large'),
                               (httpx.Response(200, headers={'Content-Type': 'application/json'}, stream=Stream([b'{"x":1e9999}'])), 'mcp:malformed_response')):
            async def handle(request, body):
                return response
            self.mock.tool_handler = handle
            with self.assertRaises(BackendError) as caught:
                await self.client.call_tool('get_generation_status', {})
            self.assertEqual(caught.exception.code, code)
        self.assertTrue(all(request.url.host == '127.0.0.1' for request, _ in self.mock.requests))
