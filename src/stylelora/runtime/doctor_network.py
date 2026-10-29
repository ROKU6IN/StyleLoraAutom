"""Explicit read-only network diagnostics; never instantiate application services."""
import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, field
import os
import time

import httpx

from ..backends.nai_direct.client import NaiDirectBackend
from ..backends.nai_direct.service import NaiSecrets
from ..llm.adapters import headers
from ..llm.config import ConfigStore
from ..llm.types import ModelConfig
from .contracts import strict_json
from .doctor import bounded, result
from .errors import ServiceError

MAX_ENDPOINTS = 16
MAX_REPLY = 1024**2


@dataclass(frozen=True)
class Probe:
    identifier: str
    kind: str
    token: str = field(repr=False)
    protocol: str | None = None
    base_url: str | None = field(default=None, repr=False)


def entry(identifier, code, exit_code=0, **details):
    return {'id': identifier, **result(code, exit_code, read_only=True, generation_performed=False, **details)}


def collect(paths):
    """Freeze bounded configuration/credentials, release all pins before HTTP."""
    rows, probes = [], []
    try:
        with ExitStack() as stack:
            stack.enter_context(bounded(paths.config/'llm.json', 4*1024**2, optional=True))
            stack.enter_context(bounded(paths.private/'secrets.json', 4*1024**2, optional=True))
            config = ConfigStore(None, paths.config, paths.private)
            endpoints = config.state['endpoints']
            if len(endpoints) > MAX_ENDPOINTS:
                rows.append(entry('network.llm', 'doctor:too_many_endpoints', 7, attempted=False,
                                  configured_count=len(endpoints), maximum=MAX_ENDPOINTS))
            elif not endpoints:
                rows.append(entry('network.llm', 'doctor:not_configured', attempted=False))
            else:
                for eid, endpoint in sorted(endpoints.items()):
                    identifier = 'network.llm.'+eid
                    try:
                        token = next((value for ref in endpoint['token_refs']
                                      if (value := config.secrets.read(ref))), None)
                        if token is None:
                            rows.append(entry(identifier, 'doctor:credentials_missing', 7, attempted=False))
                        else:
                            probes.append(Probe(identifier, 'llm_models', token, endpoint['config']['protocol'],
                                                endpoint['config']['base_url']))
                    except ServiceError:
                        rows.append(entry(identifier, 'doctor:credentials_unavailable', 5, attempted=False))
    except Exception:
        rows.append(entry('network.llm', 'doctor:network_config_invalid', 5, attempted=False))
    try:
        with bounded(paths.private/'nai-direct'/'secrets.json', 4*1024**2, optional=True):
            secrets = NaiSecrets(paths.private/'nai-direct')
            configured = 'novelai' in secrets.entries or secrets.env_name('novelai') in os.environ
            token = secrets.read('novelai') if configured else None
        if not configured:
            rows.append(entry('network.nai_direct', 'doctor:not_configured', attempted=False))
        elif not token:
            rows.append(entry('network.nai_direct', 'doctor:credentials_missing', 7, attempted=False))
        else:
            probes.append(Probe('network.nai_direct', 'nai_subscription', token))
    except Exception:
        rows.append(entry('network.nai_direct', 'doctor:credentials_unavailable', 5, attempted=False))
    return rows, probes


async def models(probe, timeout, transport=None):
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
            limits=httpx.Limits(max_connections=1)) as client:
        async with client.stream('GET', probe.base_url+'/models', headers={
                **headers(probe.protocol, probe.token), 'Accept': 'application/json', 'Accept-Encoding': 'identity',
        }) as response:
            status = response.status_code
            code = ('network_unauthorized' if status in (401, 403) else
                    'network_rate_limited' if status == 429 else
                    'network_probe_unsupported' if status in (404, 405, 501) else
                    'network_redirect_rejected' if 300 <= status < 400 else 'network_http_error')
            if status != 200:
                raise ServiceError(503, 'doctor:'+code)
            if (response.headers.get('content-encoding', 'identity').lower() != 'identity'
                    or response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'):
                raise ServiceError(503, 'doctor:network_invalid_reply')
            length = response.headers.get('content-length')
            if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 10 or int(length) > MAX_REPLY):
                raise ServiceError(503, 'doctor:network_reply_too_large')
            data = bytearray()
            async for chunk in response.aiter_bytes(65536):
                if len(data)+len(chunk) > MAX_REPLY:
                    raise ServiceError(503, 'doctor:network_reply_too_large')
                data.extend(chunk)
    try:
        value = strict_json(bytes(data))
        items = value['data']
        if not isinstance(items, list) or len(items) > 1000 or type(value.get('has_more', False)) is not bool:
            raise ValueError()
        names = [ModelConfig.model_validate({'name': item['id']}).name for item in items]
        if len(set(names)) != len(names):
            raise ValueError()
        return {'returned_model_count': len(names), 'more_models': value.get('has_more', False)}
    except Exception:
        raise ServiceError(503, 'doctor:network_invalid_reply') from None


async def execute(probes, deadline, *, transport=None):
    rows = []
    for probe in probes:
        remaining = deadline-time.monotonic()
        details = {'probe': probe.kind, 'attempted': False, 'credentials_accepted': None, 'reply_valid': False}
        if remaining <= 0:
            rows.append(entry(probe.identifier, 'doctor:network_budget_exhausted', 7, **details))
            continue
        details['attempted'] = True
        try:
            if probe.kind == 'llm_models':
                extra = await asyncio.wait_for(models(probe, remaining, transport), remaining)
            else:
                # Reuse the fixed official URL and bounded account response contract.
                await asyncio.wait_for(NaiDirectBackend(transport=transport, timeout=remaining).account(probe.token), remaining)
                extra = {}  # Never return or persist balance/account identifiers.
            rows.append(entry(probe.identifier, 'doctor:network_verified',
                              **{**details, 'credentials_accepted': True, 'reply_valid': True}, **extra))
        except (asyncio.TimeoutError, httpx.TimeoutException):
            rows.append(entry(probe.identifier, 'doctor:network_timeout', 7, **details))
        except ServiceError as error:
            allowed = {'doctor:network_unauthorized', 'doctor:network_rate_limited', 'doctor:network_probe_unsupported',
                       'doctor:network_redirect_rejected', 'doctor:network_http_error', 'doctor:network_invalid_reply',
                       'doctor:network_reply_too_large', 'nai:unauthorized', 'nai:forbidden', 'nai:rate_limited',
                       'nai:http_error', 'nai:invalid_response', 'nai:response_too_large', 'nai:account_unavailable'}
            code = error.code if error.code in allowed else 'doctor:network_failed'
            if code in {'doctor:network_unauthorized', 'nai:unauthorized', 'nai:forbidden'}:
                details['credentials_accepted'] = False
            rows.append(entry(probe.identifier, code, 6 if code == 'doctor:network_probe_unsupported' else 5, **details))
        except Exception:
            rows.append(entry(probe.identifier, 'doctor:network_failed', 5, **details))
    return rows


def diagnose_network(paths, *, timeout=30, transport=None):
    deadline = time.monotonic()+timeout
    rows, probes = collect(paths)
    if probes:
        rows.extend(asyncio.run(execute(probes, deadline, transport=transport)))
    return rows
