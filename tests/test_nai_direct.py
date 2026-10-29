import asyncio
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from PIL import Image, PngImagePlugin

from stylelora.backends.nai_direct.client import NaiDirectBackend, unpack_png, MAX_PNG
from stylelora.backends.nai_direct.profile import DEFAULT_CONFIG, payload, capabilities
from stylelora.generation.direct import read_receipt, journal_path
from stylelora.generation.profile import validate_capabilities
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import sources
from stylelora.project_db.storage import atomic_json
from stylelora.project_db.types import ItemSeed
from stylelora.project_db.validation import digest
from stylelora.runtime.errors import ServiceError
from .api_support import APITest
from . import test_generation_handoff as handoff


def png_bytes(body):
    stream = BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text('Comment', json.dumps({'prompt': body['input'], 'seed': body['parameters']['seed']}))
    Image.new('RGB', (body['parameters']['width'], body['parameters']['height']), 'navy').save(stream, 'PNG', pnginfo=info)
    return stream.getvalue()


def zipped(png, name='image_0.png'):
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr(name, png)
    return output.getvalue()


def sample_request():
    return {'positive': 'forest', 'negative': 'blur', 'width': 512, 'height': 768,
            'count': 1, 'operation': 'generate', 'characters': []}


@contextmanager
def local_endpoint(*, drop=False):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, body))
            if drop:
                self.close_connection = True
                return
            data = zipped(png_bytes(body))
            self.send_response(201)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    class LocalOnlyTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport(retries=0)
        async def handle_async_request(self, request):
            assert request.url.host == 'image.novelai.net'
            request.url = request.url.copy_with(scheme='http', host='127.0.0.1', port=server.server_port)
            return await self.inner.handle_async_request(request)
        async def aclose(self):
            await self.inner.aclose()
    try:
        yield LocalOnlyTransport(), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class DirectContractTests(unittest.TestCase):
    def test_wire_is_complete_request_scoped_and_deterministic(self):
        value = payload(DEFAULT_CONFIG, sample_request(), 42)
        self.assertEqual(value, payload(deepcopy(DEFAULT_CONFIG), sample_request(), 42))
        self.assertEqual(value['model'], 'nai-diffusion-5-full')
        p = value['parameters']
        self.assertEqual((p['seed'], p['n_samples'], p['steps'], p['ucPreset']), (42, 1, 23, 3))
        self.assertFalse(p['qualityToggle'])
        self.assertEqual(p['v4_prompt']['caption'], {'base_caption': 'forest', 'char_captions': []})
        self.assertEqual(p['v4_negative_prompt']['caption']['base_caption'], 'blur')
        self.assertFalse(any('reference' in key for key in p))
        self.assertEqual(set(value), {'input', 'model', 'action', 'parameters'})

    def test_second_supported_model_and_parameter_bounds(self):
        config = deepcopy(DEFAULT_CONFIG)
        config['profile'].update(model='nai-diffusion-4-5-full', steps=50, scale=10, cfg_rescale=1)
        self.assertEqual(payload(config, sample_request(), 0)['model'], 'nai-diffusion-4-5-full')

    def test_unreviewed_models_effort_flags_and_endpoints_rejected(self):
        for key, value in [('model', 'other'), ('effort', 'medium'), ('sampler', 'other'), ('steps', 51),
                           ('noise_schedule', 'native'), ('smea', True), ('variety_plus', True), ('decrisp', True)]:
            with self.subTest(key=key):
                config = deepcopy(DEFAULT_CONFIG)
                config['profile'][key] = value
                with self.assertRaises(ServiceError):
                    payload(config, sample_request(), 1)
        config = deepcopy(DEFAULT_CONFIG)
        config['base_url'] = 'https://untrusted.invalid'
        with self.assertRaises(ServiceError):
            validate_capabilities(config, '', capabilities())

    def test_multisample_references_and_invalid_seed_rejected(self):
        for patch_value in ({'count': 2}, {'characters': [{}]}, {'operation': 'img2img'}, {'width': 1600}):
            with self.assertRaises(ServiceError):
                payload(DEFAULT_CONFIG, {**sample_request(), **patch_value}, 1)
        for seed in (-1, 2**32, True):
            with self.assertRaises(ServiceError):
                payload(DEFAULT_CONFIG, sample_request(), seed)

    def test_zip_uses_bytes_and_never_extracts_server_path(self):
        png = png_bytes(payload(DEFAULT_CONFIG, sample_request(), 1))
        self.assertEqual(unpack_png(zipped(png)), png)
        for name in ('../out.png', '/out.png', 'C:/out.png', 'nested/out.png', 'out.jpg'):
            with self.subTest(name=name), self.assertRaises(ServiceError):
                unpack_png(zipped(png, name))

    def test_multiple_empty_and_corrupt_archives_rejected(self):
        stream = BytesIO()
        with ZipFile(stream, 'w') as archive:
            archive.writestr('one.png', b'bad')
            archive.writestr('two.png', b'bad')
        for data in (b'not a zip', zipped(b'not png'), stream.getvalue()):
            with self.assertRaises(ServiceError):
                unpack_png(data)

    def test_advertised_uncompressed_size_bounded(self):
        import struct
        raw = bytearray(zipped(b'\x89PNG\r\n\x1a\n'))
        offset = raw.index(b'PK\x01\x02')
        struct.pack_into('<I', raw, offset + 24, MAX_PNG + 1)
        with self.assertRaises(ServiceError):
            unpack_png(bytes(raw))

    def test_http_uses_only_official_image_host_and_bearer(self):
        calls = []
        def respond(request):
            calls.append(request)
            self.assertEqual(str(request.url), 'https://image.novelai.net/ai/generate-image')
            self.assertEqual(request.headers['authorization'], 'Bearer fixture-token')
            self.assertEqual(request.headers['accept'], 'application/zip')
            return httpx.Response(201, content=zipped(png_bytes(json.loads(request.content))), headers={'content-type': 'application/zip'})
        backend = NaiDirectBackend(transport=httpx.MockTransport(respond))
        png = asyncio.run(backend.generate('fixture-token', payload(DEFAULT_CONFIG, sample_request(), 7)))
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertEqual(len(calls), 1)

    def test_http_errors_redact_body_and_never_retry_or_redirect(self):
        for status in (302, 401, 403, 429, 500):
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, text='PRIVATE_UPSTREAM_BODY', headers={'location': 'https://untrusted.invalid'})
            backend = NaiDirectBackend(transport=httpx.MockTransport(respond))
            with self.subTest(status=status), self.assertRaises(ServiceError) as caught:
                asyncio.run(backend.generate('fixture-token', {}))
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertEqual(caught.exception.outcome, 'unknown')
            self.assertEqual(len(calls), 1)

    def test_invalid_mime_compression_and_oversize_rejected(self):
        for headers in ({'content-type': 'text/html'}, {'content-type': 'application/zip', 'content-length': str(50*1024*1024)},
                        {'content-type': 'application/zip', 'content-encoding': 'gzip'}):
            with self.subTest(headers=headers), self.assertRaises(ServiceError):
                backend = NaiDirectBackend(transport=httpx.MockTransport(lambda _: httpx.Response(201, headers=headers)))
                asyncio.run(backend.generate('fixture-token', {}))

    def test_transport_timeout_is_unknown_without_retry(self):
        calls = []
        def respond(request):
            calls.append(request)
            raise httpx.ReadTimeout('PRIVATE_TOKEN')
        with self.assertRaises(ServiceError) as caught:
            asyncio.run(NaiDirectBackend(transport=httpx.MockTransport(respond)).generate('fixture-token', {}))
        self.assertEqual(caught.exception.code, 'nai:request_unknown')
        self.assertEqual(len(calls), 1)

    def test_account_projection_has_no_payment_or_token_fields(self):
        def respond(request):
            self.assertEqual(str(request.url), 'https://api.novelai.net/user/subscription')
            return httpx.Response(200, json={'active': True, 'tier': 3, 'paymentProcessorData': {'token': 'PRIVATE'},
                'trainingStepsLeft': {'fixedTrainingStepsLeft': 100, 'purchasedTrainingSteps': 20}})
        result = asyncio.run(NaiDirectBackend(transport=httpx.MockTransport(respond)).account('fixture-token'))
        self.assertEqual(result['purchased_anlas'], 20)
        self.assertFalse(result['free_generation_verified'])
        self.assertIsNone(result['estimated_anlas'])
        self.assertNotIn('PRIVATE', str(result))

    def test_malformed_account_not_interpreted_as_zero_or_free(self):
        for value in ({}, {'active': True, 'tier': 3, 'trainingStepsLeft': {'fixedTrainingStepsLeft': True, 'purchasedTrainingSteps': 5}}):
            with self.assertRaises(ServiceError):
                asyncio.run(NaiDirectBackend(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))).account('fixture-token'))


class DirectGenerationTests(APITest):
    task = handoff.GenerationTests.task

    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {'STYLELORA_NAI_TOKEN': 'fixture-token'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service = self.runtime.nai_direct
        from stylelora.tagging.library import CatalogSnapshot
        self.runtime.catalog.state['catalog'] = {'complete': True, 'tags': [{'tag': 'forest', 'aliases': [], 'category': 'general'}]}
        self.runtime.catalog._snapshot = CatalogSnapshot(self.runtime.catalog.state)
        self.driver = self.service.driver
        self.project, self.context = self.runtime.context.handle, self.runtime.context
        self.calls = []
        self.handler = None
        def respond(request):
            self.calls.append(request)
            if self.handler:
                return self.handler(request)
            return httpx.Response(201, content=zipped(png_bytes(json.loads(request.content))), headers={'content-type': 'application/zip'})
        self.service.backend = NaiDirectBackend(transport=httpx.MockTransport(respond))

    def create(self, *, count=1, policy='allow', start=True):
        body = {'name': 'Direct source', 'config': deepcopy(DEFAULT_CONFIG), 'style_prompt': 'geometric style',
                'negative_prompt': 'blur', 'target_count': count, 'cost_policy': policy}
        self.source = self.request('POST', self.p+'/sources', body, status=201)
        self.sid = self.source['source_id']
        self.path = self.p+f'/sources/{self.sid}'
        if start:
            submit_write(self.project, lambda db: commands.start_source(db, self.sid,
                [ItemSeed(i+1, f'forest, tree {i}') for i in range(count)])).result()
        return self.source

    def item(self, seq=1):
        with read_snapshot(self.project) as db:
            iid = db.execute('SELECT item_id FROM source_items WHERE source_id=? AND seq=?', (self.sid, seq)).fetchone()[0]
            return sources.get_item(db, iid)

    def view(self):
        return self.request('GET', self.path)

    def cycle(self):
        self.driver.dispatch(self.context, self.sid)
        self.driver.wait()

    def control(self, action, status=200):
        result = self.request('POST', self.path+'/control', {'action': action}, version=self.view()['version'], status=status)
        self.driver.wait()
        return result

    def test_generation_registers_and_exports_without_launcher(self):
        self.create(count=2)
        with patch.object(self.runtime.settings_leases, 'status', side_effect=AssertionError('No shared settings')):
            self.cycle()
        self.assertEqual(self.view()['status'], 'done')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.context.users, 0)
        self.assertEqual([self.item(i).status for i in (1, 2)], ['done', 'done'])
        self.keep([self.item(i).image_id for i in (1, 2)])
        report = self.run_export(self.export_preview())
        self.assertEqual(report['status'], 'finished')
        summary = self.request('GET', f'/api/backends/nai_direct/account-status?project_id={self.pid}&source_id={self.sid}')
        self.assertIsNone(summary['estimated_anlas'])
        self.assertEqual(summary['unpriced_attempts'], 2)
        self.assertEqual(summary['counts']['submitted'], 2)

    def test_http_handoff_locks_and_dispatches_direct_source(self):
        self.create(start=False)
        task = self.task(1)
        preview = self.request('POST', self.path+'/handoff-preview', {'task_id': task['task_id'], 'expected_task_revision': task['revision']}, version=self.source['version'])
        self.assertTrue(preview['can_start'], preview)
        body = {k: preview[k] for k in ('preview_id', 'digest', 'source_digest', 'seeds_digest', 'count')}
        self.request('POST', self.path+'/start', body, version=self.source['version'])
        self.driver.wait()
        self.assertEqual(self.view()['status'], 'done')
        self.request('POST', self.path+'/start', body, version=self.source['version'])
        self.assertEqual(len(self.calls), 1)

    def test_ask_pauses_for_unknown_cost_then_confirms_cycle(self):
        self.create(policy='ask')
        self.cycle()
        self.assertFalse(self.calls)
        state = self.request('GET', self.path+'/cost-state')
        self.assertTrue(state['can_confirm'])
        self.assertIsNone(state['pending']['estimated_anlas'])
        self.request('POST', self.path+'/confirm-cost', {'cycle_id': state['cycle_id']}, version=state['source_version'])
        self.driver.wait()
        self.assertEqual(self.item().status, 'done')
        self.assertEqual(len(self.calls), 1)

    def test_deny_never_assumes_free(self):
        self.create(policy='deny')
        self.cycle()
        self.assertEqual(self.item().status, 'pending')
        self.assertFalse(self.calls)
        self.assertEqual(self.view()['status'], 'paused')
        self.assertFalse(self.request('GET', self.path+'/cost-state')['can_confirm'])

    def test_missing_token_never_sends_or_marks_sent(self):
        os.environ['STYLELORA_NAI_TOKEN'] = ''
        self.create()
        self.cycle()
        self.assertFalse(self.calls)
        self.assertEqual(self.item().status, 'pending')
        self.assertEqual(self.view()['runtime']['pause_reason'], 'nai:token_missing')

    def test_timeout_remains_sent_and_resume_never_reposts(self):
        self.create()
        def timeout(_):
            raise httpx.ReadTimeout('PRIVATE')
        self.handler = timeout
        self.cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertEqual(self.view()['runtime']['pause_reason'], 'nai:request_unknown')
        self.control('resume', status=409)
        self.assertEqual(len(self.calls), 1)
        item = self.item()
        recovery = self.request('GET', self.path+f'/recovery/{item.item_id}')
        self.assertEqual(recovery['reason'], 'nai:outcome_unknown')
        self.assertFalse(recovery['can_reconcile'])
        self.assertFalse(recovery['can_restore'])

    def test_http_rejection_is_not_fabricated_no_charge_proof(self):
        self.create()
        self.handler = lambda _: httpx.Response(429, text='PRIVATE')
        self.cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertEqual(self.view()['runtime']['pause_reason'], 'nai:rate_limited')
        self.control('resume', status=409)
        self.assertEqual(len(self.calls), 1)

    def test_malformed_zip_never_registers_or_resends(self):
        self.create()
        self.handler = lambda _: httpx.Response(201, content=zipped(b'broken'), headers={'content-type': 'application/zip'})
        self.cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertIsNone(self.item().image_id)
        self.control('resume', status=409)
        self.assertEqual(len(self.calls), 1)

    def fail_at(self, phase):
        def fault(name):
            if name == phase:
                raise OSError('simulated interruption')
        self.project._fault_hook = fault

    def test_crash_before_response_receipt_never_adopts_or_resubmits(self):
        for phase in ('nai_after_response', 'nai_after_save'):
            with self.subTest(phase=phase):
                self.create()
                self.fail_at(phase)
                self.cycle()
                self.project._fault_hook = None
                item = self.item()
                self.assertEqual(read_receipt(self.project, item.item_id, 1)['phase'], 'intent')
                recovery = self.request('GET', self.path+f'/recovery/{item.item_id}')
                self.assertFalse(recovery['can_reconcile'])
                before = len(self.calls)
                self.control('resume', status=409)
                self.assertEqual(len(self.calls), before)

    def test_receipt_survives_project_reopen_and_recovers_without_token_or_network(self):
        self.create()
        self.fail_at('nai_after_receipt')
        self.cycle()
        self.project._fault_hook = None
        iid = self.item().item_id
        self.runtime._close_current()
        view = self.runtime.open_now(self.root)
        self.pid = view['project_id']
        self.p = '/api/projects/'+self.pid
        self.path = self.p+f'/sources/{self.sid}'
        self.project, self.context = self.runtime.context.handle, self.runtime.context
        os.environ['STYLELORA_NAI_TOKEN'] = ''
        value = self.request('GET', self.path+f'/recovery/{iid}')
        self.assertTrue(value['can_reconcile'], value)
        accepted = self.request('POST', self.path+'/recovery', {'item_id': iid, 'action': 'reconcile'}, version=value['version'], status=202)
        self.wait_operation(accepted)
        self.assertEqual(self.item().status, 'done')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(read_receipt(self.project, iid, 1)['phase'], 'closed')

    def test_artifact_commit_failure_reconciles_same_bytes(self):
        self.create()
        self.fail_at('artifact_after_commit')
        self.cycle()
        self.project._fault_hook = None
        iid = self.item().item_id
        value = self.request('GET', self.path+f'/recovery/{iid}')
        self.assertTrue(value['can_reconcile'], value)
        accepted = self.request('POST', self.path+'/recovery', {'item_id': iid, 'action': 'reconcile'}, version=value['version'], status=202)
        self.wait_operation(accepted)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.view()['counts']['done'], 1)

    def test_tampered_image_cannot_be_registered(self):
        self.create()
        self.fail_at('nai_after_receipt')
        self.cycle()
        self.project._fault_hook = None
        item = self.item()
        path = self.project.root/f'images/.pending/{item.item_id}/1/out.png'
        body = json.loads(self.calls[0].content)
        body['input'] = 'different prompt'
        path.write_bytes(png_bytes(body))
        with self.assertRaises(ServiceError):
            self.driver._accept_local(self.context, item.item_id)
        self.assertEqual(self.item().status, 'sent')

    def test_receipt_binding_rejects_wrong_request_even_with_valid_envelope(self):
        self.create()
        self.fail_at('nai_after_receipt')
        self.cycle()
        self.project._fault_hook = None
        item = self.item()
        row = read_receipt(self.project, item.item_id, 1)
        row['seed'] += 1
        atomic_json(journal_path(self.project, item.item_id, 1), {'content': row, 'digest': digest(row)})
        value = self.request('GET', self.path+f'/recovery/{item.item_id}')
        self.assertEqual(value['reason'], 'nai:receipt_mismatch')
        self.assertFalse(value['can_reconcile'])

    def test_stop_during_post_settles_only_the_authorized_image(self):
        self.create(count=2)
        entered, release = threading.Event(), threading.Event()
        async def respond(request):
            self.calls.append(request)
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            return httpx.Response(201, content=zipped(png_bytes(json.loads(request.content))), headers={'content-type': 'application/zip'})
        self.service.backend = NaiDirectBackend(transport=httpx.MockTransport(respond))
        self.driver.dispatch(self.context, self.sid)
        try:
            self.assertTrue(entered.wait(3))
            view = self.view()
            self.request('POST', self.path+'/control', {'action': 'pause'}, version=view['version'])
            state = self.request('GET', '/api/nai-direct')
            self.request('DELETE', '/api/nai-direct/token', version=state['version'], status=409)
        finally:
            release.set()
            self.driver.wait()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.item(1).status, self.item(2).status), ('done', 'pending'))

    def test_get_account_is_local_and_explicit_probe_is_readonly(self):
        state = self.request('GET', '/api/nai-direct')
        self.assertTrue(state['available'])
        self.assertNotIn('fixture-token', json.dumps(state))
        self.assertFalse(self.calls)
        self.handler = lambda _: httpx.Response(200, json={'active': True, 'tier': 3,
            'trainingStepsLeft': {'fixedTrainingStepsLeft': 100, 'purchasedTrainingSteps': 30}})
        accepted = self.request('POST', '/api/nai-direct/account/probe', {}, status=202)
        self.wait_operation(accepted)
        self.assertEqual([r.method for r in self.calls], ['GET'])
        self.assertEqual(self.request('GET', '/api/nai-direct')['account']['subscription_anlas'], 100)

    def test_direct_registry_and_source_cannot_invoke_launcher_check(self):
        self.create(start=False)
        with patch.object(self.runtime.backends, 'read_inspection', side_effect=AssertionError('Launcher invoked')):
            value = self.request('POST', self.path+'/launcher-check', {}, version=self.source['version'], status=422)
        self.assertEqual(value['error']['code'], 'source:backend_mismatch')
        registry = self.request('GET', '/api/backends')['items']
        self.assertEqual([entry['backend_id'] for entry in registry], ['nai_launcher', 'nai_direct'])
        self.assertTrue(registry[1]['features']['generation'])
        self.assertEqual(self.request('GET', '/api/backends/nai_direct/capabilities')['backend'], 'nai_direct')
        self.assertFalse(self.calls)

    def test_token_api_uses_encrypted_store_cas_and_no_project_credentials(self):
        os.environ.pop('STYLELORA_NAI_TOKEN')
        state = self.request('GET', '/api/nai-direct')
        with patch('stylelora.llm.secrets.dpapi', side_effect=lambda data, **kw: data[::-1]):
            saved = self.request('PUT', '/api/nai-direct/token', {'value': 'private-fixture-secret'}, version=state['version'])
            self.assertTrue(saved['available'])
            self.assertNotIn('private-fixture-secret', json.dumps(saved))
            self.assertNotIn('private-fixture-secret', json.dumps(self.service.secrets.entries))
            self.request('PUT', '/api/nai-direct/token', {'value': 'different'}, version=state['version'], status=409)
            removed = self.request('DELETE', '/api/nai-direct/token', version=saved['version'])
            self.assertFalse(removed['available'])
        bad = self.request('PUT', '/api/nai-direct/token', {'value': 'PRIVATE\nINVALID'}, version=removed['version'], status=422)
        self.assertNotIn('PRIVATE', str(bad))

    def test_unknown_attempt_cannot_be_manually_retried_as_failed(self):
        self.create()
        self.handler = lambda _: httpx.Response(500)
        self.cycle()
        item = self.item()
        value = self.request('GET', self.p+f'/source-items/{item.item_id}')
        self.request('POST', self.p+f'/source-items/{item.item_id}/retry', {'expected_attempt': 1}, version=value['version'], status=409)
        self.assertEqual(len(self.calls), 1)

    def test_secret_replace_acknowledgement_failure_invalidates_version_and_account(self):
        from stylelora.backends.nai_direct.service import NaiSecrets
        os.environ.pop('STYLELORA_NAI_TOKEN')
        self.service.secrets = NaiSecrets(self.base/'uncertain-secret')
        state = self.request('GET', '/api/nai-direct')
        self.service.account = {'old_account': True}
        def write_then_fail(path, data):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            raise OSError('lost acknowledgement')
        with patch('stylelora.llm.secrets.dpapi', side_effect=lambda data, **kw: data[::-1]), \
                patch('stylelora.llm.secrets.private_write', side_effect=write_then_fail):
            result = self.request('PUT', '/api/nai-direct/token', {'value': 'uncertain-fixture-token'}, version=state['version'], status=503)
            self.assertEqual(result['error']['outcome'], 'unknown')
            self.assertEqual(result['error']['code'], 'nai:secret_write_unknown')
            current = self.request('GET', '/api/nai-direct')
            self.assertTrue(current['available'])
            self.assertNotEqual(current['version'], state['version'])
            self.assertIsNone(current['account'])
            self.assertNotIn('uncertain-fixture-token', json.dumps(current))

    def test_real_local_http_stream_runs_through_existing_registration(self):
        self.create()
        with local_endpoint() as (transport, calls):
            self.service.backend = NaiDirectBackend(transport=transport)
            self.cycle()
            self.assertEqual(self.item().status, 'done')
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], '/ai/generate-image')
            self.assertEqual(calls[0][1]['parameters']['n_samples'], 1)

    def test_connection_drop_after_server_received_body_cannot_be_retried(self):
        self.create()
        with local_endpoint(drop=True) as (transport, calls):
            self.service.backend = NaiDirectBackend(transport=transport)
            self.cycle()
            self.assertEqual(self.item().status, 'sent')
            self.control('resume', status=409)
            self.assertEqual(len(calls), 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows encrypted secret store')
    def test_real_dpapi_storage_persists_without_plaintext(self):
        from stylelora.backends.nai_direct.service import NaiSecrets
        os.environ.pop('STYLELORA_NAI_TOKEN')
        directory = self.base/'private-nai-secrets'
        store = NaiSecrets(directory)
        store.put('novelai', 'private-dpapi-fixture')
        self.assertNotIn(b'private-dpapi-fixture', store.path.read_bytes())
        self.assertEqual(NaiSecrets(directory).read('novelai'), 'private-dpapi-fixture')
        store.put('novelai', None)
        self.assertIsNone(NaiSecrets(directory).read('novelai'))
