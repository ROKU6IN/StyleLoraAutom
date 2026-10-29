import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.types import ItemSeed
from stylelora.project_db.validation import digest
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import PipeServer
from . import api_support, test_generation_handoff as handoff, test_generation_costs as costs
from .support import TEMP_ROOT

CLIENT = httpx.Client
PID, INSTANCE, TASK, PREVIEW, KEY = (str(uuid4()) for _ in range(5))
VERSION, CYCLE = 'a'*64, 'b'*32
P = '/api/projects/'+PID+'/sources'
CREDENTIALS = {'authority': '127.0.0.1:8301', 'token': 'private-test-token', 'instance_id': INSTANCE, 'protocol_version': 1}


def invoke(*args):
    output, progress = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(progress):
        code = main(['source', *map(str, args)])
    return code, json.loads(output.getvalue()), progress.getvalue()


def source(state='running', **extra):
    return {'source_id': 1, 'type': 'nai', 'status': state, 'locked': state != 'draft', 'cost_policy': 'ask',
            'version': VERSION, 'counts': dict.fromkeys(('pending', 'sent', 'done', 'failed', 'skipped'), 0), **extra}


def report():
    return {'source_id': 1, 'source_version': VERSION, 'can_start': True, 'blockers': [], 'cost_policy': 'ask',
            'preview_id': PREVIEW, 'digest': 'c'*64, 'source_digest': 'd'*64, 'seeds_digest': 'e'*64, 'count': 2}


def cost_state(**extra):
    return {'source_id': 1, 'source_version': VERSION, 'cycle_id': CYCLE, 'status': 'paused', 'cost_policy': 'ask',
            'acknowledged': False, 'confirmation_required': True, 'can_confirm': True, 'pending': {'estimated_anlas': 4}, **extra}


class SourceCLITransportTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def save(self, value, name='plan.json'):
        path = self.base/name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def plan(self):
        preview = report()
        body = {k: preview[k] for k in ('preview_id', 'digest', 'source_digest', 'seeds_digest', 'count')}
        plan = {'schema_version': 1, 'instance_id': INSTANCE, 'project_id': PID, 'source_id': 1,
                'idempotency_key': KEY, 'body': body, 'preview': preview}
        return {'plan': plan, 'confirmation_digest': digest(plan)}

    def run_args(self, document=None, *extra):
        document = self.plan() if document is None else document
        return ['run', '--preview', self.save(document), '--confirm-digest', document['confirmation_digest'], *extra]

    def run_cli(self, args, handler, *, sleep=None):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer '+CREDENTIALS['token'])
            if request.url.path == '/api/projects/current':
                return httpx.Response(200, json={'project_id': PID})
            return handler(request)
        def factory(**kw):
            self.assertFalse(kw['trust_env'])
            self.assertFalse(kw['follow_redirects'])
            return CLIENT(**kw, transport=httpx.MockTransport(handle))
        with patch('stylelora.runtime.attached_cli.pipe_request', return_value=CREDENTIALS), \
                patch('stylelora.runtime.attached_cli.httpx.Client', side_effect=factory), \
                patch('stylelora.runtime.source_cli.time.sleep', side_effect=sleep):
            result = invoke(*args)
        self.assertNotIn(CREDENTIALS['token'], json.dumps(result))
        return result

    def target(self, command, *extra):
        return [command, '1', '--project-id', PID, *extra]

    def test_preview_is_confirmable_and_does_not_dispatch(self):
        args = self.target('preview', '--task-id', TASK, '--expected-task-revision', 3)
        code, value, _ = self.run_cli(args, lambda r: httpx.Response(200, json=source('draft') if r.method == 'GET' else report()))
        self.assertEqual(code, 0)
        self.assertEqual(value['confirmation_digest'], digest(value['plan']))
        self.assertEqual(value['plan']['project_id'], PID)
        self.assertEqual(value['plan']['instance_id'], INSTANCE)
        self.assertEqual(self.requests[-1].url.path, P+'/1/handoff-preview')
        self.assertEqual(json.loads(self.requests[-1].content), {'task_id': TASK, 'expected_task_revision': 3})
        self.assertEqual(self.requests[-1].headers['If-Match'], '"'+VERSION+'"')

    def test_unavailable_preview_returns_blockers_and_no_run(self):
        document = self.plan()
        document['plan']['preview'].update(can_start=False, blockers=['generation:adapter_unavailable'])
        document['confirmation_digest'] = digest(document['plan'])
        code, value, _ = self.run_cli(self.run_args(document), lambda r: self.fail('No start allowed'))
        self.assertEqual((code, value['code']), (4, 'source:preview_blocked'))
        self.assertEqual(len(self.requests), 1)

    def test_detach_keeps_state_and_dispatch_evidence_without_polling(self):
        code, value, _ = self.run_cli(self.run_args(None, '--detach'), lambda r: httpx.Response(200, json=source(reconciled=False, dispatched=True)))
        self.assertEqual((code, value['status'], value['dispatched']), (0, 'running', True))
        self.assertEqual(value['cli']['source_id'], 1)
        self.assertEqual(value['cli']['request']['idempotency_key'], KEY)
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])

    def test_wait_stops_on_cost_pause_without_automatic_confirmation(self):
        def handle(r):
            if r.method == 'POST':
                return httpx.Response(200, json=source(reconciled=False, dispatched=True))
            return httpx.Response(200, json=cost_state() if r.url.path.endswith('cost-state') else source('paused'))
        code, value, _ = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['status'], value['cost_state']['confirmation_required']), (7, 'paused', True))
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    def test_wait_returns_partial_failure_after_running(self):
        polls = []
        def handle(r):
            if r.method == 'POST':
                return httpx.Response(200, json=source(reconciled=False, dispatched=True))
            polls.append(1)
            return httpx.Response(200, json=source() if len(polls) == 1 else source('done', counts={**source()['counts'], 'failed': 1}))
        code, value, _ = self.run_cli(self.run_args(), handle)
        self.assertEqual((code, value['status'], len(polls)), (6, 'done', 2))

    def test_lost_start_records_identity_and_never_automatically_retries(self):
        def timeout(r):
            raise httpx.ReadTimeout(CREDENTIALS['token'])
        code, value, _ = self.run_cli(self.run_args(), timeout)
        self.assertEqual((code, value['outcome']), (7, 'unknown'))
        self.assertEqual(value['cli']['request']['body'], self.plan()['plan']['body'])
        self.assertEqual(len(self.requests), 2)

    def test_bad_mutation_response_is_unknown_not_success(self):
        for response in (httpx.Response(302, headers={'location': 'http://example.invalid'}),
                         httpx.Response(202, json=source()), httpx.Response(200, json={}),
                         httpx.Response(200, json=source(source_id=2, reconciled=False, dispatched=True))):
            with self.subTest(response=response.status_code):
                code, value, _ = self.run_cli(self.run_args(None, '--detach'), lambda r: response)
                self.assertEqual((code, value['outcome']), (7, 'unknown'))

    def test_invalid_plan_rejected_before_attach(self):
        for change in (lambda p: p.update(schema_version=True), lambda p: p['body'].update(count=True),
                       lambda p: p['body'].update(token='secret'), lambda p: p.update(source_id=True),
                       lambda p: p['preview'].update(source_id=2), lambda p: p['body'].update(count=3)):
            document = self.plan()
            change(document['plan'])
            document['confirmation_digest'] = digest(document['plan'])
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke(*self.run_args(document))
            self.assertEqual((code, value['code']), (2, 'source:invalid_plan'))
            pipe.assert_not_called()

    def test_tampered_plan_or_wrong_confirmation_rejected(self):
        document = self.plan()
        document['plan']['preview']['cost_policy'] = 'allow'
        with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
            self.assertEqual(invoke(*self.run_args(document))[0], 2)
            self.assertEqual(invoke(*self.run_args(None, '--confirm-digest', 'f'*64))[0], 2)
        pipe.assert_not_called()

    def test_project_switch_and_instance_restart_prevent_submission(self):
        for field in ('project_id', 'instance_id'):
            document = self.plan()
            document['plan'][field] = str(uuid4())
            document['confirmation_digest'] = digest(document['plan'])
            code, value, _ = self.run_cli(self.run_args(document), lambda r: self.fail('No write allowed'))
            self.assertEqual(code, 4)
            self.assertEqual(len(self.requests), 1 if field == 'project_id' else 0)

    def test_control_replay_uses_original_version_not_fresh_version(self):
        args = self.target('pause', '--if-match', VERSION, '--instance-id', INSTANCE, '--idempotency-key', KEY)
        code, value, _ = self.run_cli(args, lambda r: httpx.Response(200, json=source(version='b'*64) if r.method == 'GET' else source('paused')))
        self.assertEqual(code, 0)
        self.assertEqual(self.requests[-1].headers['If-Match'], '"'+VERSION+'"')
        self.assertEqual(value['cli']['request']['idempotency_key'], KEY)

    def test_replay_and_cycle_arguments_must_be_explicit(self):
        for args in (self.target('pause', '--idempotency-key', KEY), self.target('confirm-cost', '--cycle-id', str(uuid4())),
                     self.target('set-cost-policy', '--policy', 'allow'), ['run', '--preview', 'x']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(['source', *map(str, args)])
            self.assertEqual(error.exception.code, 2)

    def test_confirm_uses_exact_cycle_and_does_not_substitute_state(self):
        code, value, _ = self.run_cli(self.target('confirm-cost', '--cycle-id', CYCLE, '--detach'),
            lambda r: httpx.Response(200, json=source('paused') if r.method == 'GET' else source()))
        self.assertEqual(code, 0)
        self.assertEqual(value['cli']['request']['body'], {'cycle_id': CYCLE})
        self.assertEqual(self.requests[-1].url.path, P+'/1/confirm-cost')

    def test_patch_has_receipt_cas_and_unknown_semantics(self):
        def handle(r):
            if r.method == 'GET':
                return httpx.Response(200, json=source())
            raise httpx.ReadTimeout('response lost')
        code, value, _ = self.run_cli(self.target('set-cost-policy', '--policy', 'deny'), handle)
        self.assertEqual((code, value['outcome']), (7, 'unknown'))
        self.assertEqual(value['cli']['request']['method'], 'PATCH')
        self.assertEqual(value['cli']['request']['if_match'], VERSION)
        self.assertEqual(value['cli']['request']['body'], {'cost_policy': 'deny', 'confirm_allow': False})

    def test_import_cost_control_is_rejected_without_writing(self):
        code, value, _ = self.run_cli(self.target('confirm-cost', '--cycle-id', CYCLE, '--detach'), lambda r: httpx.Response(200, json=source('paused', type='import')))
        self.assertEqual((code, value['code']), (2, 'source:not_generation'))
        self.assertTrue(all(r.method == 'GET' for r in self.requests))

    def test_pagination_stays_one_page(self):
        page = {'items': [], 'has_more': True, 'next_cursor': 'next'}
        code, value, _ = self.run_cli(self.target('items', '--limit', 2, '--cursor', 'previous'), lambda r: httpx.Response(200, json=page))
        self.assertEqual(code, 0)
        self.assertEqual(self.requests[-1].url.params['cursor'], 'previous')
        self.assertEqual(value['next_cursor'], 'next')
        self.assertEqual(len(self.requests), 2)

    def test_create_requires_explicit_allow_and_rejects_secret_fields(self):
        for body, extra in (({**handoff.draft(), 'cost_policy': 'allow'}, []), ({**handoff.draft(), 'token': 'secret'}, []),
                            (handoff.draft(), ['--confirm-allow'])):
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = invoke('create', '--type', 'nai', '--spec', self.save(body), '--project-id', PID, *extra)
            self.assertEqual((code, value['code']), (2, 'source:invalid_spec'))
            self.assertNotIn('secret', json.dumps(value))
            pipe.assert_not_called()

    def test_create_strict_files_reject_duplicate_nonfinite_and_oversize(self):
        path = self.base/'spec.json'
        for raw in ('{"name":"a","name":"b"}', '{"x":NaN}', '[]', '\ud800'):
            path.write_bytes(raw.encode('utf-8', errors='surrogatepass'))
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                self.assertEqual(invoke('create', '--type', 'nai', '--spec', path, '--project-id', PID)[0], 2)
            pipe.assert_not_called()
        path.write_text(' '*200, encoding='utf-8')
        with patch('stylelora.runtime.prompt_cli.FILE_LIMIT', 100):
            self.assertEqual(invoke('create', '--type', 'nai', '--spec', path, '--project-id', PID)[0], 2)

    def test_oversized_preview_is_not_emitted_as_a_usable_plan(self):
        with patch('stylelora.runtime.source_cli.FILE_LIMIT', 100):
            code, value, _ = self.run_cli(self.target('preview', '--task-id', TASK, '--expected-task-revision', 1),
                lambda r: httpx.Response(200, json=source('draft') if r.method == 'GET' else report()))
        self.assertEqual((code, value['code']), (2, 'source:plan_too_large'))
        self.assertNotIn('plan', value)

    def test_ctrl_c_retains_source_and_does_not_cancel(self):
        def handle(r):
            return httpx.Response(200, json=source(reconciled=False, dispatched=True))
        code, value, progress = self.run_cli(self.run_args(), handle, sleep=KeyboardInterrupt)
        self.assertEqual((code, value['cli']['source_id']), (130, 1))
        self.assertIn('continues', progress)
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    def test_reconcile_after_restart_uses_explicit_current_project_and_never_starts(self):
        document = self.plan()
        document['plan'].update(instance_id=str(uuid4()), project_id=str(uuid4()))
        document['confirmation_digest'] = digest(document['plan'])
        args = ['reconcile', '--preview', self.save(document), '--confirm-digest', document['confirmation_digest'], '--project-id', PID]
        code, value, _ = self.run_cli(args, lambda r: httpx.Response(200, json=source('paused', reconciled=True, dispatched=False)))
        self.assertEqual((code, value['reconciled'], value['dispatched']), (0, True, False))
        self.assertEqual(self.requests[-1].url.path, P+'/1/reconcile-start')
        self.assertEqual(set(json.loads(self.requests[-1].content)), {'source_digest', 'seeds_digest', 'count'})

    def test_no_instance_and_invalid_query_response_exit_codes(self):
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=OSError('secret')):
            code, value, _ = invoke('capabilities')
        self.assertEqual((code, value['code']), (3, 'instance:unavailable'))
        for body in ({}, source(counts={'done': True}), source(source_id=True)):
            self.assertEqual(self.run_cli(self.target('status'), lambda r: httpx.Response(200, json=body))[0], 5)


class SourceCLIAPITests(api_support.APITest):
    setup_driver = handoff.GenerationTests.setup_driver
    task = handoff.GenerationTests.task

    def setUp(self):
        super().setUp()
        self.setup_driver()

    def cli(self, *args, code=0):
        server = PipeServer.__new__(PipeServer)
        server.runtime = self.runtime
        with patch('stylelora.runtime.attached_cli.pipe_request', side_effect=server._dispatch), \
                patch('stylelora.runtime.attached_cli.httpx.Client', return_value=contextlib.nullcontext(self.client)):
            actual, value, progress = invoke(*args)
        self.assertEqual(actual, code, value)
        self.assertNotIn(self.runtime.token, json.dumps(value)+progress)
        return value

    def target(self, command, sid, *args, code=0):
        return self.cli(command, sid, '--project-id', self.pid, *args, code=code)

    def save(self, value, name='plan.json'):
        path = self.base/name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def create(self, body=None, *extra, code=0):
        return self.cli('create', '--type', 'nai', '--spec', self.save(body or handoff.draft(), 'draft.json'), '--project-id', self.pid, *extra, code=code)

    def preview(self, source=None, task=None, *, code=0):
        source, task = source or self.create(), task or self.task()
        return self.target('preview', source['source_id'], '--task-id', task['task_id'], '--expected-task-revision', task['revision'], code=code)

    def run_plan(self, document, *extra, code=0):
        return self.cli('run', '--preview', self.save(document), '--confirm-digest', document['confirmation_digest'], *extra, code=code)

    def test_create_preview_run_replay_listing_and_content_only_items(self):
        document = self.preview()
        self.assertEqual(self.driver.received, [])
        sid = document['plan']['source_id']
        self.assertEqual(self.target('items', sid)['items'], [])
        started = self.run_plan(document, '--detach')
        self.assertTrue(started['dispatched'])
        again = self.run_plan(document, '--detach')
        self.assertTrue(again['reconciled'])
        self.assertFalse(again['dispatched'])
        self.assertEqual(len(self.driver.received), 1)
        self.assertEqual(len(self.cli('list')['items']), 1)
        page = self.target('items', sid, '--limit', 1)
        self.assertTrue(page['has_more'])
        second = self.target('items', sid, '--limit', 1, '--cursor', page['next_cursor'])
        self.assertNotEqual(page['items'][0]['item_id'], second['items'][0]['item_id'])
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM source_items').fetchone()[0], 2)
            columns = [r[1] for r in db.execute('PRAGMA table_info(source_items)')]
            self.assertFalse({'task_id', 'dictionary_id'} & set(columns))

    def test_unavailable_driver_is_reported_and_cannot_start(self):
        self.runtime.generation_sources.driver = None
        self.assertFalse(self.cli('capabilities')['generation'])
        document = self.preview(code=6)
        self.assertIn('generation:adapter_unavailable', document['plan']['preview']['blockers'])
        self.run_plan(document, '--detach', code=4)
        self.assertEqual(self.target('items', document['plan']['source_id'])['items'], [])

    def test_create_unknown_replay_is_session_scoped_and_conflicting_body_rejected(self):
        original, lost = self.client.request, []
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path == self.p+'/sources' and not lost:
                lost.append(1)
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            unknown = self.create(code=7)
        args = ['--instance-id', unknown['cli']['instance_id'], '--idempotency-key', unknown['cli']['request']['idempotency_key']]
        created = self.create(None, *args)
        self.assertEqual(len(self.cli('list')['items']), 1)
        self.assertEqual(self.create({**handoff.draft(), 'name': 'different'}, *args, code=4)['code'], 'idempotency:key_conflict')
        self.assertEqual(created['status'], 'draft')

    def test_lost_start_reconciles_without_redispatch(self):
        document = self.preview()
        original = self.client.request
        def request(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path.endswith('/start'):
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=request):
            unknown = self.run_plan(document, '--detach', code=7)
        sid = unknown['cli']['source_id']
        self.assertEqual(len(self.driver.received), 1)
        recovered = self.cli('reconcile', '--preview', self.save(document), '--confirm-digest', document['confirmation_digest'], '--project-id', self.pid)
        self.assertTrue(recovered['reconciled'])
        self.target('pause', sid)
        again = self.run_plan(document, '--detach', code=7)
        self.assertEqual(again['status'], 'paused')
        self.assertEqual(len(self.driver.received), 1)

    def test_stale_source_or_task_revision_rejected_before_freezing(self):
        source, task = self.create(), self.task()
        self.target('preview', source['source_id'], '--task-id', task['task_id'], '--expected-task-revision', task['revision']+1, code=4)
        document = self.preview(source, task)
        self.request('PUT', self.p+f"/sources/{source['source_id']}", {**handoff.draft(), 'name': 'changed'}, version=source['version'])
        self.run_plan(document, '--detach', code=4)
        self.assertEqual(self.target('items', source['source_id'])['items'], [])
        self.assertEqual(self.driver.received, [])

    def test_controls_cas_replay_and_resume_do_not_inherit_old_intent(self):
        document = self.preview()
        current = self.run_plan(document, '--detach')
        sid = current['source_id']
        paused = self.target('pause', sid)
        self.target('resume', sid, '--if-match', current['version'], '--detach', code=4)
        resumed = self.target('resume', sid, '--detach')
        receipt = resumed['cli']['request']
        self.target('resume', sid, '--detach', '--if-match', receipt['if_match'], '--instance-id', resumed['cli']['instance_id'],
                    '--idempotency-key', receipt['idempotency_key'])
        self.assertEqual(len(self.driver.received), 2)
        self.assertNotEqual(paused['version'], resumed['version'])
        self.target('cancel', sid)
        self.target('status', sid, code=7)
        self.target('resume', sid, '--detach', code=4)

    def test_explicit_allow_and_patch_replay_preserve_policy(self):
        created = self.create({**handoff.draft(), 'cost_policy': 'allow'}, '--confirm-allow')
        sid = created['source_id']
        changed = self.target('set-cost-policy', sid, '--policy', 'deny')
        receipt = changed['cli']['request']
        replay = self.target('set-cost-policy', sid, '--policy', 'deny', '--instance-id', changed['cli']['instance_id'],
                             '--if-match', receipt['if_match'], '--idempotency-key', receipt['idempotency_key'])
        self.assertEqual((changed['version'], replay['version'], replay['cost_policy']), (changed['version'], changed['version'], 'deny'))
        self.target('set-cost-policy', sid, '--policy', 'ask', '--if-match', created['version'], code=4)

    def test_reconcile_unstarted_source_never_starts_it(self):
        document = self.preview()
        value = self.cli('reconcile', '--preview', self.save(document), '--confirm-digest', document['confirmation_digest'], '--project-id', self.pid, code=7)
        self.assertEqual((value['status'], value['reconciled'], value['dispatched']), ('draft', False, False))
        self.assertEqual(self.driver.received, [])

    def test_run_wait_returns_cost_pause_without_sending_an_item(self):
        document = self.preview()
        original = self.driver.dispatch
        def dispatch(context, sid):
            original(context, sid)
            with read_snapshot(context.handle) as db:
                item_id = db.execute('SELECT item_id FROM source_items WHERE source_id=? ORDER BY seq', (sid,)).fetchone()[0]
            self.assertIsNone(self.runtime.generation_costs.reserve_dispatch(context, item_id, estimated_anlas=4,
                expected_cycle_id=commands.cost_cycle_id(context.handle, sid), instance_id=digest('instance'), contract_digest=digest('contract')))
        with patch.object(self.driver, 'dispatch', side_effect=dispatch):
            value = self.run_plan(document, code=7)
        self.assertEqual((value['status'], value['cost_state']['pending']['estimated_anlas']), ('paused', 4))
        self.assertEqual(value['counts']['sent'], 0)
        self.assertFalse(value['cost_state']['acknowledged'])
        items = self.target('items', value['source_id'])['items']
        self.assertTrue(all(row['attempts'] == 0 for row in items))


class SourceCLICostTests(api_support.APITest):
    write = costs.CostTests.write
    reserve = costs.CostTests.reserve
    state = costs.CostTests.state
    item = costs.CostTests.item
    cli = SourceCLIAPITests.cli
    target = SourceCLIAPITests.target

    def setUp(self):
        super().setUp()
        self.context = self.runtime.context
        self.project = self.context.handle
        self.service = self.runtime.generation_costs
        self.runtime.generation_sources.driver = handoff.FakeDriver()
        self.source = self.request('POST', self.p+'/sources', handoff.draft(), status=201)
        self.sid = self.source['source_id']
        self.path = self.p+f'/sources/{self.sid}'
        self.write(commands.start_source, self.sid, [ItemSeed(1, 'forest'), ItemSeed(2, 'river')])
        with read_snapshot(self.project) as db:
            self.ids = [r[0] for r in db.execute('SELECT item_id FROM source_items WHERE source_id=? ORDER BY seq', (self.sid,))]

    def test_cost_pause_confirm_replay_and_new_cycle_requires_new_consent(self):
        self.assertIsNone(self.reserve(4))
        state = self.target('cost-state', self.sid)
        self.assertTrue(state['confirmation_required'])
        paused = self.target('status', self.sid, '--wait', code=7)
        self.assertEqual(paused['cost_state']['cycle_id'], state['cycle_id'])
        self.assertEqual(self.item().attempts, 0)
        resumed = self.target('confirm-cost', self.sid, '--cycle-id', state['cycle_id'], '--detach')
        receipt = resumed['cli']['request']
        self.target('confirm-cost', self.sid, '--cycle-id', state['cycle_id'], '--detach', '--instance-id', resumed['cli']['instance_id'],
                    '--if-match', receipt['if_match'], '--idempotency-key', receipt['idempotency_key'])
        self.assertEqual(len(self.runtime.generation_sources.driver.received), 1)
        self.assertTrue(self.state()['acknowledged'])
        self.target('pause', self.sid)
        self.target('resume', self.sid, '--detach')
        self.assertFalse(self.state()['acknowledged'])
        self.assertIsNone(self.reserve(9))
        self.assertNotEqual(self.state()['cycle_id'], state['cycle_id'])
        self.target('confirm-cost', self.sid, '--cycle-id', state['cycle_id'], '--detach', code=4)
        self.assertEqual(self.item().attempts, 0)

    def test_deny_policy_cannot_be_overridden_by_confirmation(self):
        self.target('set-cost-policy', self.sid, '--policy', 'deny')
        self.assertIsNone(self.reserve(4))
        state = self.target('cost-state', self.sid)
        self.assertFalse(state['confirmation_required'])
        value = self.target('confirm-cost', self.sid, '--cycle-id', state['cycle_id'], '--detach', code=4)
        self.assertEqual(value['code'], 'cost:denied')
        self.assertEqual(self.item().attempts, 0)
