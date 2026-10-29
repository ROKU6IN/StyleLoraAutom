import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.llm.secrets import SecretStore
from stylelora.llm.types import EndpointInput, ROLES
from stylelora.project_db.storage import atomic_json
from stylelora.runtime import doctor, doctor_network as network
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.paths import locations
from .support import TEMP_ROOT
from .test_doctor import snapshot


MODELS = {'data': [{'id': 'PRIVATE_MODEL_NAME'}]}
ACCOUNT = {'active': True, 'tier': 3,
           'trainingStepsLeft': {'fixedTrainingStepsLeft': 10000, 'purchasedTrainingSteps': 20000},
           'PRIVATE_ACCOUNT': 'PRIVATE_ACCOUNT_VALUE'}


@contextmanager
def local_models(*, disconnect=False):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization')))
            if disconnect:
                self.close_connection = True
                return
            data = json.dumps(MODELS).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class NetworkDoctorTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.paths = locations(self.base/'runtime')
        self.state = {'format': 1, 'revision': 0, 'endpoints': {}, 'roles': dict.fromkeys(ROLES)}
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith('STYLELORA_LLM_TOKEN_') and k != 'STYLELORA_NAI_TOKEN'}
        patched = patch.dict(os.environ, clean_env, clear=True)
        patched.start()
        self.addCleanup(patched.stop)
        for name in ('instance_status', 'dpapi_capability'):
            patched = patch.object(doctor, name, return_value=doctor.result())
            patched.start()
            self.addCleanup(patched.stop)

    def endpoint(self, *, protocol='openai_chat', refs=None, base_url='https://example.invalid/private/v1', token='PRIVATE_TOKEN'):
        eid = str(uuid4())
        refs = ['fixture'] if refs is None else refs
        self.state['endpoints'][eid] = {'endpoint_id': eid, 'revision': 1, 'token_refs': refs,
            'config': EndpointInput(base_url=base_url, protocol=protocol).model_dump()}
        self.paths.config.mkdir(parents=True, exist_ok=True)
        atomic_json(self.paths.config/'llm.json', self.state)
        if token is not None:
            os.environ[SecretStore.env_name('fixture')] = token
        return 'network.llm.'+eid

    def run_network(self, handler=None, **kwargs):
        if handler is None:
            handler = lambda request: httpx.Response(200, json=MODELS)
        return network.diagnose_network(self.paths, transport=httpx.MockTransport(handler), **kwargs)

    @staticmethod
    def row(rows, identifier):
        return next(row for row in rows if row['id'] == identifier)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['doctor', '--runtime-dir', str(self.paths.runtime), *args])
        return code, out.getvalue(), err.getvalue()

    def test_default_and_content_modes_never_decrypt_credentials_or_probe(self):
        self.endpoint()
        os.environ['STYLELORA_NAI_TOKEN'] = 'PRIVATE_NAI_TOKEN'
        with patch.object(SecretStore, 'read', side_effect=AssertionError('no credential read')), \
                patch.object(network, 'diagnose_network', side_effect=AssertionError('no network')):
            for options in ({}, {'verify_content': True}):
                report = doctor.diagnose(runtime_dir=self.paths.runtime, **options)
                self.assertEqual(report['exit_code'], 0)
                self.assertFalse(report['network_requested'])
                self.assertFalse(report['network_probed'])

    def test_unconfigured_probe_never_opens_socket_or_creates_directories(self):
        with patch('socket.socket', side_effect=AssertionError('not configured')):
            report = doctor.diagnose(runtime_dir=self.paths.runtime, probe_network=True)
        self.assertTrue(report['network_requested'])
        self.assertFalse(report['network_probed'])
        self.assertEqual(report['scope'], 'local_and_network')
        self.assertEqual(report['exit_code'], 0)
        self.assertFalse(self.paths.runtime.exists())

    def test_three_protocols_use_get_and_fixed_auth_headers_without_persisting_discovery(self):
        protocols = ('openai_chat', 'openai_responses', 'anthropic_messages')
        identifiers = [self.endpoint(protocol=p) for p in protocols]
        calls, before = [], snapshot(self.base)
        def handler(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.content, b'')
            self.assertEqual(str(request.url), 'https://example.invalid/private/v1/models')
            return httpx.Response(200, json={**MODELS, 'has_more': True, 'private': 'PRIVATE_REPLY'})
        with patch('stylelora.runtime.lifecycle.Runtime', side_effect=AssertionError('no service')), \
                patch('stylelora.llm.config.ConfigStore.save', side_effect=AssertionError('no discovery save')):
            rows = self.run_network(handler)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sum('x-api-key' in r.headers for r in calls), 1)
        self.assertEqual(sum(r.headers.get('authorization') == 'Bearer PRIVATE_TOKEN' for r in calls), 2)
        for identifier in identifiers:
            row = self.row(rows, identifier)
            self.assertEqual(row['code'], 'doctor:network_verified')
            self.assertEqual(row['details']['returned_model_count'], 1)
            self.assertTrue(row['details']['more_models'])
        raw = json.dumps(rows)
        for hidden in ('PRIVATE', 'example.invalid', 'private/v1', 'fixture'):
            self.assertNotIn(hidden, raw)
        self.assertEqual(before, snapshot(self.base))

    def test_first_available_token_is_used_once_without_rotation_on_rejection(self):
        identifier = self.endpoint(refs=['missing', 'fixture', 'backup'])
        os.environ[SecretStore.env_name('backup')] = 'PRIVATE_BACKUP'
        calls = []
        def handler(request):
            calls.append(request.headers['authorization'])
            return httpx.Response(401, text='PRIVATE_BODY')
        row = self.row(self.run_network(handler), identifier)
        self.assertEqual(calls, ['Bearer PRIVATE_TOKEN'])
        self.assertEqual(row['code'], 'doctor:network_unauthorized')
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_missing_credentials_need_attention_without_network(self):
        identifier = self.endpoint(token=None)
        rows = self.run_network(lambda request: self.fail('must not send'))
        row = self.row(rows, identifier)
        self.assertEqual(row['exit_code'], 7)
        self.assertFalse(row['details']['attempted'])

    def test_invalid_environment_token_masks_stored_token_without_silent_fallback(self):
        identifier = self.endpoint(token='\nPRIVATE_INVALID')
        self.paths.private.mkdir(parents=True)
        atomic_json(self.paths.private/'secrets.json', {'format': 1, 'entries': {'fixture': 'ZmFrZQ=='}})
        with patch('stylelora.llm.secrets.dpapi', side_effect=AssertionError('environment wins')):
            row = self.row(self.run_network(lambda request: self.fail('must not send')), identifier)
        self.assertEqual(row['code'], 'doctor:credentials_missing')

    def test_bad_credential_store_is_reported_without_raw_error_or_token(self):
        identifier = self.endpoint(token=None)
        self.paths.private.mkdir(parents=True)
        atomic_json(self.paths.private/'secrets.json', {'format': 1, 'entries': {'fixture': 'ZmFrZQ=='}})
        with patch('stylelora.llm.secrets.dpapi', side_effect=ValueError('PRIVATE_DPAPI')):
            row = self.row(self.run_network(lambda request: self.fail('must not send')), identifier)
        self.assertEqual(row['code'], 'doctor:credentials_unavailable')
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_endpoint_limit_does_not_silently_probe_a_subset(self):
        for _ in range(3):
            self.endpoint()
        with patch.object(network, 'MAX_ENDPOINTS', 2), patch.object(SecretStore, 'read', side_effect=AssertionError('limit first')):
            rows = self.run_network(lambda request: self.fail('must not send'))
        row = self.row(rows, 'network.llm')
        self.assertEqual(row['code'], 'doctor:too_many_endpoints')
        self.assertEqual(row['details']['configured_count'], 3)

    def test_malformed_llm_config_does_not_block_independent_nai_check(self):
        self.endpoint()
        (self.paths.config/'llm.json').write_bytes(b'{PRIVATE_INVALID')
        os.environ['STYLELORA_NAI_TOKEN'] = 'PRIVATE_NAI_TOKEN'
        calls = []
        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(200, json=ACCOUNT)
        rows = self.run_network(handler)
        self.assertEqual(self.row(rows, 'network.llm')['code'], 'doctor:network_config_invalid')
        self.assertEqual(self.row(rows, 'network.nai_direct')['code'], 'doctor:network_verified')
        self.assertEqual(calls, ['api.novelai.net'])

    def test_http_errors_never_retry_or_follow_redirects_or_echo_response(self):
        identifier = self.endpoint()
        for status, code, exit_code in ((401, 'unauthorized', 5), (403, 'unauthorized', 5),
                (429, 'rate_limited', 5), (307, 'redirect_rejected', 5), (404, 'probe_unsupported', 6),
                (405, 'probe_unsupported', 6), (501, 'probe_unsupported', 6), (500, 'http_error', 5)):
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, text='PRIVATE_REPLY', headers={'Location': 'https://private.invalid', 'Retry-After': '999'})
                row = self.row(self.run_network(handler), identifier)
                self.assertEqual(len(calls), 1)
                self.assertEqual(row['code'], 'doctor:network_'+code)
                self.assertEqual(row['exit_code'], exit_code)
                self.assertNotIn('PRIVATE_REPLY', json.dumps(row))

    def test_malformed_json_models_and_duplicate_keys_are_rejected(self):
        identifier = self.endpoint()
        payloads = [b'{', b'[]', b'{"data":[],"data":[]}', b'{}',
                    json.dumps({'data': [{}]}).encode(), json.dumps({'data': [{'id': 123}]}).encode(),
                    json.dumps({'data': [{'id': 'a'}, {'id': 'a'}]}).encode(),
                    json.dumps({'data': [], 'has_more': 'PRIVATE'}).encode()]
        for payload in payloads:
            with self.subTest(payload=payload):
                row = self.row(self.run_network(lambda request: httpx.Response(200, content=payload,
                               headers={'Content-Type': 'application/json'})), identifier)
                self.assertEqual(row['code'], 'doctor:network_invalid_reply')

    def test_wrong_mime_compression_and_declared_size_are_rejected(self):
        identifier = self.endpoint()
        for extra, code in (({'content-type': 'text/html'}, 'invalid_reply'),
                            ({'content-encoding': 'unsupported'}, 'invalid_reply'),
                            ({'content-length': str(network.MAX_REPLY+1)}, 'reply_too_large'),
                            ({'content-length': '-1'}, 'reply_too_large')):
            row = self.row(self.run_network(lambda request: httpx.Response(200, content=b'{}',
                           headers={'content-type': 'application/json', **extra})), identifier)
            self.assertEqual(row['code'], 'doctor:network_'+code)

    def test_chunked_size_limit_closes_stream(self):
        identifier = self.endpoint()
        class Stream(httpx.AsyncByteStream):
            closed = False
            chunks = 0
            async def __aiter__(self):
                for _ in range(3):
                    self.chunks += 1
                    yield b'x'*65536
            async def aclose(self):
                self.closed = True
        stream = Stream()
        with patch.object(network, 'MAX_REPLY', 65536):
            row = self.row(self.run_network(lambda request: httpx.Response(200, stream=stream,
                           headers={'content-type': 'application/json'})), identifier)
        self.assertEqual(row['code'], 'doctor:network_reply_too_large')
        self.assertTrue(stream.closed)
        self.assertEqual(stream.chunks, 2)

    def test_total_deadline_cancels_inflight_and_skips_remaining_endpoints(self):
        self.endpoint()
        self.endpoint()
        calls, canceled = [], []
        async def handler(request):
            calls.append(request)
            try:
                await asyncio.sleep(10)
            finally:
                canceled.append(True)
        rows = self.run_network(handler, timeout=.03)
        codes = [r['code'] for r in rows]
        self.assertIn('doctor:network_timeout', codes)
        self.assertIn('doctor:network_budget_exhausted', codes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(canceled, [True])

    def test_slow_body_cannot_extend_total_deadline(self):
        identifier = self.endpoint()
        class Stream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(.01)
                    yield b' '
            async def aclose(self):
                self.closed = True
        stream = Stream()
        row = self.row(self.run_network(lambda request: httpx.Response(200, stream=stream,
                       headers={'content-type': 'application/json'}), timeout=.035), identifier)
        self.assertEqual(row['code'], 'doctor:network_timeout')
        self.assertTrue(stream.closed)

    def test_transport_errors_and_unapproved_error_codes_are_sanitized(self):
        identifier = self.endpoint()
        for error in (httpx.ConnectError('PRIVATE_URL_TOKEN'), ServiceError(503, 'PRIVATE_ERROR_CODE')):
            def handler(request):
                raise error
            row = self.row(self.run_network(handler), identifier)
            self.assertEqual(row['code'], 'doctor:network_failed')
            self.assertNotIn('PRIVATE', json.dumps(row))

    def test_nai_probe_uses_fixed_get_and_discards_account_content(self):
        os.environ['STYLELORA_NAI_TOKEN'] = 'PRIVATE_NAI_TOKEN'
        before, calls = snapshot(self.base), []
        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), 'https://api.novelai.net/user/subscription')
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.headers['authorization'], 'Bearer PRIVATE_NAI_TOKEN')
            return httpx.Response(200, json=ACCOUNT)
        with patch('stylelora.backends.nai_direct.client.NaiDirectBackend.generate', side_effect=AssertionError('no generation')):
            rows = self.run_network(handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.row(rows, 'network.nai_direct')['code'], 'doctor:network_verified')
        for private in ('PRIVATE', '10000', '20000', 'tier', 'active'):
            self.assertNotIn(private, json.dumps(rows))
        self.assertEqual(before, snapshot(self.base))

    def test_nai_invalid_response_and_auth_failures_are_not_success(self):
        os.environ['STYLELORA_NAI_TOKEN'] = 'PRIVATE_NAI_TOKEN'
        for status, body in ((200, {}), (401, {}), (302, {})):
            row = self.row(self.run_network(lambda request: httpx.Response(status, json=body)), 'network.nai_direct')
            self.assertEqual(row['exit_code'], 5, row)
            self.assertFalse(row['details']['reply_valid'])

    @unittest.skipUnless(os.name == 'nt', 'Windows CurrentUser DPAPI')
    def test_native_stored_credentials_are_read_for_get_only_and_never_rewritten(self):
        from stylelora.backends.nai_direct.service import NaiSecrets
        identifier = self.endpoint(token=None)
        SecretStore(self.paths.private).put('fixture', 'LOCAL_FIXTURE_LLM_TOKEN')
        NaiSecrets(self.paths.private/'nai-direct').put('novelai', 'LOCAL_FIXTURE_NAI_TOKEN')
        before, calls = snapshot(self.base), []
        def handler(request):
            calls.append(request.method)
            nai = request.url.host == 'api.novelai.net'
            self.assertEqual(request.headers['authorization'], 'Bearer LOCAL_FIXTURE_'+('NAI' if nai else 'LLM')+'_TOKEN')
            return httpx.Response(200, json=ACCOUNT if nai else MODELS)
        rows = self.run_network(handler)
        self.assertEqual(calls, ['GET', 'GET'])
        self.assertEqual(self.row(rows, identifier)['code'], 'doctor:network_verified')
        self.assertEqual(self.row(rows, 'network.nai_direct')['code'], 'doctor:network_verified')
        self.assertNotIn('LOCAL_FIXTURE', json.dumps(rows))
        self.assertEqual(before, snapshot(self.base))

    def test_secret_and_config_files_are_unpinned_before_network_wait(self):
        self.endpoint()
        self.paths.private.mkdir(parents=True)
        atomic_json(self.paths.private/'secrets.json', {'format': 1, 'entries': {}})
        before = snapshot(self.base)
        def handler(request):
            # Windows sharing checks ensure the diagnostic does not hold writers
            # out during a slow remote request; opening does not modify contents.
            with (self.paths.config/'llm.json').open('r+b'), (self.paths.private/'secrets.json').open('r+b'):
                pass
            return httpx.Response(200, json=MODELS)
        rows = self.run_network(handler)
        self.assertTrue(any(r['code'] == 'doctor:network_verified' for r in rows))
        self.assertEqual(before, snapshot(self.base))

    def test_snapshot_preserves_credentials_and_target_during_config_change(self):
        self.endpoint(base_url='https://one.invalid/v1')
        self.endpoint(base_url='https://two.invalid/v1')
        tokens, hosts = [], []
        def handler(request):
            tokens.append(request.headers['authorization'])
            hosts.append(request.url.host)
            os.environ[SecretStore.env_name('fixture')] = 'PRIVATE_REPLACEMENT'
            (self.paths.config/'llm.json').write_bytes(b'{}')
            return httpx.Response(200, json=MODELS)
        rows = self.run_network(handler)
        self.assertEqual(tokens, ['Bearer PRIVATE_TOKEN']*2)
        self.assertEqual(set(hosts), {'one.invalid', 'two.invalid'})
        self.assertEqual(sum(r['code'] == 'doctor:network_verified' for r in rows), 2)

    def test_runner_failure_preserves_local_results_without_raw_exception(self):
        with patch.object(network, 'diagnose_network', side_effect=ValueError('PRIVATE_URL_TOKEN')):
            code, output, errors = self.invoke('--probe-network')
        report = json.loads(output)
        self.assertEqual(code, 5)
        self.assertEqual(self.row(report['checks'], 'network')['details']['request_state'], 'unknown')
        self.assertEqual(self.row(report['checks'], 'sqlite')['exit_code'], 0)
        self.assertNotIn('PRIVATE', output)
        self.assertEqual(errors, '')

    def test_network_failure_aggregates_with_local_checks_and_preserves_partial_exit(self):
        with local_models() as (url, _):
            self.endpoint(base_url=url)
            original = network.diagnose_network
            transport = httpx.MockTransport(lambda request: httpx.Response(404))
            with patch.object(network, 'diagnose_network', side_effect=lambda paths, **kwargs:
                       original(paths, transport=transport, **kwargs)):
                code, output, _ = self.invoke('--probe-network')
        report = json.loads(output)
        self.assertEqual(code, 6)
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(report['network_probed'])

    def test_cli_network_timeout_validation_precedes_checks(self):
        for args in (['--network-timeout', '10'], ['--probe-network', '--network-timeout', '4'],
                     ['--probe-network', '--network-timeout', '121'], ['--probe-network', '--network-timeout', '5.5']):
            with redirect_stderr(io.StringIO()), patch.object(doctor, 'diagnose') as diagnosed, self.assertRaises(SystemExit) as error:
                main(['doctor', *args])
            self.assertEqual(error.exception.code, 2)
            diagnosed.assert_not_called()

    def test_cli_interrupt_produces_no_success_json(self):
        with patch.object(network, 'diagnose_network', side_effect=KeyboardInterrupt()):
            code, output, errors = self.invoke('--probe-network')
        self.assertEqual(code, 130)
        self.assertEqual(output, '')
        self.assertIn('interrupted', errors)

    def test_real_local_get_ignores_proxy_and_preserves_config(self):
        with local_models() as (url, calls):
            identifier = self.endpoint(base_url=url)
            before = snapshot(self.base)
            with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
                                        'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                code, output, errors = self.invoke('--probe-network', '--network-timeout', '5', '--verify-content', '--json')
            self.assertEqual(calls, [('/v1/models', 'Bearer PRIVATE_TOKEN')])
        report = json.loads(output)
        self.assertEqual(code, 0, report)
        self.assertEqual(self.row(report['checks'], identifier)['code'], 'doctor:network_verified')
        self.assertTrue(report['network_probed'])
        self.assertTrue(report['content_verification_requested'])
        self.assertEqual(errors, '')
        self.assertNotIn('PRIVATE', output)
        self.assertEqual(before, snapshot(self.base))

    def test_real_disconnect_after_get_is_not_retried(self):
        with local_models(disconnect=True) as (url, calls):
            identifier = self.endpoint(base_url=url)
            code, output, _ = self.invoke('--probe-network')
            self.assertEqual(len(calls), 1)
        self.assertEqual(code, 5)
        self.assertEqual(self.row(json.loads(output)['checks'], identifier)['code'], 'doctor:network_failed')
