import asyncio
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.llm.types import LlmRequest
from stylelora.project_db.storage import atomic_json
from stylelora.project_db.validation import digest
from stylelora.runtime.errors import ServiceError
from stylelora.services.product_config import ProductConfig, DEFAULTS
from .api_support import APITest
from .test_generation_handoff import draft
from .test_llm import configure, reply, SCHEMA


class ProductDefaultsTests(APITest):
    def change(self, body, *, status=200):
        value = self.request('GET', '/api/config')
        return self.request('PATCH', '/api/config', body, version=value['version'], status=status,
                            headers={'Idempotency-Key': str(uuid4())})

    def test_v1_read_migration_preserves_bytes_and_commits_on_change(self):
        store = ProductConfig(self.runtime, self.base/'config')
        self.runtime.product_config = store
        old = {'schema_version': 1, 'revision': 4, 'storage_thresholds': DEFAULTS}
        store.path.parent.mkdir()
        atomic_json(store.path, {'content': old, 'digest': digest(old)})
        before = store.path.read_bytes()
        self.assertEqual(store.get()['cost_policy'], 'ask')
        self.change({'cost_policy': 'ask'})
        self.assertEqual(store.path.read_bytes(), before)
        saved = self.change({'wd_enabled': True, 'llm_limits': {'concurrency': 1}})
        self.assertEqual(saved['schema_version'], 2)
        self.assertEqual(saved['revision'], 5)
        self.assertEqual(saved['storage_thresholds'], DEFAULTS)
        self.assertEqual(ProductConfig(self.runtime, store.path.parent).get(), saved)

    def test_allow_confirmation_and_strict_fields(self):
        for body in ({}, {'confirm_allow': True}, {'cost_policy': None}, {'wd_enabled': 1},
                     {'llm_limits': {}}, {'llm_limits': {'concurrency': True}},
                     {'llm_limits': {'concurrency': 9}}, {'llm_limits': {'timeout_s': 601}},
                     {'mcp': {'discovery_mode': 'external'}}, {'mcp': {'path': 'secret'}},
                     {'cost_policy': 'allow'}, {'cost_policy': 'deny', 'confirm_allow': True}):
            with self.subTest(body=body):
                self.change(body, status=422)
        self.assertEqual(self.change({'cost_policy': 'allow', 'confirm_allow': True})['cost_policy'], 'allow')
        self.assertNotIn('confirm_allow', self.request('GET', '/api/config'))

    def test_source_default_copied_once_and_explicit_override(self):
        body = draft(); del body['cost_policy']
        self.change({'cost_policy': 'deny'})
        key = str(uuid4())
        first = self.request('POST', self.p+'/sources', body, key=key, status=201)
        self.assertEqual(first['cost_policy'], 'deny')
        self.change({'cost_policy': 'ask', 'wd_enabled': True})
        self.assertEqual(self.request('POST', self.p+'/sources', body, key=key, status=201), first)
        self.assertEqual(self.request('GET', self.p+'/sources/'+str(first['source_id']))['cost_policy'], 'deny')
        second = self.request('POST', self.p+'/sources', body, status=201)
        self.assertEqual(second['cost_policy'], 'ask')
        self.assertTrue(self.request('GET', '/api/executors/capabilities')['wd']['default_enabled'])
        self.assertEqual(self.request('GET', '/api/generation/capabilities')['default_cost_policy'], 'ask')

    def test_configured_discovery_requires_explicit_environment(self):
        self.change({'mcp': {'discovery_mode': 'configured'}})
        with patch.dict('os.environ', {'STYLELORA_LAUNCHER_APPDATA': ''}):
            with self.assertRaises(ServiceError) as raised:
                self.runtime.backends.client()
        self.assertEqual(raised.exception.code, 'config:mcp_directory_required')
        with patch.dict('os.environ', {'STYLELORA_LAUNCHER_APPDATA': str(self.base)}), \
             patch('stylelora.backends.nai_launcher.discovery.discover', return_value='fixture') as discover:
            client = self.runtime.backends.client()
            self.assertEqual(client.discovery(), 'fixture')
            discover.assert_called_once_with(appdata=str(self.base))
            asyncio.run(client.close())

    def test_new_llm_requests_freeze_limits_and_share_one_counter(self):
        configure(self.runtime)
        self.change({'llm_limits': {'concurrency': 1, 'timeout_s': 7}})
        async def exercise():
            binding = await self.runtime.llm.resolve_role('prompt')
            active, peak, budgets = 0, 0, []
            async def handler(request):
                nonlocal active, peak
                active += 1; peak = max(peak, active)
                await asyncio.sleep(.02)
                active -= 1
                return httpx.Response(200, json=reply('openai_chat'))
            self.runtime.llm.transport = httpx.MockTransport(handler)
            original = self.runtime.llm._send
            async def observed(request, endpoint, event, budget, **kw):
                budgets.append((budget.remaining, budget.concurrency))
                return await original(request, endpoint, event, budget, **kw)
            with patch.object(self.runtime.llm, '_send', side_effect=observed):
                await asyncio.gather(*(self.runtime.llm.complete_json(LlmRequest(str(uuid4()), binding, 'test', {}, SCHEMA)) for _ in range(3)))
            self.assertEqual(peak, 1)
            self.assertEqual(budgets, [(7, 1)]*3)
            self.assertEqual(self.runtime.llm.slots.used, 0)
        asyncio.run(exercise())

    def test_corrupt_limits_do_not_fall_back_or_send(self):
        store = ProductConfig(self.runtime, self.base/'config'); self.runtime.product_config = store
        store.path.parent.mkdir()
        bad = deepcopy(store.defaults()); bad['llm_limits']['concurrency'] = 99
        atomic_json(store.path, {'content': bad, 'digest': digest(bad)})
        configure(self.runtime)
        async def exercise():
            binding = await self.runtime.llm.resolve_role('prompt')
            with patch.object(self.runtime.llm, '_send') as send:
                with self.assertRaises(ServiceError):
                    await self.runtime.llm.complete_json(LlmRequest(str(uuid4()), binding, 'test', {}, SCHEMA))
                send.assert_not_called()
            self.assertFalse(self.runtime.llm.active)
        asyncio.run(exercise())
