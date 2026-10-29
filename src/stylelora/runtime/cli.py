import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
import webbrowser

import httpx

from .contracts import strict_json
from .errors import ServiceError
from .instance import pipe_request
from .startup import add_options, validate_options, ensure_service


def main(argv):
    parser = argparse.ArgumentParser(prog="stylelora")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start the local HTTP runtime")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--workers", type=int, default=1)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--project", type=Path)
    serve.add_argument("--runtime-dir", type=Path)
    serve.add_argument("--product-dir", type=Path)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--dev-origin")
    ui = sub.add_parser("ui", help="Create a new browser session")
    ui.add_argument("--print-link", action="store_true")
    add_options(ui)
    grant = sub.add_parser("grant", help="Authorize a CLI-supplied path through the current-user pipe")
    grant.add_argument("purpose", choices=("project_create", "project_open", "import_root"))
    grant.add_argument("path", type=Path)
    grant.add_argument("--project-id")
    api = sub.add_parser("api", help="Call the running HTTP API without exposing its token")
    api.add_argument("method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"))
    api.add_argument("path")
    api.add_argument("--json-file", type=Path)
    api.add_argument("--if-match")
    api.add_argument("--idempotency-key")
    api.add_argument("--wait", action="store_true")
    args = parser.parse_args(argv)
    if args.command == 'ui':
        validate_options(parser, args)
    try:
        if args.command == "serve":
            from .server import serve as run
            return run(**{key: value for key, value in vars(args).items() if key != "command"})
        if args.command == "ui":
            identity = ensure_service({}, timeout=args.startup_timeout or 30)['instance_id'] if args.start_service else None
            link = pipe_request({"action": "bootstrap", **({'instance_id': identity} if identity else {})})["url"]
            if args.print_link:
                print(link)
            else:
                webbrowser.open(link)
            return 0
        if args.command == "grant":
            result = pipe_request({"action": "grant", "purpose": args.purpose, "path": str(args.path.absolute()), "project_id": args.project_id})
        else:
            if not args.path.startswith("/api/") or "#" in args.path or "\\" in args.path:
                raise ServiceError(422, "request:invalid_path")
            credentials = pipe_request({"action": "hello"})
            headers = {"Authorization": "Bearer " + credentials["token"]}
            if args.if_match:
                headers["If-Match"] = '"' + args.if_match.strip('"') + '"'
            if args.method not in ("GET", "DELETE"):
                headers["Idempotency-Key"] = args.idempotency_key or str(uuid4())
            body = strict_json(args.json_file.read_bytes()) if args.json_file else {} if args.method != "GET" else None
            # No proxy, no redirect, and no automatic write retry on an unknown outcome.
            with httpx.Client(base_url="http://"+credentials["authority"], headers=headers, trust_env=False, follow_redirects=False, timeout=30) as client:
                response = client.request(args.method, args.path, json=body) if body is not None else client.request(args.method, args.path)
                if response.status_code == 204:
                    result = {"released": True}
                elif response.headers.get("content-type", "").startswith("application/json"):
                    result = response.json()
                else:
                    raise ServiceError(422, "request:json_response_required", "Use an HTTP client to read media or events")
                if response.is_error:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 4 if response.status_code in (409, 410, 423, 428) else 3 if response.status_code in (401, 403) else 5 if response.status_code >= 500 else 2
                while args.wait and response.status_code == 202:
                    url = result["status_url"]
                    while True:
                        time.sleep(0.1)
                        polled = client.get(url)
                        polled.raise_for_status()
                        result = polled.json()
                        if result["status"] in ("done", "finished", "failed", "canceled", "interrupted"):
                            break
                    break
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not isinstance(result, dict):
            return 0
        if result.get("status") in ("failed", "interrupted", "canceled"):
            return 7 if result["status"] != "failed" else 5
        outcome = result.get("result") or result
        if isinstance(outcome, dict) and (outcome.get("failed", 0) or outcome.get("counts", {}).get("failed", 0)):
            return 6
        if isinstance(outcome, dict) and (outcome.get("pending", 0) or outcome.get("sent", 0) or outcome.get("status") == "paused"):
            return 7
        return 0
    except ServiceError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.outcome == 'unknown':
            return 7
        if exc.code == 'process:start_failed':
            return 5
        return 3 if exc.status in (401, 403, 503) else 4 if exc.status in (409, 423) else 2
    except (OSError, httpx.HTTPError) as exc:
        print("Local request failed; inspect state before retrying a write.", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("Stopped waiting. Accepted server tasks continue; use their status/cancel endpoints.", file=sys.stderr)
        return 130
