"""Synthetic MCP contracts. Never installed as a production allowlist."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from stylelora.backends.nai_launcher.compatibility import CORE_TOOLS, schema_digest
from stylelora.backends.nai_launcher.discovery import PROTOCOLS
from stylelora.backends.types import DiscoveryInfo, ReviewedContract, ToolSchema

TOKEN = 'synthetic-fixture-only-token'
TOOLS = [{'name': name, 'inputSchema': {'type': 'object', 'properties': {}},
          'outputSchema': {'type': 'object'}} for name in sorted(CORE_TOOLS)]


def info(port=8123, token=TOKEN):
    return DiscoveryInfo(f'http://127.0.0.1:{port}/mcp', port, token, 1, PROTOCOLS, '4.3.0')


def contract():
    digest = schema_digest(ToolSchema(t['name'], t['inputSchema'], t['outputSchema']) for t in TOOLS)
    return ReviewedContract('4.3.0', '4.3.0', PROTOCOLS[0], digest, digest)


def discovery_file(base, port=8123, **override):
    data = dict(endpoint=f'http://127.0.0.1:{port}/mcp', port=port, token=TOKEN, pid=1,
                protocol_versions=list(PROTOCOLS), app_version='4.3.0')
    data.update(override)
    path = Path(base) / 'nai-launcher' / 'mcp-server.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class MockMcp:
    def __init__(self):
        self.requests, self.initializations, self.deletes = [], 0, 0
        self.tool_handler = None
        self.tools = TOOLS
        self.paged = False

    @staticmethod
    def result(body, value, **kwargs):
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': value}, **kwargs)

    async def __call__(self, request):
        if request.method == 'DELETE':
            self.deletes += 1
            return httpx.Response(404)
        body = json.loads(request.content)
        self.requests.append((request, body))
        method = body['method']
        if method == 'initialize':
            self.initializations += 1
            return self.result(body, {'protocolVersion': PROTOCOLS[0], 'capabilities': {'tools': {}},
                                    'serverInfo': {'name': 'Synthetic fixture', 'version': '4.3.0'}},
                               headers={'Mcp-Session-Id': 'session-' + str(self.initializations)})
        if method.startswith('notifications/'):
            return httpx.Response(202)
        if method == 'tools/list':
            value = {'tools': self.tools}
            if self.paged:
                value = {'tools': self.tools[4:]} if body['params'].get('cursor') else {'tools': self.tools[:4], 'nextCursor': 'page-2'}
            return self.result(body, value)
        if method == 'tools/call' and self.tool_handler:
            return await self.tool_handler(request, body)
        return self.result(body, {'structuredContent': {'ok': True, 'value': 'fixture'}})


class LocalMcpServer:
    """Loopback socket fixture: paginated tools/list over JSON then fragmented SSE."""
    def __init__(self, appdata):
        self.appdata = Path(appdata)
        self.methods = []
        self.mode = 'normal'
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_DELETE(self):
                fixture.methods.append('DELETE')
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                method = body['method']
                fixture.methods.append(method)
                if fixture.mode == 'unauthorized' or self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    return
                if method.startswith('notifications/'):
                    self.send_response(202)
                    self.end_headers()
                    return
                value = {'protocolVersion': PROTOCOLS[0], 'capabilities': {'tools': {}},
                         'serverInfo': {'name': 'Local fixture', 'version': '4.3.0'}}
                sse = method == 'tools/list' and body['params'].get('cursor') == 'second'
                if method == 'tools/list':
                    value = {'tools': TOOLS[4:]} if sse else {'tools': TOOLS[:4], 'nextCursor': 'second'}
                raw = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': value}).encode()
                if sse:
                    raw = b': local heartbeat\r\n\r\ndata: ' + raw + b'\r\n\r\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if sse else 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                if method == 'initialize':
                    self.send_header('Mcp-Session-Id', 'local-session')
                self.end_headers()
                try:
                    for index in range(0, len(raw), 17):
                        self.wfile.write(raw[index:index + 17])
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        discovery_file(self.appdata, self.server.server_port)
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
