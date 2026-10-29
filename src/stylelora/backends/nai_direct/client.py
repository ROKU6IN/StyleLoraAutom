"""Bounded HTTP, fixed official hosts, no redirects/proxy environment/retries.

Transport injection is Python-only for local tests. No configurable remote URL
is accepted through the product API or project configuration.
"""
import asyncio
from io import BytesIO
import re
import zipfile

import httpx

from ...runtime.contracts import strict_json
from ...runtime.errors import ServiceError

MAX_RESPONSE = 40 * 1024 * 1024
MAX_PNG = 32 * 1024 * 1024


def unpack_png(data):
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                raise ValueError()
            entry = entries[0]
            # Never extract a server-supplied path. Also reject unusual archives
            # rather than silently accepting hidden files or duplicate members.
            if (not re.fullmatch(r'[A-Za-z0-9_.-]+\.png', entry.filename)
                    or entry.is_dir() or entry.flag_bits & 1
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    or not 1 <= entry.file_size <= MAX_PNG
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError()
            with archive.open(entry) as stream:
                png = stream.read(MAX_PNG + 1)
            if len(png) != entry.file_size or png[:8] != b'\x89PNG\r\n\x1a\n':
                raise ValueError()
            return png
    except Exception:
        raise ServiceError(503, 'nai:invalid_image_response', outcome='unknown') from None


class NaiDirectBackend:
    def __init__(self, *, transport=None, timeout=180):
        self.transport, self.timeout = transport, timeout

    async def _request(self, method, url, token, *, body=None, limit=MAX_RESPONSE):
        if not isinstance(token, str) or not re.fullmatch(r'[\x21-\x7e]{1,8192}', token):
            raise ServiceError(503, 'nai:token_missing')
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(self.timeout, connect=15), limits=httpx.Limits(max_connections=1)) as client:
                async with client.stream(method, url, json=body, headers={
                    'Authorization': 'Bearer ' + token,
                    'Accept': 'application/zip' if method == 'POST' else 'application/json',
                    'Accept-Encoding': 'identity',
                }) as response:
                    if response.status_code not in ((201,) if method == 'POST' else (200,)):
                        code = {401: 'unauthorized', 403: 'forbidden', 429: 'rate_limited'}.get(
                            response.status_code, 'http_error')
                        raise ServiceError(503, 'nai:' + code, outcome='unknown' if method == 'POST' else 'rejected')
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise ServiceError(503, 'nai:invalid_response', outcome='unknown')
                    expected = 'application/zip' if method == 'POST' else 'application/json'
                    if response.headers.get('content-type', '').split(';')[0].strip().lower() != expected:
                        raise ServiceError(503, 'nai:invalid_response', outcome='unknown')
                    size = response.headers.get('content-length')
                    if size and (not size.isdigit() or int(size) > limit):
                        raise ServiceError(503, 'nai:response_too_large', outcome='unknown')
                    data = bytearray()
                    async for chunk in response.aiter_bytes(65536):
                        if len(data) + len(chunk) > limit:
                            raise ServiceError(503, 'nai:response_too_large', outcome='unknown')
                        data.extend(chunk)
                    return bytes(data)
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(503, 'nai:request_unknown' if method == 'POST' else 'nai:account_unavailable',
                               outcome='unknown' if method == 'POST' else 'rejected') from None

    async def generate(self, token, body):
        try:
            data = await asyncio.wait_for(self._request('POST', 'https://image.novelai.net/ai/generate-image',
                                                       token, body=body), self.timeout + 1)
            return unpack_png(data)
        except asyncio.TimeoutError:
            raise ServiceError(503, 'nai:request_unknown', outcome='unknown') from None

    async def account(self, token):
        try:
            data = await asyncio.wait_for(self._request('GET', 'https://api.novelai.net/user/subscription',
                                                       token, limit=65536), 30)
            value = strict_json(data)
            # Drop payment/account identifiers and every unreviewed field.
            if type(value.get('active')) is not bool or type(value.get('tier')) is not int:
                raise ValueError()
            steps = value.get('trainingStepsLeft', {})
            fixed, purchased = steps.get('fixedTrainingStepsLeft'), steps.get('purchasedTrainingSteps')
            if any(type(v) is not int or not 0 <= v <= 9007199254740991 for v in (fixed, purchased)):
                raise ValueError()
            return {'active': value['active'], 'tier': value['tier'], 'subscription_anlas': fixed,
                    'purchased_anlas': purchased, 'estimated_anlas': None, 'free_generation_verified': False}
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(503, 'nai:account_unavailable') from None
