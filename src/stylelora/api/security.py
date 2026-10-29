import secrets
from uuid import UUID, uuid4

from starlette.responses import JSONResponse, Response

from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError, safe_error

MAX_BODY = 16*1024*1024
ALLOW_HEADERS = {"authorization", "content-type", "if-match", "idempotency-key", "last-event-id", "x-request-id"}


class SecurityMiddleware:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.decode().lower(), []).append(value.decode("latin-1"))
        request_id = str(uuid4())
        try:
            candidate = headers.get("x-request-id", [None])[0]
            if candidate and str(UUID(candidate)) == candidate:
                request_id = candidate
        except ValueError:
            pass
        scope["request_id"] = request_id
        started = False
        async def decorated_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                extra = [(b"x-request-id", request_id.encode()), (b"x-content-type-options", b"nosniff"),
                         (b"referrer-policy", b"no-referrer"), (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")]
                present = {key.lower() for key, _ in message.get("headers", [])}
                if b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                origin = headers.get("origin", [None])[0]
                if origin and origin == self.runtime.dev_origin:
                    extra.extend([(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin"),
                                  (b"access-control-expose-headers", b"ETag, Location, Retry-After, X-Request-ID")])
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)
        try:
            if headers.get("host") != [self.runtime.authority]:
                raise ServiceError(403, "security:host_denied")
            origin = headers.get("origin", [None])[0]
            if len(headers.get("origin", [])) > 1 or origin is not None and origin not in (self.runtime.origin, self.runtime.dev_origin):
                raise ServiceError(403, "security:origin_denied")
            if headers.get("sec-fetch-site") == ["cross-site"]:
                raise ServiceError(403, "security:origin_denied")
            api = scope["path"].startswith("/api")
            if scope["method"] == "OPTIONS":
                requested = {part.strip().lower() for part in headers.get("access-control-request-headers", [""])[0].split(",") if part.strip()}
                if not origin or requested - ALLOW_HEADERS or headers.get("access-control-request-method", [""])[0] not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    raise ServiceError(403, "security:origin_denied")
                response = Response(status_code=204, headers={"Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE", "Access-Control-Allow-Headers": ", ".join(sorted(ALLOW_HEADERS))})
                return await response(scope, receive, decorated_send)
            bootstrap = scope["path"] == "/api/session/bootstrap" and scope["method"] == "POST"
            if bootstrap and not origin:
                raise ServiceError(401, "auth:bootstrap_invalid")
            if api and not bootstrap:
                authorization = headers.get("authorization", [])
                if not authorization:
                    raise ServiceError(401, "auth:required")
                if len(authorization) != 1 or not secrets.compare_digest(authorization[0].encode("latin-1"), ("Bearer " + self.runtime.token).encode()):
                    raise ServiceError(401, "auth:invalid")
            if api and self.runtime.draining and scope["method"] not in ("GET", "HEAD"):
                if scope["path"] != "/api/session/shutdown":
                    raise ServiceError(503, "process:draining")
            if api and scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
                if headers.get("content-encoding", ["identity"])[0] != "identity":
                    raise ServiceError(415, "request:encoding_unsupported")
                if len(headers.get("content-length", [])) > 1:
                    raise ServiceError(400, "request:malformed_length")
                try:
                    length = int(headers.get("content-length", ["0"])[0])
                except ValueError as exc:
                    raise ServiceError(400, "request:malformed_length") from exc
                if length < 0:
                    raise ServiceError(400, "request:malformed_length")
                media_type = headers.get('content-type', [''])[0].split(';')[0].strip().lower()
                multipart = media_type == 'multipart/form-data' and scope['path'] in ('/api/tags/imports/preview','/api/rules/imports/preview','/api/prompt-production/imports/preview')
                max_body = 11*1024*1024 if multipart else MAX_BODY
                if length > max_body:
                    raise ServiceError(413, "request:too_large")
                chunks, count = [], 0
                while True:
                    chunk = await receive()
                    if chunk["type"] == "http.disconnect":
                        return
                    count += len(chunk.get("body", b""))
                    if count > max_body:
                        raise ServiceError(413, "request:too_large")
                    chunks.append(chunk.get("body", b""))
                    if not chunk.get("more_body", False):
                        break
                body = b"".join(chunks)
                if body:
                    if not multipart and media_type != "application/json":
                        raise ServiceError(415, "request:content_type")
                    if not multipart:
                        strict_json(body)
                delivered = False
                original_receive = receive
                async def buffered_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await original_receive()
                receive = buffered_receive
            return await self.app(scope, receive, decorated_send)
        except Exception as exc:
            if started:
                raise
            error = safe_error(exc)
            return await JSONResponse({"error": error.body(request_id)}, status_code=error.status)(scope, receive, decorated_send)
