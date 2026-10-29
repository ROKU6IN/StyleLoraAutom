"""Bounded Streamable HTTP client; unknown writes are never replayed."""
import asyncio
import json
import re
import random
from uuid import uuid4

import httpx

from ... import __version__
from ..types import BackendError, ToolReply, ToolSchema
from .compatibility import READ_TOOLS, WRITE_TOOLS, REVIEWED_CONTRACTS, verify_compatibility
from .discovery import PROTOCOLS, VERSION, discover, strict_json
from .sse import SSEDecoder

BODY_LIMIT = 8 * 1024 * 1024
RESPONSE_LIMIT = 16 * 1024 * 1024


def body_bytes(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BackendError('mcp:invalid_request') from None
    if len(raw) > BODY_LIMIT:
        raise BackendError('mcp:body_too_large')
    return raw


class McpClient:
    def __init__(self, *, discovery=discover, reviewed=REVIEWED_CONTRACTS, transport=None,
                 connect_timeout=10, read_timeout=30, tool_timeout=660, read_retries=3, backoff=1):
        self.discovery, self.reviewed = discovery, reviewed
        self.connect_timeout, self.read_timeout, self.tool_timeout = connect_timeout, read_timeout, tool_timeout
        self.read_retries, self.backoff = read_retries, backoff
        self.http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        self.session_lock, self.write_lock = asyncio.Lock(), asyncio.Lock()
        self.read_slots = asyncio.Semaphore(2)
        self.connected, self.closed, self.closing = False, False, False
        self.info, self.session_id, self.protocol = None, None, None
        self.generation, self.server_version, self.tools = 0, None, ()
        self.server_name = None
        self.compatibility, self.contract = None, None
        self.write_barrier = None
        self.active = {}

    def __repr__(self):
        return f'McpClient(connected={self.connected}, generation={self.generation}, blocked={self.write_barrier is not None})'

    @staticmethod
    def _json(raw):
        try:
            return strict_json(raw)
        except (ValueError, TypeError, RecursionError):
            raise BackendError('mcp:malformed_response') from None

    @staticmethod
    def _result(value, request_id, *, stream):
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
            raise BackendError('mcp:malformed_response')
        if 'id' not in value:
            if stream and isinstance(value.get('method'), str):
                return None, False  # Progress/log notifications are not results.
            raise BackendError('mcp:malformed_response')
        if value['id'] != request_id:
            if stream:
                return None, False
            raise BackendError('mcp:response_id_mismatch')
        if ('result' in value) == ('error' in value):
            raise BackendError('mcp:malformed_response')
        if 'error' in value:
            raise BackendError('mcp:rpc_error', outcome='rejected')
        if not isinstance(value['result'], dict):
            raise BackendError('mcp:malformed_response')
        return value['result'], True

    async def _wire(self, method, params=None, *, info, session=None, protocol=None, notification=False, timeout=None, request_id=None):
        request_id = None if notification else request_id or str(uuid4())
        body = {'jsonrpc': '2.0', 'method': method}
        if request_id is not None:
            body['id'] = request_id
        if params is not None:
            body['params'] = params
        raw = body_bytes(body)
        headers = {'Authorization': 'Bearer ' + info.token, 'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream', 'Accept-Encoding': 'identity'}
        if session:
            headers['Mcp-Session-Id'] = session
        if protocol:
            headers['MCP-Protocol-Version'] = protocol
        deadline = timeout if timeout is not None else self.read_timeout
        try:
            async with asyncio.timeout(deadline):
                async with self.http.stream('POST', info.endpoint, content=raw, headers=headers,
                                            timeout=httpx.Timeout(deadline, connect=min(self.connect_timeout, deadline))) as response:
                    if response.status_code in (401, 403):
                        raise BackendError('mcp:unauthorized')
                    if response.status_code == 404 and session:
                        raise BackendError('mcp:session_expired', retry='read_only')
                    if response.status_code == 429:
                        error = BackendError('mcp:rate_limited', retry='read_only')
                        wait = response.headers.get('retry-after', '')
                        error.retry_after = min(float(wait), 30) if re.fullmatch(r'\d+(?:\.\d+)?', wait) else 0
                        raise error
                    if response.status_code != (202 if notification else 200):
                        raise BackendError('mcp:http_error', retry='read_only' if response.status_code >= 500 else 'manual')
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise BackendError('mcp:malformed_response')
                    session_id = response.headers.get('mcp-session-id')
                    if session_id is not None and (not 1 <= len(session_id) <= 256 or any(not 33 <= ord(c) <= 126 for c in session_id)):
                        raise BackendError('mcp:malformed_response')
                    if notification:
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                raise BackendError('mcp:malformed_response')
                        return {}, session_id
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if content_type not in ('application/json', 'text/event-stream'):
                        raise BackendError('mcp:malformed_response')
                    length = response.headers.get('content-length')
                    if length and (not length.isdecimal() or int(length) > RESPONSE_LIMIT):
                        raise BackendError('mcp:response_too_large')
                    total, chunks, parser = 0, [], SSEDecoder(RESPONSE_LIMIT)
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > RESPONSE_LIMIT:
                            raise BackendError('mcp:response_too_large')
                        if content_type == 'application/json':
                            chunks.append(chunk)
                        else:
                            for event in parser.feed(chunk):
                                if not event.strip():
                                    continue
                                value, found = self._result(self._json(event), request_id, stream=True)
                                if found:
                                    return value, session_id
                    if content_type == 'text/event-stream':
                        parser.feed(b'', final=True)
                        raise BackendError('mcp:response_incomplete')
                    value, _ = self._result(self._json(b''.join(chunks)), request_id, stream=False)
                    return value, session_id
        except (TimeoutError, httpx.TimeoutException):
            raise BackendError('mcp:read_timeout', retry='read_only') from None
        except httpx.HTTPError:
            raise BackendError('mcp:disconnected', retry='read_only') from None

    async def connect(self, *, expected_generation=None):
        async with self.session_lock:
            if self.closed or self.closing:
                raise BackendError('mcp:closed')
            if self.connected and (expected_generation is None or self.generation != expected_generation):
                return self.compatibility
            self.connected = False
            info = await asyncio.to_thread(self.discovery)
            supported = [v for v in PROTOCOLS if v in info.protocol_versions]
            if not supported:
                raise BackendError('backend:unsupported_protocol', phase='initialize')
            session = None
            protocol = supported[0]
            try:
                async with asyncio.timeout(self.read_timeout):
                    result, session = await self._wire('initialize', {'protocolVersion': protocol, 'capabilities': {},
                        'clientInfo': {'name': 'StyleLoraAutom', 'version': __version__}}, info=info, timeout=self.connect_timeout)
                    protocol = result.get('protocolVersion')
                    server = result.get('serverInfo', {})
                    capabilities = result.get('capabilities', {})
                    if protocol not in supported:
                        raise BackendError('backend:unsupported_protocol', phase='initialize')
                    if (not isinstance(server, dict) or not isinstance(server.get('version'), str)
                            or len(server['version']) > 128 or not VERSION.fullmatch(server['version']) or info.token in server['version']
                            or not isinstance(capabilities, dict) or not isinstance(capabilities.get('tools'), dict)):
                        raise BackendError('mcp:malformed_response', phase='initialize')
                    await self._wire('notifications/initialized', info=info, session=session, protocol=protocol,
                                     notification=True, timeout=self.connect_timeout)
                    rows, seen, cursors, cursor, size = [], set(), set(), None, 0
                    for _ in range(32):
                        page, _ = await self._wire('tools/list', {'cursor': cursor} if cursor else {}, info=info,
                                                   session=session, protocol=protocol)
                        tools = page.get('tools')
                        if not isinstance(tools, list):
                            raise BackendError('mcp:malformed_response', phase='tools')
                        size += len(json.dumps(page, ensure_ascii=False).encode('utf-8'))
                        if size > RESPONSE_LIMIT or len(rows) + len(tools) > 10000:
                            raise BackendError('mcp:tools_limit', phase='tools')
                        for tool in tools:
                            if not isinstance(tool, dict):
                                raise BackendError('mcp:malformed_response', phase='tools')
                            name, schema, output = tool.get('name'), tool.get('inputSchema'), tool.get('outputSchema')
                            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,128}', name)
                                    or name in seen or not isinstance(schema, dict) or schema.get('type') != 'object'
                                    or output is not None and not isinstance(output, dict)):
                                raise BackendError('mcp:malformed_response', phase='tools')
                            seen.add(name)
                            rows.append(ToolSchema(name, schema, output))
                        cursor = page.get('nextCursor')
                        if cursor is None:
                            break
                        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or cursor in cursors:
                            raise BackendError('mcp:pagination_invalid', phase='tools')
                        cursors.add(cursor)
                    else:
                        raise BackendError('mcp:tools_limit', phase='tools')
                    report, contract = verify_compatibility(info, server['version'], protocol, rows, reviewed=self.reviewed)
                    self.info, self.session_id, self.protocol = info, session, protocol
                    self.server_version, self.tools = server['version'], tuple(rows)
                    self.server_name = server.get('name')
                    self.compatibility, self.contract = report, contract
                    self.generation += 1
                    self.connected = True
                    return report
            except BaseException as error:
                if session:
                    await self._delete(info, session, protocol if isinstance(protocol, str) and protocol in PROTOCOLS else supported[0])
                if isinstance(error, TimeoutError):
                    raise BackendError('mcp:read_timeout', phase='initialize', retry='read_only') from None
                if isinstance(error, (UnicodeError, ValueError, RecursionError)):
                    raise BackendError('mcp:malformed_response', phase='initialize') from None
                raise

    @staticmethod
    def _tool_reply(result):
        if type(result.get('isError', False)) is not bool:
            raise BackendError('mcp:malformed_response')
        if result.get('isError'):
            raise BackendError('mcp:tool_error', outcome='rejected')
        payload = result.get('structuredContent')
        if payload is None:
            content = result.get('content')
            if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or content[0].get('type') != 'text':
                raise BackendError('mcp:malformed_response')
            payload = McpClient._json(content[0].get('text'))
        if not isinstance(payload, dict):
            raise BackendError('mcp:malformed_response')
        if payload.get('ok') is False:
            raise BackendError('mcp:tool_error', outcome='rejected')
        return ToolReply(payload)

    async def call_tool(self, name, arguments):
        if name not in READ_TOOLS | WRITE_TOOLS or not isinstance(arguments, dict):
            raise BackendError('backend:unsupported_tool')
        # Validate size before connecting, locking, or recording any sent intent.
        request_id = str(uuid4())
        frozen = body_bytes({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
        arguments = self._json(frozen)['params']['arguments']
        try:
            return await self._call_tool(name, arguments, request_id)
        except TimeoutError:
            if self.write_barrier == request_id:
                raise BackendError('mcp:write_outcome_unknown', phase='tools/call', outcome='unknown') from None
            raise BackendError('mcp:read_timeout', retry='read_only') from None

    async def call_readonly(self, name):
        from .readonly import READONLY_TOOLS
        if name not in READONLY_TOOLS:
            raise BackendError('backend:unsupported_tool')
        try:
            return await self._call_tool(name, {}, str(uuid4()), readonly=True)
        except TimeoutError:
            raise BackendError('mcp:read_timeout', retry='read_only') from None

    def _authorize(self, name, readonly):
        if readonly:
            from .readonly import verify_readonly
            verify_readonly(self, name)
        elif not self.compatibility.accepted:
            raise BackendError('backend:incompatible_schema', phase='compatibility')

    async def _call_tool(self, name, arguments, request_id, *, readonly=False):
        writing = name in WRITE_TOOLS
        async with self.write_lock if writing else self.read_slots:
            await self.connect()
            self._authorize(name, readonly)
            if writing and self.write_barrier:
                raise BackendError('mcp:write_barrier', phase='dispatch', outcome='unknown')
            async with asyncio.timeout(self.tool_timeout if writing else self.read_timeout):
                for attempt in range(1 if writing else self.read_retries + 1):
                    self._authorize(name, readonly)
                    generation = self.generation
                    self.active[request_id] = (self.info, self.session_id, self.protocol)
                    try:
                        result, _ = await self._wire('tools/call', {'name': name, 'arguments': arguments}, info=self.info,
                            session=self.session_id, protocol=self.protocol, timeout=self.tool_timeout if writing else self.read_timeout,
                            request_id=request_id)
                        return self._tool_reply(result)
                    except asyncio.CancelledError:
                        if writing:
                            self.write_barrier = request_id
                        raise
                    except BackendError as error:
                        if writing:
                            self.write_barrier = request_id
                            raise BackendError('mcp:write_outcome_unknown', phase='tools/call', outcome='unknown') from None
                        if attempt >= self.read_retries or error.retry != 'read_only':
                            raise
                        if error.code == 'mcp:session_expired':
                            await self.connect(expected_generation=generation)
                            self._authorize(name, readonly)
                        else:
                            delay = min(self.backoff * 2**attempt * random.uniform(.75, 1.25), 30)
                            await asyncio.sleep(max(delay, getattr(error, 'retry_after', 0)))
                    finally:
                        self.active.pop(request_id, None)

    async def cancel_request(self, request_id):
        state = self.active.get(request_id)
        if state is None:
            raise BackendError('mcp:request_not_active')
        info, session, protocol = state
        await self._wire('notifications/cancelled', {'requestId': request_id, 'reason': 'Client requested cancellation'},
                         info=info, session=session, protocol=protocol, notification=True, timeout=self.connect_timeout)

    async def _delete(self, info, session, protocol):
        try:
            async with asyncio.timeout(min(self.connect_timeout, 3)):
                async with self.http.stream('DELETE', info.endpoint, headers={'Authorization': 'Bearer ' + info.token,
                    'Mcp-Session-Id': session, 'MCP-Protocol-Version': protocol}, timeout=min(self.connect_timeout, 3)) as response:
                    return response.status_code in (200, 202, 204, 404, 405)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return False

    async def close(self):
        if self.closed:
            return ()
        self.closing = True
        warnings = []
        async with self.write_lock, self.read_slots, self.read_slots, self.session_lock:
            try:
                if self.write_barrier:
                    warnings.append('mcp:write_barrier')
                elif self.connected and self.session_id and not await self._delete(self.info, self.session_id, self.protocol):
                    warnings.append('mcp:close_unconfirmed')
            finally:
                await self.http.aclose()
                self.connected, self.closed = False, True
        return tuple(warnings)
