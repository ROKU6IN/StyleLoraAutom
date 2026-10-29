import asyncio
import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import threading
import time
from uuid import uuid4
from weakref import WeakKeyDictionary

import httpx

from ..runtime.contracts import strict_json, Versions
from ..runtime.errors import ServiceError
from . import adapters
from .audit import Audit
from .config import ConfigStore
from .contracts import validator, validate_reply
from .images import encode_image, test_image
from .types import JsonReply, LlmFailure, LlmRequest, RepairBudget, RoleBinding, EndpointInput


@dataclass
class Budget:
    remaining: float
    retries: int
    attempts: int = 0
    tier: int = 0
    sent: bool = False
    concurrency: int = 2


class RequestSlots:
    """One shared counter; changing limits never swaps an in-use semaphore."""
    def __init__(self):
        self.lock, self.used = threading.Lock(), 0

    def acquire(self, blocking=False, *, limit=2):
        with self.lock:
            if self.used >= limit:
                return False
            self.used += 1
            return True

    def release(self):
        with self.lock:
            if self.used <= 0:
                raise ValueError('Unbalanced release')
            self.used -= 1


class LlmService:
    def __init__(self, runtime, directory=None, secret_dir=None, *, transport=None):
        self.runtime = runtime
        self.config = ConfigStore(runtime, directory, secret_dir)
        self.audit = Audit(secret_dir)
        from .templates import TemplateRegistry
        self.templates = TemplateRegistry()
        self.transport = transport  # Explicitly injectable local test transport only.
        self.lock = threading.RLock()
        self.slots = RequestSlots()
        self.bindings = WeakKeyDictionary()
        self.active = {}
        self.rotation, self.cooldown = {}, {}
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
            for cancel in self.active.values():
                cancel.set()

    def _bind(self, role, endpoint, model):
        binding = RoleBinding(str(uuid4()), role, model, endpoint['config']['protocol'], endpoint['revision'])
        with self.lock:
            self.bindings[binding] = copy.deepcopy(endpoint)
        return binding

    async def resolve_role(self, role):
        with self.config.lock:
            if role.startswith('clean'):
                raise LlmFailure('clean_reserved', outcome='not_sent')
            value = self.config.state['roles'].get(role)
            if not value:
                raise LlmFailure('role_unconfigured', outcome='not_sent')
            return self._bind(role, self.config.snapshot(value['endpoint_id']), value['model'])

    async def cancel(self, request_id):
        with self.lock:
            event = self.active.get(request_id)
            if event:
                event.set()
            return event is not None

    @staticmethod
    def _check(request, event, budget):
        if event.is_set() or (request.cancel is not None and request.cancel.is_set()):
            raise LlmFailure('canceled', outcome='unknown' if budget.sent else 'not_sent')

    async def _wait(self, duration, request, event, budget):
        start = time.monotonic()
        try:
            until = start + min(duration, budget.remaining)
            while time.monotonic() < until:
                self._check(request, event, budget)
                await asyncio.sleep(min(.02, max(0, until-time.monotonic())))
        finally:
            budget.remaining -= time.monotonic() - start
        if budget.remaining <= 0:
            raise LlmFailure('deadline', outcome='unknown' if budget.sent else 'not_sent')

    def _token(self, endpoint):
        eid, refs = endpoint['endpoint_id'], endpoint['token_refs']
        with self.lock:
            start = self.rotation.get(eid, 0)
            waits = []
            for offset in range(len(refs)):
                index = (start + offset) % len(refs)
                ref = refs[index]
                token = self.config.secrets.read(ref)
                if not token:
                    continue
                wait = self.cooldown.get((eid, ref), 0) - time.monotonic()
                if wait > 0:
                    waits.append(wait)
                    continue
                self.rotation[eid] = (index + 1) % len(refs)
                return token, index, ref, 0
            if waits:
                return None, None, None, min(waits)
            raise LlmFailure('secret_unavailable', outcome='not_sent')

    @staticmethod
    def _retry_after(value):
        try:
            delay = float(value)
            if not 0 <= delay <= 300:
                return 300.0
            return delay
        except (TypeError, ValueError):
            try:
                return min(300, max(0, (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                return 1.0

    async def _http(self, method, url, body, headers, timeout):
        async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                                    timeout=httpx.Timeout(min(60, timeout), connect=min(10, timeout))) as client:
            async with client.stream(method, url, json=body, headers=headers) as response:
                data = bytearray()
                if 200 <= response.status_code < 300:
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 4 * 1024 * 1024:
                            raise LlmFailure('reply_too_large')
                return response.status_code, response.headers.get('retry-after'), bytes(data)

    async def _send(self, request, endpoint, event, budget, *, models=False):
        while True:
            self._check(request, event, budget)
            if budget.remaining <= 0:
                raise LlmFailure('deadline', outcome='unknown' if budget.sent else 'not_sent')
            # Waiting for a shared slot never spends the network/backoff budget.
            while not self.slots.acquire(blocking=False, limit=budget.concurrency):
                self._check(request, event, budget)
                await asyncio.sleep(.01)
            started, index, status = None, None, 'not_sent'
            attempt_id, tier_used = None, budget.tier
            wait, failure = 0, None
            try:
                self._check(request, event, budget)
                token, index, ref, wait = self._token(endpoint)
                if wait:
                    continue
                image = None
                if not models:
                    if request.image_input is not None:
                        image = encode_image(request.image_input, budget.tier)
                    elif request.images:
                        image = encode_image(request.images[0])
                    url, body = adapters.build(endpoint, request, image)
                else:
                    url, body = endpoint['config']['base_url'] + '/models', None
                self._check(request, event, budget)
                started = time.monotonic()
                budget.attempts += 1
                budget.sent = True
                attempt_id = str(uuid4())
                self.audit.write(request_id=request.request_id, role=request.binding.role,
                    endpoint_id=endpoint['endpoint_id'], protocol=request.binding.protocol, model=request.binding.model,
                    token_index=index, status='sending', retry=budget.attempts-1,
                    tier=tier_used if request.image_input is not None or request.images else None, duration=0,
                    attempt_id=attempt_id, phase='started')
                task = asyncio.create_task(self._http('GET' if models else 'POST', url, body,
                    adapters.headers(endpoint['config']['protocol'], token), budget.remaining))
                try:
                    deadline = started + budget.remaining
                    while not task.done():
                        self._check(request, event, budget)
                        if time.monotonic() >= deadline:
                            raise httpx.ReadTimeout('deadline')
                        await asyncio.wait({task}, timeout=min(.02, max(0, deadline-time.monotonic())))
                    self._check(request, event, budget)  # Discard late replies after cancellation.
                    code, retry_after, data = await task
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                status = f'http_{code // 100}xx'
                if code in (401, 403):
                    raise LlmFailure('unauthorized')
                if code == 429:
                    wait = self._retry_after(retry_after)
                    with self.lock:
                        self.cooldown[(endpoint['endpoint_id'], ref)] = time.monotonic() + wait
                    # Rotate immediately if another token is available.
                    wait = 0
                    failure = LlmFailure('rate_limited', retryable=True)
                elif code in (408, 502, 503, 504):
                    wait, failure = .1, LlmFailure('upstream_unavailable', retryable=True, outcome='unknown')
                elif code < 200 or code >= 300:
                    raise LlmFailure('redirect_rejected' if 300 <= code < 400 else 'upstream_rejected')
                else:
                    try:
                        return strict_json(data)
                    except ServiceError:
                        raise LlmFailure('invalid_reply') from None
            except httpx.TimeoutException:
                status = 'timeout'
                if request.timeout_policy == 'vision_downgrade' and budget.tier < 2:
                    budget.tier += 1
                    failure = LlmFailure('timeout', retryable=True, outcome='unknown')
                else:
                    raise LlmFailure('timeout', outcome='unknown') from None
            except httpx.TransportError:
                status = 'network'
                wait, failure = .1, LlmFailure('network', retryable=True, outcome='unknown')
            except LlmFailure as exc:
                status = exc.code
                raise
            finally:
                self.slots.release()
                if started is not None:
                    elapsed = time.monotonic() - started
                    budget.remaining -= elapsed
                    self.audit.write(request_id=request.request_id, role=request.binding.role,
                        endpoint_id=endpoint['endpoint_id'], protocol=request.binding.protocol, model=request.binding.model,
                        token_index=index, status=status, retry=max(0, budget.attempts-1),
                        tier=tier_used if request.image_input is not None or request.images else None, duration=elapsed,
                        attempt_id=attempt_id)
                if started is None and wait:
                    await self._wait(wait, request, event, budget)
            if failure:
                if budget.retries <= 0:
                    raise failure
                budget.retries -= 1
                if wait:
                    await self._wait(wait, request, event, budget)

    async def _run(self, request, *, json_mode=False, models=False, repair_limit=1):
        if type(repair_limit) is not int or repair_limit not in (0, 1):
            raise LlmFailure('invalid_repair_limit', outcome='not_sent')
        with self.lock:
            if self.closed:
                raise LlmFailure('closed', outcome='not_sent')
            if request.request_id in self.active:
                raise LlmFailure('duplicate_request', outcome='not_sent')
            if len(self.active) >= 128:
                raise LlmFailure('queue_full', outcome='not_sent')
            endpoint = self.bindings.get(request.binding)
            if endpoint is None:
                raise LlmFailure('binding_expired', outcome='not_sent')
            event = threading.Event()
            self.active[request.request_id] = event
        try:
            limits = self.runtime.product_config.get()['llm_limits']
            budget = Budget(min(request.timeout_s, limits['timeout_s']), request.transport_retry_limit,
                            tier=request.initial_tier, concurrency=limits['concurrency'])
            request = replace(request, payload=copy.deepcopy(request.payload), response_schema=copy.deepcopy(request.response_schema))
            check = validator(request.response_schema) if request.response_schema is not None else None
            if check:
                request = replace(request, response_schema=check.schema)
            if json_mode and check is None:
                raise LlmFailure('schema_required', outcome='not_sent')
            model = next((m for m in endpoint['config']['models'] if m['name'] == request.binding.model), None)
            if (request.images or request.image_input is not None) and (not model or not model['supports_images']):
                raise LlmFailure('image_capability_required', outcome='not_sent')
            repair = request.repair_budget or RepairBudget(limit=repair_limit)
            repaired = False
            while True:
                value = await self._send(request, endpoint, event, budget, models=models)
                if models:
                    return value
                reply = adapters.parse(endpoint, request, value,
                    budget.tier if request.images or request.image_input is not None else None, budget.attempts)
                if not json_mode:
                    return reply
                try:
                    value = validate_reply(reply.text, check)
                    return JsonReply(value, reply, repaired)
                except LlmFailure:
                    # Semantic refusal/invalid_input belongs to routing, never repair.
                    try:
                        semantic = strict_json(reply.text)
                    except ServiceError:
                        semantic = None
                    if isinstance(semantic, dict) and semantic.get('status') in ('refused_nsfw', 'invalid_input'):
                        raise LlmFailure('semantic_result_invalid') from None
                    if repair_limit == 0 or not repair.take():
                        raise
                    repaired = True
                    # Keep original trusted instructions/input; never replay raw model text.
                    request = replace(request, system_text=request.system_text +
                        '\nThe previous result failed JSON/schema validation. Produce a fresh result following the schema exactly.')
        finally:
            with self.lock:
                self.active.pop(request.request_id, None)

    async def complete(self, request):
        return await self._run(request)

    async def complete_json(self, request, *, repair_limit=1):
        return await self._run(request, json_mode=True, repair_limit=repair_limit)

    def probe(self, eid, expected, model=None):
        with self.config.lock:
            endpoint = self.config.snapshot(eid)
            Versions.require(expected, self.config.version(endpoint))
            if model is not None and not any(m['name'] == model for m in endpoint['config']['models']):
                raise ServiceError(422, 'llm:model_not_configured')

        async def execute(task):
            frozen = copy.deepcopy(endpoint)
            if model is None:
                binding = self._bind('diagnostic', frozen, 'models-probe')
                request = LlmRequest(task.operation_id, binding, '', {}, None, 30, 2, cancel=task.cancel)
                response = await self._run(request, models=True)
                try:
                    data = response['data']
                    if not isinstance(data, list) or len(data) > 1000 or response.get('has_more'):
                        raise ValueError()
                    discovered = []
                    for item in data:
                        modalities = item.get('input_modalities', item.get('modalities', {}).get('input', [])
                            if isinstance(item.get('modalities'), dict) else [])
                        discovered.append({'name': item['id'], 'supports_images': 'image' in modalities,
                            'capability_source': 'models', 'tested_at': None, 'response_aliases': []})
                    names = {m['name'] for m in frozen['config']['models']}
                    frozen['config']['models'].extend(m for m in discovered if m['name'] not in names)
                except (KeyError, TypeError, ValueError):
                    raise LlmFailure('invalid_models_reply') from None
            else:
                chosen = next(m for m in frozen['config']['models'] if m['name'] == model)
                chosen['supports_images'] = True
                binding = self._bind('diagnostic', frozen, model)
                schema = {'type': 'object', 'properties': {key: {'type': 'string'} for key in
                    ('left_color', 'left_shape', 'right_color', 'right_shape')},
                    'required': ['left_color', 'left_shape', 'right_color', 'right_shape'], 'additionalProperties': False}
                result = await self.complete_json(LlmRequest(task.operation_id, binding,
                    'Identify the left and right objects in the supplied image. Use lowercase English color and shape names.',
                    {}, schema, 30, 1, image_input=test_image(), cancel=task.cancel, repair_budget=RepairBudget(limit=0)))
                if result.value != {'left_color': 'red', 'left_shape': 'square', 'right_color': 'blue', 'right_shape': 'circle'}:
                    raise LlmFailure('image_test_failed')
                chosen['capability_source'] = 'tested'
                chosen['tested_at'] = datetime.now(timezone.utc).isoformat()
            if task.cancel.is_set():
                raise LlmFailure('canceled', outcome='unknown')
            # CAS prevents a slow diagnostic from overwriting newer user edits.
            result = self.config.save(EndpointInput.model_validate(frozen['config']), eid, expected, trusted_probe=True)
            return {'endpoint': result, 'passed': True}

        return self.runtime.supervisor.submit('llm.image_test' if model else 'llm.models_probe',
            lambda task: asyncio.run(execute(task)))
