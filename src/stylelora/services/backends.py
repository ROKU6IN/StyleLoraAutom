"""Explicit, cancelable, product-scoped connection diagnostics. No generation."""
import asyncio
from copy import deepcopy
import threading
import os

from ..backends.nai_launcher.client import McpClient
from ..backends.nai_launcher.compatibility import CORE_TOOLS
from ..backends.nai_launcher.readonly import inspect as inspect_readonly
from ..backends.types import BackendError
from ..project_db.validation import utc_now
from ..runtime.errors import ServiceError
from ..runtime.supervisor import TERMINAL


def initial_view():
    return {'backend_id': 'nai_launcher', 'name': 'NAI Launcher', 'reserved': False,
            'available': False, 'reachable': False, 'transport_verified': False,
            'status': 'not_checked', 'reason': 'backend:not_checked', 'checked_at': None,
            'app_version': None, 'server_version': None, 'protocol_version': None,
            'schema_hash': None, 'full_schema_hash': None, 'tool_count': 0,
            'core_tools': [], 'missing_core_tools': sorted(CORE_TOOLS),
            'features': {'generation': False, 'tag_search': False, 'interrogate': False},
            'warnings': []}


class BackendService:
    def __init__(self, runtime, *, client_factory=McpClient):
        self.runtime, self.client_factory = runtime, client_factory
        self.lock = threading.RLock()
        self.cached, self.active_id = initial_view(), None
        self.inspection, self.inspection_id = None, None

    def client(self):
        if self.client_factory is not McpClient:
            return self.client_factory()
        mode = self.runtime.product_config.get()['mcp']['discovery_mode']
        if mode == 'default':
            return McpClient()
        from ..backends.nai_launcher.discovery import discover
        directory = os.environ.get('STYLELORA_LAUNCHER_APPDATA')
        if not directory or not os.path.isabs(directory):
            raise ServiceError(503, 'config:mcp_directory_required')
        return McpClient(discovery=lambda: discover(appdata=directory))

    @staticmethod
    def require(backend_id):
        if backend_id != 'nai_launcher':
            raise ServiceError(404, 'backend:not_found')

    def list(self):
        # Reading status never reads credentials or contacts Launcher.
        with self.lock:
            available = self.runtime.nai_direct.available()
            direct = {**initial_view(), 'backend_id': 'nai_direct', 'name': 'NovelAI 直连',
                      'available': available, 'status': 'configured' if available else 'not_configured',
                      'reason': None if available else 'nai:token_missing', 'missing_core_tools': [],
                      'features': {'generation': available, 'tag_search': False, 'interrogate': False}}
            return {'items': [deepcopy(self.cached), direct], 'probe_operation_id': self.active_id,
                    'inspection': deepcopy(self.inspection), 'inspection_operation_id': self.inspection_id}

    def inspect(self, backend_id):
        self.require(backend_id)
        with self.lock:
            if self.inspection_id:
                current = self.runtime.supervisor.get(self.inspection_id)
                if current['status'] not in TERMINAL:
                    return current
            operation = self.runtime.supervisor.submit('backend.inspect', self._run_readonly)
            self.inspection_id = operation['operation_id']
            return operation

    async def read_inspection(self, task):
        """Fresh, cancelable reads without publishing the global diagnostic cache."""
        client = self.client()
        view = {'status': 'unavailable', 'reason': None, 'generation_enabled': False}
        work = asyncio.create_task(inspect_readonly(client))

        async def watch_cancel():
            while not task.cancel.is_set():
                await asyncio.sleep(.025)
            work.cancel()

        watcher = asyncio.create_task(watch_cancel())
        try:
            view = await work
        except BackendError as error:
            view['reason'] = error.code
        except asyncio.CancelledError:
            raise ServiceError(409, 'operation:canceled') from None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            view['warnings'] = list(await client.close())
        if task.cancel.is_set():
            raise ServiceError(409, 'operation:canceled')
        view['checked_at'] = utc_now()
        return view

    def _run_readonly(self, task):
        view = asyncio.run(self.read_inspection(task))
        with self.lock:
            if task.cancel.is_set():
                raise ServiceError(409, 'operation:canceled')
            self.inspection = view
        self.runtime.emit('backend.changed', {'backend_id': 'nai_launcher', 'status': view['status']},
                          resource={'kind': 'backend', 'id': 'nai_launcher'})
        return deepcopy(view)

    def capabilities(self, backend_id):
        if backend_id == 'nai_direct':
            from ..backends.nai_direct.form import form_capabilities
            return form_capabilities()
        self.require(backend_id)
        with self.lock:
            code = 'backend:generation_unavailable' if self.cached['transport_verified'] else 'backend:incompatible_schema'
            raise ServiceError(503, code, details={'reason': self.cached['reason'], 'checked_at': self.cached['checked_at']})

    def probe(self, backend_id):
        if backend_id == 'nai_direct':
            return self.runtime.nai_direct.probe()
        self.require(backend_id)
        with self.lock:
            if self.active_id:
                current = self.runtime.supervisor.get(self.active_id)
                if current['status'] not in TERMINAL:
                    return current
            operation = self.runtime.supervisor.submit('backend.probe', self._run)
            self.active_id = operation['operation_id']
            return operation

    async def _inspect(self, task):
        client = self.client()
        view = initial_view()
        work = asyncio.create_task(client.connect())

        async def watch_cancel():
            while not task.cancel.is_set():
                await asyncio.sleep(.025)
            work.cancel()

        watcher = asyncio.create_task(watch_cancel())
        try:
            report = await work
            names = {tool.name for tool in client.tools}
            view.update(reachable=True, transport_verified=report.accepted,
                        status='verified' if report.accepted else 'incompatible',
                        reason=report.reason or 'backend:generation_unavailable',
                        app_version=client.info.app_version, server_version=client.server_version,
                        protocol_version=client.protocol, schema_hash=report.schema_hash,
                        full_schema_hash=report.full_schema_hash, tool_count=len(client.tools),
                        core_tools=sorted(names & CORE_TOOLS), missing_core_tools=sorted(CORE_TOOLS - names))
        except BackendError as error:
            view.update(status='unavailable', reason=error.code)
        except asyncio.CancelledError:
            raise ServiceError(409, 'operation:canceled') from None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            view['warnings'] = list(await client.close())
        if task.cancel.is_set():
            raise ServiceError(409, 'operation:canceled')
        view['checked_at'] = utc_now()
        return view

    def _run(self, task):
        view = asyncio.run(self._inspect(task))
        with self.lock:
            if task.cancel.is_set():
                raise ServiceError(409, 'operation:canceled')
            self.cached = view
        self.runtime.emit('backend.changed', {'backend_id': 'nai_launcher', 'status': view['status']},
                          resource={'kind': 'backend', 'id': 'nai_launcher'})
        return deepcopy(view)
