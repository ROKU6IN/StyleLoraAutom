from copy import deepcopy
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from stylelora.llm.types import RolesInput, LlmFailure
from stylelora.prompt_production.composer import gate, validate, Composer
from stylelora.prompt_production.sampling import frozen_pool
from stylelora.prompt_production.types import DictionaryInput
from stylelora.prompt_production.tasks import PromptProduction
from stylelora.runtime.errors import ServiceError
from . import test_prompt_production as support
from .test_llm import configure, reply


def composed(tags, **kw):
    import re
    tags = sorted(tags, key=lambda t: 0 if t == 'no humans' or re.fullmatch(r'\d+(?:girl|boy|girls|boys)', t) else 1)
    return {'status': 'ok', 'tags': tags, 'natural_language': 'Trees stand beside a river.',
            'aspect': 'landscape', 'added': [], 'dropped': [], 'out_of_frame': [], **kw}


class ComposerTests(unittest.TestCase):
    setUp = support.PromptTests.setUp
    tearDown = support.PromptTests.tearDown
    request = support.PromptTests.request
    dictionary = support.PromptTests.dictionary
    sampling = support.PromptTests.sampling
    start = support.PromptTests.start
    wait = support.PromptTests.wait

    def inputs(self, n=1, **kw):
        d = self.dictionary({'kind': 'prompts', 'name': 'input', 'entries': [support.entry(
            ['forest', 'sunlight', 'no humans', 'tree ' + str(i)], rating='general', aspect='landscape') for i in range(n)]})
        return self.sampling(d, optimize=True, coverage_enabled=False, plan={'target_count': 1, 'random_seed': 42},
                             options={'tag_range': [1, 20]}, **kw)

    def mock(self, callback=None, protocol='openai_chat'):
        ep, _ = configure(self.runtime, protocol)
        self.calls = []
        def handle(request):
            body = json.loads(request.content)
            self.calls.append(body)
            if protocol == 'openai_chat':
                payload = json.loads(body['messages'][-1]['content'][0]['text'])
            elif protocol == 'openai_responses':
                payload = json.loads(body['input'][-1]['content'][0]['text'])
            else:
                payload = json.loads(body['messages'][-1]['content'][0]['text'])
            value = callback(payload) if callback else composed([t for tags in payload['features'].values() for t in tags])
            return httpx.Response(200, json=reply(protocol, json.dumps(value)))
        self.runtime.llm.transport = httpx.MockTransport(handle)
        return ep

    def finish(self, request):
        task, _ = self.start(request)
        value = self.wait(task['task_id'])
        return value, self.service.outcomes(task['task_id'])

    def test_three_protocols_preview_has_no_calls_and_snapshots_no_secrets(self):
        for protocol in ('openai_chat', 'openai_responses', 'anthropic_messages'):
            with self.subTest(protocol=protocol):
                self.mock(protocol=protocol)
                request = self.inputs()
                preview = self.service.preview(request)
                self.assertEqual(len(self.calls), 0)
                self.assertEqual(preview['refinement']['roles']['prompt']['protocol'], protocol)
                value, rows = self.finish(request)
                self.assertTrue(value['ready'], rows)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(rows[0]['status'], 'ok')
                frozen = json.dumps(self.service.load(value['task_id']))
                for private in ('base_url', '127.0.0.1', 'unit-test-secret', 'token_refs', 'project_id', 'source_id'):
                    self.assertNotIn(private, frozen)
                self.assertEqual(self.service.seeds(value['task_id'], value['revision'])['count'], 1)

    def test_original_nl_is_not_in_r1_and_feature_dimensions_retained(self):
        self.mock()
        d = self.dictionary({'kind': 'features', 'name': 'dims', 'dimensions': {
            'scene': [{'tag': 'forest', 'rating': 'general'}], 'count': [{'tag': 'no humans', 'rating': 'general'}]}})
        value, rows = self.finish(self.sampling(d, optimize=True, coverage_enabled=False,
                plan={'target_count': 1, 'random_seed': 42}, options={'tag_range': [1, 20]}))
        self.assertTrue(value['ready'], rows)
        payload = json.loads(self.calls[0]['messages'][-1]['content'][0]['text'])
        self.assertEqual(payload['features'], {'scene': ['forest'], 'count': ['no humans']})
        self.assertEqual(payload['rating_policy'], 'sfw')
        self.assertNotIn('natural_language', payload)

    def test_empty_roles_keep_compliant_original(self):
        value, rows = self.finish(self.inputs())
        self.assertTrue(value['ready'])
        self.assertEqual(rows[0]['reason'], 'role_empty')
        self.mock()
        d = self.dictionary({'kind': 'prompts', 'name': 'nsfw route', 'entries': [support.entry(['forest'], rating='questionable')]})
        value, rows = self.finish(self.sampling(d, optimize=True, coverage_enabled=False,
                 plan={'target_count': 1, 'random_seed': 1}, fence={'enabled': False}))
        self.assertEqual(rows[0]['reason'], 'role_empty')
        self.assertEqual(self.calls, [])

    def test_semantic_invalid_input_no_repair_and_two_refill_rounds(self):
        self.mock(lambda p: {'status': 'invalid_input', 'reason': 'no usable features', 'tags': [],
            'natural_language': None, 'aspect': None, 'added': [], 'dropped': [], 'out_of_frame': []})
        value, rows = self.finish(self.inputs(6))
        self.assertFalse(value['ready'])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['reason'] == 'invalid_input' for r in rows))
        self.assertEqual(value['progress']['deficits_reason'], 'refill_exhausted')
        self.assertEqual(value['coverage']['status'], 'search_exhausted')
        self.assertEqual(value['coverage']['unprocessed_candidates'], 3)
        self.assertFalse(value['coverage']['complete_pool'])

    def test_hint_contract_repair_once_then_refill(self):
        self.mock(lambda p: composed([t for v in p['features'].values() for t in v], aspect='square'))
        value, rows = self.finish(self.inputs())
        self.assertFalse(value['ready'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(rows[0]['reason'], 'invalid_contract')

    def test_refusal_escalates_and_rechecks_fence_without_calling_nsfw(self):
        self.mock(lambda p: {'status': 'refused_nsfw', 'reason': 'route', 'tags': [],
            'natural_language': None, 'aspect': None, 'added': [], 'dropped': [], 'out_of_frame': []})
        value, rows = self.finish(self.inputs())
        self.assertFalse(value['ready'])
        self.assertEqual(rows[0]['status'], 'fenced')
        self.assertEqual(len(self.calls), 1)
        request = self.inputs()
        request['fence']['enabled'] = False
        value, rows = self.finish(request)
        self.assertTrue(value['ready'])
        self.assertEqual(rows[0]['result']['rating'], 'questionable')
        self.assertEqual(rows[0]['reason'], 'role_empty')
        self.assertEqual(len(self.calls), 2)

    def nsfw_role(self, endpoint):
        roles = self.runtime.llm.config.roles()
        self.runtime.llm.config.save_roles(RolesInput.model_validate({'roles': {**roles['roles'],
            'prompt_nsfw': {'endpoint_id': endpoint['endpoint_id'], 'model': 'example/model'}}}), roles['version'])

    def test_nsfw_template_three_protocols_and_frozen_contract(self):
        for protocol in ('openai_chat', 'openai_responses', 'anthropic_messages'):
            with self.subTest(protocol=protocol):
                self.nsfw_role(self.mock(protocol=protocol))
                d = self.dictionary({'kind': 'prompts', 'name': 'restricted route', 'entries': [support.entry(
                    ['forest', 'no humans'], rating='questionable', aspect='landscape')]})
                spec = self.sampling(d, optimize=True, coverage_enabled=False, options={'tag_range': [1, 20]},
                    plan={'target_count': 1, 'random_seed': 1}, fence={'enabled': False})
                self.assertTrue(self.service.preview(spec)['refinement']['nsfw_template'])
                self.assertEqual(self.calls, [])
                value, rows = self.finish(spec)
                self.assertTrue(value['ready'], rows)
                self.assertEqual(rows[0]['status'], 'ok')
                self.assertEqual(rows[0]['result']['route'], 'nsfw')
                self.assertEqual(rows[0]['result']['rating'], 'questionable')
                serialized = json.dumps(self.calls[0])
                self.assertIn('restricted content editor', serialized)
                self.assertNotIn('This skill only handles sfw', serialized)
                self.assertEqual(len(self.calls), 1)
                self.assertIn('R1-nsfw', json.dumps(self.service.load(value['task_id'])))

    def test_refusal_reroutes_only_once_and_shared_budget(self):
        refusal = {'status': 'refused_nsfw', 'reason': 'route', 'tags': [],
            'natural_language': None, 'aspect': None, 'added': [], 'dropped': [], 'out_of_frame': []}
        def response(payload):
            if payload['rating_policy'] == 'sfw':
                return refusal
            return composed([t for tags in payload['features'].values() for t in tags])
        self.nsfw_role(self.mock(response))
        spec = self.inputs(); spec['fence']['enabled'] = False
        value, rows = self.finish(spec)
        self.assertTrue(value['ready'], rows)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(rows[0]['result']['rating'], 'questionable')
        self.assertEqual(rows[0]['result']['route'], 'nsfw')
        self.nsfw_role(self.mock(lambda p: refusal))
        value, rows = self.finish(spec)
        self.assertFalse(value['ready'])
        self.assertEqual(rows[0]['reason'], 'upstream_refused')
        self.assertEqual(len(self.calls), 2)

    def test_legacy_composer_retains_missing_nsfw_contract(self):
        from stylelora.prompt_production.composer import dependencies
        frozen = dependencies(self.runtime)
        frozen['version'] = 'composer-v1'
        frozen['templates'] = {name: self.runtime.llm.templates.snapshot(name) for name in ('R0', 'R1-sfw', 'R5')}
        self.nsfw_role(self.mock())
        with patch('stylelora.prompt_production.tasks.dependencies', return_value=frozen):
            d = self.dictionary({'kind': 'prompts', 'name': 'legacy route', 'entries': [support.entry(['forest'], rating='questionable')]})
            value, rows = self.finish(self.sampling(d, optimize=True, coverage_enabled=False,
                plan={'target_count': 1, 'random_seed': 1}, fence={'enabled': False}))
        self.assertTrue(value['ready'], rows)
        self.assertEqual(rows[0]['reason'], 'nsfw_contract_missing')
        self.assertEqual(self.calls, [])

    def test_r5_unknown_then_refine_preserves_known_lower_bound(self):
        def response(p):
            if 'features' not in p:
                return {'rating': 'general', 'reason': 'visible scene'}
            return composed([t for v in p['features'].values() for t in v])
        ep = self.mock(response)
        roles = self.runtime.llm.config.roles()
        self.runtime.llm.config.save_roles(RolesInput.model_validate({'roles': {**roles['roles'],
            'rating': {'endpoint_id': ep['endpoint_id'], 'model': 'example/model'}}}), roles['version'])
        d = self.dictionary({'kind': 'features', 'name': 'unknown', 'dimensions': {
            'scene': [{'tag': 'forest', 'rating': 'sensitive'}], 'count': [{'tag': 'no humans'}]}})
        value, rows = self.finish(self.sampling(d, optimize=True, coverage_enabled=False,
            plan={'target_count': 1, 'random_seed': 1}, options={'tag_range': [1, 20]}, fence={'use_rating_model': True}))
        self.assertTrue(value['ready'], rows)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(rows[0]['result']['rating'], 'sensitive')
        self.assertTrue(rows[0]['result']['verified'])

    def test_timeout_unknown_is_not_retried(self):
        self.mock()
        with patch.object(self.runtime.llm, 'complete_json', side_effect=LlmFailure('timeout', outcome='unknown')) as call:
            value, rows = self.finish(self.inputs())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(rows[0]['reason'], 'llm_outcome_unknown')
        self.assertFalse(value['ready'])

    def test_auth_pauses_batch_then_resume_original_binding(self):
        self.mock()
        with patch.object(self.runtime.llm, 'complete_json', side_effect=LlmFailure('unauthorized')):
            task, _ = self.start(self.inputs())
            value = self.wait(task['task_id'], 'paused')
        self.assertEqual(value['reason'], 'llm:unauthorized')
        self.await_idle(task['task_id'])
        value = self.service.get(task['task_id'])
        self.service.control(task['task_id'], {'action': 'resume', 'expected_revision': value['revision']})
        done = self.wait(task['task_id'])
        self.assertTrue(done['ready'])
        self.assertEqual(len(self.calls), 1)

    def await_idle(self, identifier):
        for _ in range(200):
            if identifier not in self.service.refinement_busy:
                return
            time.sleep(.01)
        self.fail('worker still active')

    def test_pause_accepts_inflight_cancel_discards_and_restart_no_resend(self):
        for action in ('pause', 'cancel'):
            entered, release = threading.Event(), threading.Event()
            def response(p):
                entered.set()
                release.wait(5)
                return composed([t for v in p['features'].values() for t in v])
            self.mock(response)
            task, _ = self.start(self.inputs(2))
            self.assertTrue(entered.wait(5))
            current = self.service.get(task['task_id'])
            self.service.control(task['task_id'], {'action': action, 'expected_revision': current['revision']})
            release.set()
            self.await_idle(task['task_id'])
            rows = self.service.outcomes(task['task_id'])
            self.assertEqual(rows[0]['status'], 'ok' if action == 'pause' else 'failed')
            if action == 'pause':
                current = self.service.get(task['task_id'])
                self.service.control(task['task_id'], {'action': 'resume', 'expected_revision': current['revision']})
                self.wait(task['task_id'])
                self.assertEqual(len(self.calls), 1)
        # Simulate a crashed process with one uncertain in-flight entry.
        value = self.service.load(task['task_id'])
        value['status'] = 'running'
        value['execution']['outcomes'][0]['status'] = 'running'
        self.service.files.put('prompt_tasks', task['task_id'], value)
        restored = PromptProduction(self.runtime, self.root/'product')
        self.assertEqual(restored.get(task['task_id'])['status'], 'paused')
        self.assertEqual(restored.outcomes(task['task_id'])[0]['reason'], 'llm_outcome_unknown')

    def test_dictionary_append_preview_replay_cas_and_two_file_gap(self):
        self.mock()
        value, rows = self.finish(self.inputs())
        target = self.dictionary({'kind': 'prompts', 'name': 'target', 'entries': []})
        selection = {'target_id': target['dictionary_id'], 'indices': [rows[0]['index']]}
        preview = self.request('POST', '/tasks/' + value['task_id'] + '/append-preview', selection)
        body = {**selection, 'expected_revision': preview['expected_revision'], 'target_version': preview['target_version']}
        original = self.service.files.put
        def interrupted(group, identifier, payload):
            if group == 'prompt_tasks':
                raise ServiceError(503, 'prompt:write_failed')
            return original(group, identifier, payload)
        with patch.object(self.service.files, 'put', side_effect=interrupted):
            self.request('POST', '/tasks/' + value['task_id'] + '/append', body, status=503)
        self.assertEqual(len(self.service.dictionaries.get(target['dictionary_id'])['entries']), 1)
        result = self.request('POST', '/tasks/' + value['task_id'] + '/append', body)
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(self.request('POST', '/tasks/' + value['task_id'] + '/append', body), result)
        self.assertEqual(len(self.service.get(value['task_id'])['appends']), 1)

    def test_automatic_append_only_successful_optimized_and_conflict_keeps_result(self):
        self.mock()
        target = self.dictionary({'kind': 'prompts', 'name': 'target', 'entries': []})
        value, rows = self.finish(self.inputs(append_target_id=target['dictionary_id']))
        self.await_idle(value['task_id'])
        self.assertEqual(len(self.service.dictionaries.get(target['dictionary_id'])['entries']), 1)
        self.assertEqual(len(self.service.get(value['task_id'])['appends']), 1)
        # Empty role is skipped, so automatic append cannot silently copy originals.
        roles = self.runtime.llm.config.roles()
        self.runtime.llm.config.save_roles(RolesInput.model_validate({'roles': {}}), roles['version'])
        value, _ = self.finish(self.inputs(append_target_id=target['dictionary_id']))
        self.await_idle(value['task_id'])
        self.assertEqual(self.service.get(value['task_id'])['appends'], [])

    def test_gate_object_exceptions_disambiguation_and_semantic_review(self):
        request = self.inputs()
        snapshot = self.service.freeze(request)
        for term in ('watercolor background', 'soft shading', 'camera', 'gradient', '8k', '16:9', 'portrait orientation',
                     'line_art', 'oil painting (medium)', 'English_Text', 'in the style of someone', 'dreamy atmosphere'):
            entry = {'tags': [term], 'natural_language': None}
            self.assertIsNotNone(gate(entry, entry, snapshot), term)
        for term in ('gradient hair', 'pastel pink dress', 'border collie', 'gothic architecture', 'wide shot', 'ganyu (genshin impact)'):
            entry = {'tags': [term], 'natural_language': None}
            self.assertIsNone(gate(entry, entry, snapshot), term)
        candidate = {'entry': support.entry(['forest'], aspect='landscape'), 'rating': 'general'}
        self.assertEqual(validate(composed(['forest'], added=['forest']), candidate, snapshot), 'invalid_contract')
        self.assertEqual(validate(composed(['tree'], added=['tree'], out_of_frame=['forest'],
                         natural_language='A forest is visible.'), candidate, snapshot), 'invalid_contract')

    def test_parallel_completion_is_ordered_and_global_gate_is_two(self):
        import asyncio
        lock = threading.Lock()
        active, peak = 0, 0
        self.mock()
        async def response(request):
            nonlocal active, peak
            payload = json.loads(json.loads(request.content)['messages'][-1]['content'][0]['text'])
            with lock:
                active += 1
                peak = max(active, peak)
            await asyncio.sleep(.04)
            with lock:
                active -= 1
            return httpx.Response(200, json=reply(text=json.dumps(composed([t for v in payload['features'].values() for t in v]))))
        self.runtime.llm.transport = httpx.MockTransport(response)
        request = self.inputs(8)
        request['plan']['target_count'] = 4
        tasks = [self.start(request)[0] for _ in range(2)]
        for task in tasks:
            self.assertTrue(self.wait(task['task_id'])['ready'])
            rows = self.service.outcomes(task['task_id'])
            self.assertEqual([r['index'] for r in rows], sorted(r['index'] for r in rows))
        self.assertEqual(peak, 2)

    def test_known_reply_survives_write_failure_without_model_recall(self):
        self.mock()
        original = self.service.files.put
        blocked = threading.Event()
        def fail_reply(group, identifier, payload):
            if group == 'prompt_tasks' and payload.get('execution') and any(r['status'] == 'ok' for r in payload['execution']['outcomes']):
                blocked.set()
                raise ServiceError(503, 'prompt:write_failed')
            return original(group, identifier, payload)
        with patch.object(self.service.files, 'put', side_effect=fail_reply):
            task, _ = self.start(self.inputs())
            self.assertTrue(blocked.wait(5))
            self.await_idle(task['task_id'])
            self.assertIn(task['task_id'], self.service.pending_commits)
        value = self.service.get(task['task_id'])
        self.service.control(task['task_id'], {'action': 'resume', 'expected_revision': value['revision']})
        self.assertTrue(self.wait(task['task_id'])['ready'])
        self.assertEqual(len(self.calls), 1)

    def test_frozen_role_change_pauses_without_using_replacement(self):
        ep = self.mock()
        request = self.inputs()
        snapshot = self.service.freeze(request)
        config = self.runtime.llm.config
        from stylelora.llm.types import EndpointInput
        updated = {k: ep[k] for k in ('base_url', 'protocol', 'models')}
        updated['base_url'] = 'http://127.0.0.1:19284/v1'
        config.save(EndpointInput.model_validate(updated), ep['endpoint_id'], config.view(ep['endpoint_id'])['version'])
        pool, _, _ = frozen_pool(snapshot)
        with self.assertRaisesRegex(ServiceError, 'frozen dependencies changed'):
            Composer(self.runtime, snapshot, threading.Event()).compose({'input': pool[0]}, lambda changes: None)
        self.assertEqual(self.calls, [])

    def test_append_revision_conflict_retains_ready_seeds(self):
        entered, release = threading.Event(), threading.Event()
        def response(p):
            entered.set()
            release.wait(5)
            return composed([t for v in p['features'].values() for t in v])
        self.mock(response)
        target = self.dictionary({'kind': 'prompts', 'name': 'target', 'entries': []})
        task, _ = self.start(self.inputs(append_target_id=target['dictionary_id']))
        self.assertTrue(entered.wait(5))
        self.request('PUT', '/dictionaries/' + target['dictionary_id'], {'kind': 'prompts', 'name': 'updated', 'entries': []}, version=target['version'])
        release.set()
        self.wait(task['task_id'])
        self.await_idle(task['task_id'])
        value = self.service.get(task['task_id'])
        self.assertTrue(value['ready'])
        self.assertIsNotNone(value['append_error'])
        self.assertEqual(self.service.seeds(task['task_id'], value['revision'])['count'], 1)
        self.assertEqual(self.service.dictionaries.get(target['dictionary_id'])['entries'], [])

    def test_refill_after_coverage_drift_and_canonical_duplicate(self):
        self.mock()
        d = self.dictionary()
        request = self.sampling(d, optimize=True, options={'tag_range': [1, 30]})
        # Real validator plus fake model: the first round moves every scene to a new
        # tag, so quotas must reject it and replenish from untouched candidates.
        seen = []
        def response(p):
            tags = [t for v in p['features'].values() for t in v]
            seen.append(tags)
            if len(seen) <= 12:
                old = next(t for t in tags if t in ('forest', 'beach', 'city street'))
                return composed([t for t in tags if t != old] + ['river'], added=['river'], dropped=[{'tag': old, 'reason': 'scene'}])
            return composed(tags)
        self.mock(response)
        value, rows = self.finish(request)
        self.assertTrue(value['ready'], rows)
        self.assertEqual(sum(r['reason'] == 'coverage_drift' for r in rows), 12)
        self.assertEqual(value['coverage']['count'], 12)
        self.assertEqual(len(rows), 24)

    def test_r5_can_resolve_fallback_and_partial_keywords_stay_unverified(self):
        ep = self.mock(lambda p: {'rating': 'general', 'reason': 'scene'} if 'features' not in p else composed([t for v in p['features'].values() for t in v]))
        roles = self.runtime.llm.config.roles()
        self.runtime.llm.config.save_roles(RolesInput.model_validate({'roles': {**roles['roles'], 'rating': {
            'endpoint_id': ep['endpoint_id'], 'model': 'example/model'}}}), roles['version'])
        d = self.dictionary({'kind': 'prompts', 'name': 'unknown', 'entries': [support.entry(['forest', 'no humans'])]})
        self.runtime.vision.policy.current['keywords'] = [{'tag': 'forest', 'rating': 'general'}]
        request = self.sampling(d, optimize=True, coverage_enabled=False, plan={'target_count': 1, 'random_seed': 1},
            options={'tag_range': [1, 20]}, fence={'use_rating_model': True, 'nsfwfallback': True})
        preview = self.service.preview(request)
        self.assertTrue(preview['ready'])
        self.assertFalse(preview['examples'][0]['verified'])
        value, rows = self.finish(request)
        self.assertTrue(value['ready'], rows)
        self.assertEqual(len(self.calls), 2)

    def test_r5_and_r1_share_one_schema_repair_budget(self):
        ep = self.mock()
        roles = self.runtime.llm.config.roles()
        self.runtime.llm.config.save_roles(RolesInput.model_validate({'roles': {**roles['roles'], 'rating': {
            'endpoint_id': ep['endpoint_id'], 'model': 'example/model'}}}), roles['version'])
        calls = []
        def response(request):
            payload = json.loads(json.loads(request.content)['messages'][-1]['content'][0]['text'])
            calls.append(payload)
            if len(calls) == 1:
                text = '{invalid json'
            elif 'features' not in payload:
                text = json.dumps({'rating': 'general', 'reason': 'scene'})
            else:
                text = json.dumps(composed([t for v in payload['features'].values() for t in v], aspect='square'))
            return httpx.Response(200, json=reply(text=text))
        self.runtime.llm.transport = httpx.MockTransport(response)
        d = self.dictionary({'kind': 'prompts', 'name': 'unknown', 'entries': [support.entry(['forest'], aspect='landscape')]})
        value, rows = self.finish(self.sampling(d, optimize=True, coverage_enabled=False, plan={'target_count': 1, 'random_seed': 1},
            options={'tag_range': [1, 20]}, fence={'use_rating_model': True}))
        self.assertFalse(value['ready'])
        self.assertEqual(rows[0]['reason'], 'invalid_contract')
        self.assertEqual(len(calls), 3)

    def test_refined_duplicates_are_not_evaded_by_different_nl(self):
        def response(p):
            original = [t for v in p['features'].values() for t in v]
            kept = [t for t in original if not t.startswith('tree ')]
            return composed(kept, dropped=[{'tag': t, 'reason': 'outside scene'} for t in original if t not in kept])
        self.mock(response)
        request = self.inputs(8)
        request['plan']['target_count'] = 2
        value, rows = self.finish(request)
        self.assertFalse(value['ready'])
        self.assertLessEqual(len(rows), 6)
        self.assertEqual(value['coverage']['available'], 1)
        self.request('POST', '/tasks/' + value['task_id'] + '/seeds', {'expected_revision': value['revision']}, status=409)

    def test_r5_checkpoint_write_failure_resumes_r1_without_repeating_rating(self):
        ep = self.mock(lambda p: {'rating': 'general', 'reason': 'scene'} if 'features' not in p else composed([t for v in p['features'].values() for t in v]))
        roles = self.runtime.llm.config.roles()
        self.runtime.llm.config.save_roles(RolesInput.model_validate({'roles': {**roles['roles'], 'rating': {
            'endpoint_id': ep['endpoint_id'], 'model': 'example/model'}}}), roles['version'])
        d = self.dictionary({'kind': 'prompts', 'name': 'unknown', 'entries': [support.entry(['forest', 'no humans'])]})
        request = self.sampling(d, optimize=True, coverage_enabled=False, plan={'target_count': 1, 'random_seed': 1},
            options={'tag_range': [1, 20]}, fence={'use_rating_model': True})
        original, failed = self.service.files.put, threading.Event()
        def fail_checkpoint(group, identifier, value):
            if group == 'prompt_tasks' and value.get('execution') and any(r['phase'] == 'rated' for r in value['execution']['outcomes']):
                failed.set()
                raise ServiceError(503, 'prompt:io_error')
            return original(group, identifier, value)
        with patch.object(self.service.files, 'put', side_effect=fail_checkpoint):
            task, _ = self.start(request)
            self.assertTrue(failed.wait(5))
            self.await_idle(task['task_id'])
        current = self.service.get(task['task_id'])
        self.assertEqual(len(self.calls), 1)
        self.service.control(task['task_id'], {'action': 'resume', 'expected_revision': current['revision']})
        self.assertTrue(self.wait(task['task_id'])['ready'])
        self.assertEqual(len(self.calls), 2)

    def test_new_local_tasks_apply_content_gate_without_llm(self):
        d = self.dictionary({'kind': 'prompts', 'name': 'content', 'entries': [support.entry(['no humans', 'soft shading'], rating='general')]})
        with patch.object(self.runtime.llm, 'complete_json', side_effect=AssertionError('No remote calls')):
            value, rows = self.finish(self.sampling(d, coverage_enabled=False, plan={'target_count': 1, 'random_seed': 1}))
        self.assertFalse(value['ready'])
        self.assertEqual(rows, [])
        self.assertEqual(value['coverage']['diagnostics']['forbidden_content'], 1)
