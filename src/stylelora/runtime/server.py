import asyncio
import json
import os
import socket
import webbrowser

import uvicorn

from ..api.app import create_app
from ..project_db.storage import atomic_json
from .errors import ServiceError
from .instance import PipeServer, ProductLock, instance_names, pipe_request
from .lifecycle import Runtime
from .paths import locations


def serve(*, host="127.0.0.1", port=0, workers=1, reload=False, project=None, runtime_dir=None, no_browser=False, dev_origin=None, product_dir=None):
    if host != "127.0.0.1" or workers != 1 or reload or not 0 <= port <= 65535:
        raise ServiceError(422, "process:invalid_options", "Use one worker on 127.0.0.1 without reload")
    try:
        owner = ProductLock()
    except ServiceError as exc:
        if exc.code != "instance:already_running":
            raise
        current = pipe_request({"action": "hello"})
        if not no_browser:
            webbrowser.open(pipe_request({"action": "bootstrap"})["url"])
        print(json.dumps({"attached": True, "instance_id": current["instance_id"], "authority": current["authority"]}))
        return 0
    listener, runtime, pipe = None, None, None
    location = None
    try:
        paths = locations(runtime_dir, product_dir)
        state = paths.runtime
        state.mkdir(parents=True, exist_ok=True)
        from ..exchange.common import is_link
        if any(is_link(path) for path in (state, *state.parents)):
            raise ServiceError(422, "path:unsafe")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((host, port))
        except OSError as exc:
            raise ServiceError(503, "process:port_unavailable") from exc
        listener.setblocking(False)
        runtime = Runtime(authority=f"127.0.0.1:{listener.getsockname()[1]}", dev_origin=dev_origin, native_picker=True,
                          product_dir=paths.product, model_dir=paths.models,
                          llm_dir=paths.config, secret_dir=paths.private)
        if project:
            grant = runtime.grant(project, "project_open")
            runtime.open_now(runtime.grants.get(grant["grant_id"], "project_open"))
        pipe = PipeServer(runtime)
        app = create_app(runtime)
        class LocalServer(uvicorn.Server):
            def handle_exit(self, sig, frame):
                runtime.draining = True
                runtime.events.close()
                super().handle_exit(sig, frame)
        config = uvicorn.Config(app, host=host, port=listener.getsockname()[1], workers=1, access_log=False,
                                proxy_headers=False, server_header=False, log_level="warning", lifespan="on", timeout_graceful_shutdown=30)
        server = LocalServer(config)
        location = state / "instance.json"
        async def run():
            async def monitor():
                while not server.started and not server.should_exit:
                    await asyncio.sleep(0.01)
                if not server.started:
                    return
                info = {"pid": os.getpid(), "instance_id": runtime.instance_id, "port": listener.getsockname()[1], "protocol_version": 1, "pipe_name": instance_names()[2]}
                atomic_json(location, info)
                print(json.dumps({"ready": True, **info}), flush=True)
                if not no_browser:
                    webbrowser.open(runtime.issue_bootstrap())
                while not server.should_exit:
                    if runtime.shutdown_requested.is_set() or pipe.failure:
                        await asyncio.sleep(0.1)
                        config.timeout_graceful_shutdown = runtime.shutdown_grace_seconds
                        runtime.events.close()
                        server.should_exit = True
                        return
                    await asyncio.sleep(0.05)
            watcher = asyncio.create_task(monitor())
            try:
                await server.serve(sockets=[listener])
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        asyncio.run(run())
        return 0
    finally:
        if pipe:
            pipe.close()
        if runtime:
            runtime.shutdown()
        if listener:
            listener.close()
        if location and location.exists():
            try:
                value = json.loads(location.read_text(encoding="utf-8"))
                if runtime and value.get("instance_id") == runtime.instance_id:
                    location.unlink()
            except (OSError, ValueError):
                pass
        owner.close()
