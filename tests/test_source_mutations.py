"""Source draft edits and explicit item retries through CLI and HTTP."""
import contextlib
import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.cli import main
from stylelora.exchange.errors import ExchangeError
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import sources
from stylelora.project_db.types import ItemSeed
from . import api_support, test_source_cli as cli_support, test_generation_handoff as handoff


class SourceMutationTransportTests(unittest.TestCase):
    setUp = cli_support.SourceCLITransportTests.setUp
    save = cli_support.SourceCLITransportTests.save
    run_cli = cli_support.SourceCLITransportTests.run_cli
    target = cli_support.SourceCLITransportTests.target

    def item(self, **changes):
        return {'item_id': 1, 'source_id': 3, 'seq': 1, 'status': 'failed', 'attempts': 1,
                'image_id': None, 'error': 'fixture:failed', 'version': cli_support.VERSION, **changes}

    def test_update_requires_original_version_before_connecting(self):
        with patch('stylelora.runtime.attached_cli.pipe_request') as pipe, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['source', 'update', '1', '--spec', 'draft.json', '--project-id', cli_support.PID])
        pipe.assert_not_called()

    def test_update_validates_complete_draft_and_allow_consent_before_attach(self):
        for body, extra in (({'name': 'incomplete'}, []), ({**handoff.draft(), 'unknown': True}, []),
                            ({**handoff.draft(), 'cost_policy': 'allow'}, []), (handoff.draft(), ['--confirm-allow'])):
            with patch('stylelora.runtime.attached_cli.pipe_request') as pipe:
                code, value, _ = cli_support.invoke(*self.target('update', '--spec', self.save(body), '--if-match', cli_support.VERSION, *extra))
            self.assertEqual((code, value['code']), (2, 'source:invalid_spec'))
            pipe.assert_not_called()

    def test_update_sends_only_put_with_original_version_and_records_receipt(self):
        body = handoff.draft()
        code, value, _ = self.run_cli(self.target('update', '--spec', self.save(body), '--if-match', cli_support.VERSION),
                                    lambda r: httpx.Response(200, json=cli_support.source('draft')))
        self.assertEqual(code, 0)
        self.assertEqual([r.method for r in self.requests], ['GET', 'PUT'])
        self.assertEqual(value['cli']['request']['body'], body)
        self.assertEqual(value['cli']['request']['if_match'], cli_support.VERSION)

    def test_update_lost_response_is_unknown_without_retry(self):
        def timeout(request):
            raise httpx.ReadTimeout('secret internal detail')
        code, value, _ = self.run_cli(self.target('update', '--spec', self.save(handoff.draft()), '--if-match', cli_support.VERSION), timeout)
        self.assertEqual((code, value['outcome']), (7, 'unknown'))
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('secret internal', json.dumps(value))

    def test_update_and_retry_replay_require_instance_and_original_version(self):
        for args in (self.target('update', '--spec', 'draft.json', '--if-match', cli_support.VERSION, '--idempotency-key', cli_support.KEY),
                     self.target('retry-item', '--expected-attempt', 1, '--instance-id', cli_support.INSTANCE, '--idempotency-key', cli_support.KEY)):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(['source', *map(str, args)])
            self.assertEqual(error.exception.code, 2)

    def test_item_query_is_read_only_and_reports_failed_state_successfully(self):
        code, value, _ = self.run_cli(self.target('item'), lambda r: httpx.Response(200, json=self.item()))
        self.assertEqual((code, value['status'], value['cli']['source_id']), (0, 'failed', 3))
        self.assertTrue(all(r.method == 'GET' for r in self.requests))
        self.assertTrue(self.requests[-1].url.path.endswith('/source-items/1'))

    def test_retry_binds_item_attempt_and_version_without_resume(self):
        code, value, _ = self.run_cli(self.target('retry-item', '--expected-attempt', 1),
            lambda r: httpx.Response(200, json=self.item() if r.method == 'GET' else self.item(status='pending', attempts=2, error=None)))
        self.assertEqual((code, value['attempts']), (0, 2))
        self.assertEqual(value['cli']['request']['body'], {'expected_attempt': 1})
        self.assertEqual(value['cli']['request']['if_match'], cli_support.VERSION)
        self.assertEqual([r.url.path for r in self.requests if r.method != 'GET'], ['/api/projects/'+cli_support.PID+'/source-items/1/retry'])

    def test_retry_uses_explicit_version_for_replay(self):
        code, value, _ = self.run_cli(self.target('retry-item', '--expected-attempt', 1, '--if-match', 'b'*64,
            '--instance-id', cli_support.INSTANCE, '--idempotency-key', cli_support.KEY),
            lambda r: httpx.Response(200, json=self.item() if r.method == 'GET' else self.item(status='pending', attempts=2, error=None)))
        self.assertEqual(code, 0)
        self.assertEqual(value['cli']['request']['if_match'], 'b'*64)

    def test_retry_bad_read_is_rejected_without_mutation(self):
        for changed in ({'item_id': True}, {'item_id': 2}, {'attempts': True}, {'source_id': 0},
                        {'seq': 0}, {'status': 'unknown'}, {'image_id': True}, {'error': []}, {'version': 'bad version'}):
            code, _, _ = self.run_cli(self.target('retry-item', '--expected-attempt', 1), lambda r: httpx.Response(200, json=self.item(**changed)))
            self.assertEqual(code, 5)
            self.assertTrue(all(r.method == 'GET' for r in self.requests))

    def test_retry_invalid_write_response_is_unknown(self):
        for changed in ({'source_id': 2}, {'attempts': 3}, {'status': 'sent'}, {'image_id': 1}, {'error': 'failed'}):
            result = self.item(status='pending', attempts=2, error=None)
            result.update(changed)
            code, value, _ = self.run_cli(self.target('retry-item', '--expected-attempt', 1),
                lambda r: httpx.Response(200, json=self.item() if r.method == 'GET' else result))
            self.assertEqual((code, value['outcome']), (7, 'unknown'))
            self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    def test_retry_network_failure_retains_exact_request(self):
        def handler(request):
            if request.method == 'GET':
                return httpx.Response(200, json=self.item())
            raise httpx.ReadTimeout('lost')
        code, value, _ = self.run_cli(self.target('retry-item', '--expected-attempt', 1), handler)
        self.assertEqual((code, value['outcome']), (7, 'unknown'))
        self.assertEqual(value['cli']['item_id'], 1)
        self.assertEqual(value['cli']['request']['body'], {'expected_attempt': 1})

    def test_invalid_attempt_and_project_binding_prevent_retry(self):
        for attempt in ('0', '-1', '1.5', 'true'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['source', *map(str, self.target('retry-item', '--expected-attempt', attempt))])
        code, _, _ = self.run_cli(['retry-item', '1', '--project-id', str(uuid4()), '--expected-attempt', '1'],
                                   lambda r: self.fail('No item read or mutation after project switch'))
        self.assertEqual(code, 4)


class SourceMutationAPITests(api_support.APITest):
    cli = cli_support.SourceCLIAPITests.cli
    target = cli_support.SourceCLIAPITests.target
    save = cli_support.SourceCLIAPITests.save
    create = cli_support.SourceCLIAPITests.create

    def write(self, fn, *args, **kwargs):
        return submit_write(self.runtime.context.handle, lambda db: fn(db, *args, **kwargs)).result()

    def prepared_item(self, *, state='failed', source_state='done'):
        source = self.create(handoff.draft(1))
        sid = source['source_id']
        self.write(commands.start_source, sid, [ItemSeed(1, 'forest')])
        item_id = self.target('items', sid)['items'][0]['item_id']
        if state != 'pending':
            self.write(commands.mark_sent, item_id)
        if state == 'failed':
            self.write(commands.fail_item, item_id, attempt=1, error='fixture:failed')
        if source_state != 'running':
            self.write(commands.transition_source, sid, expected='running', target=source_state, reason='fixture')
        return self.target('item', item_id)

    def update(self, source, body, *extra, code=0):
        return self.target('update', source['source_id'], '--spec', self.save(body), '--if-match', source['version'], *extra, code=code)

    def retry(self, item, *extra, code=0):
        return self.target('retry-item', item['item_id'], '--expected-attempt', item['attempts'], '--if-match', item['version'], *extra, code=code)

    def test_complete_draft_edit_persists_and_does_not_seed_or_start(self):
        source = self.create()
        body = {**handoff.draft(3), 'name': '新草稿', 'style_prompt': 'new style', 'negative_prompt': '', 'cost_policy': 'deny'}
        body['config']['profile']['steps'] = 24
        result = self.update(source, body)
        stored = self.target('status', source['source_id'], code=7)
        self.assertEqual(result['version'], stored['version'])
        self.assertNotEqual(result['version'], source['version'])
        for key, value in body.items():
            self.assertEqual(stored['config_json' if key == 'config' else key], value)
        self.assertEqual((stored['status'], stored['locked']), ('draft', False))
        self.assertEqual(self.target('items', source['source_id'])['items'], [])

    def test_stale_draft_and_noop_updates(self):
        source = self.create()
        self.assertEqual(self.update(source, handoff.draft())['version'], source['version'])
        changed = self.update(source, {**handoff.draft(), 'name': 'changed'})
        self.assertEqual(self.update(source, handoff.draft(), code=4)['code'], 'concurrency:version_conflict')
        self.assertEqual(self.target('status', source['source_id'], code=7)['name'], changed['name'])

    def test_draft_put_replay_binds_body_and_precondition(self):
        source = self.create()
        body = {**handoff.draft(), 'name': 'edited'}
        changed = self.update(source, body)
        args = ['--instance-id', changed['cli']['instance_id'], '--idempotency-key', changed['cli']['request']['idempotency_key']]
        self.assertEqual(self.update(source, body, *args)['version'], changed['version'])
        self.assertEqual(self.update(source, handoff.draft(), *args, code=4)['code'], 'idempotency:key_conflict')
        self.assertEqual(self.update(changed, body, *args, code=4)['code'], 'idempotency:key_conflict')

    def test_unknown_draft_response_replays_once(self):
        source = self.create()
        body = {**handoff.draft(), 'name': 'response lost'}
        original = self.client.request
        def lost(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'PUT':
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=lost):
            unknown = self.update(source, body, code=7)
        replay = self.update(source, body, '--instance-id', unknown['cli']['instance_id'],
                             '--idempotency-key', unknown['cli']['request']['idempotency_key'])
        self.assertEqual(replay['name'], body['name'])
        self.assertEqual(len(self.cli('list')['items']), 1)

    def test_draft_allow_requires_explicit_confirmation(self):
        source = self.create()
        body = {**handoff.draft(), 'cost_policy': 'allow'}
        self.update(source, body, code=2)
        self.assertEqual(self.update(source, body, '--confirm-allow')['cost_policy'], 'allow')

    def test_locked_and_import_sources_cannot_use_draft_update(self):
        item = self.prepared_item()
        source = self.target('status', item['source_id'], code=6)
        self.assertEqual(self.update(source, handoff.draft(), code=4)['code'], 'generation:source_locked')
        imported = self.import_inputs()
        source = self.target('status', imported['source_id'])
        self.assertEqual(self.update(source, handoff.draft(), code=2)['code'], 'source:not_generation')

    def test_retry_finished_source_pauses_clears_error_and_keeps_frozen_content(self):
        item = self.prepared_item()
        before = self.target('status', item['source_id'], code=6)
        cycle = commands.cost_cycle_id(self.runtime.context.handle, item['source_id'])
        with patch.object(self.runtime.generation_sources, 'control', side_effect=AssertionError('must not resume')):
            result = self.retry(item)
        source = self.target('status', item['source_id'], code=7)
        self.assertEqual((result['status'], result['attempts'], result['error'], result['image_id']), ('pending', 2, None, None))
        self.assertEqual((result['prompt'], result['seq'], result['source_id']), (item['prompt'], item['seq'], item['source_id']))
        self.assertEqual((source['status'], source['finished_at'], source['locked']), ('paused', None, True))
        self.assertEqual(source['counts']['pending'], 1)
        self.assertEqual(source['counts']['failed'], 0)
        self.assertEqual(source['config_json'], before['config_json'])
        self.assertNotEqual(cycle, commands.cost_cycle_id(self.runtime.context.handle, item['source_id']))
        self.assertNotEqual(source['version'], before['version'])

    def test_retry_replay_and_current_version_reconciliation_do_not_increment_again(self):
        item = self.prepared_item()
        result = self.retry(item)
        args = ['--instance-id', result['cli']['instance_id'], '--idempotency-key', result['cli']['request']['idempotency_key']]
        self.assertEqual(self.retry(item, *args)['version'], result['version'])
        self.assertEqual(self.retry(item, code=4)['code'], 'concurrency:version_conflict')
        current = self.target('item', item['item_id'])
        again = self.retry({**current, 'attempts': item['attempts']})
        self.assertEqual((again['attempts'], again['version']), (2, current['version']))

    def test_retry_key_cannot_change_attempt_or_precondition(self):
        item = self.prepared_item()
        result = self.retry(item)
        args = ['--instance-id', result['cli']['instance_id'], '--idempotency-key', result['cli']['request']['idempotency_key']]
        self.assertEqual(self.retry({**item, 'attempts': 2}, *args, code=4)['code'], 'idempotency:key_conflict')
        self.assertEqual(self.retry({**item, 'version': result['version']}, *args, code=4)['code'], 'idempotency:key_conflict')

    def test_retry_response_lost_can_be_queried_and_replayed(self):
        item = self.prepared_item()
        original = self.client.request
        def lost(method, path, **kw):
            response = original(method, path, **kw)
            if method == 'POST' and path.endswith('/retry'):
                raise httpx.ReadTimeout('lost')
            return response
        with patch.object(self.client, 'request', side_effect=lost):
            unknown = self.retry(item, code=7)
        self.assertEqual(self.target('item', item['item_id'])['attempts'], 2)
        replay = self.retry(item, '--instance-id', unknown['cli']['instance_id'],
                            '--idempotency-key', unknown['cli']['request']['idempotency_key'])
        self.assertEqual(replay['attempts'], 2)

    def test_retry_commit_unknown_is_reconciled_without_an_extra_attempt(self):
        item = self.prepared_item()
        def fault(point):
            if point == 'after_commit':
                raise OSError('lost commit receipt')
        self.runtime.context.handle._fault_hook = fault
        try:
            value = self.retry(item, code=7)
        finally:
            self.runtime.context.handle._fault_hook = None
        self.assertEqual(value['outcome'], 'unknown')
        current = self.target('item', item['item_id'])
        self.assertEqual(current['attempts'], 2)
        self.assertEqual(self.retry({**current, 'attempts': 1})['attempts'], 2)

    def test_retry_before_commit_failure_rolls_back_item_and_source(self):
        item = self.prepared_item()
        def fault(point):
            if point == 'before_commit':
                raise OSError('rollback')
        self.runtime.context.handle._fault_hook = fault
        try:
            self.retry(item, code=5)
        finally:
            self.runtime.context.handle._fault_hook = None
        self.assertEqual(self.target('item', item['item_id'])['attempts'], 1)
        self.assertEqual(self.target('status', item['source_id'], code=6)['status'], 'done')

    def test_draft_post_commit_failure_reports_unknown_and_keeps_edit(self):
        source = self.create()
        def fault(point):
            if point == 'after_commit':
                raise OSError('lost')
        self.runtime.context.handle._fault_hook = fault
        try:
            self.assertEqual(self.update(source, {**handoff.draft(), 'name': 'committed'}, code=7)['outcome'], 'unknown')
        finally:
            self.runtime.context.handle._fault_hook = None
        self.assertEqual(self.target('status', source['source_id'], code=7)['name'], 'committed')

    def test_retry_rejects_sent_pending_canceled_and_wrong_attempt(self):
        for state in ('pending', 'sent'):
            item = self.prepared_item(state=state, source_state='running')
            self.assertEqual(self.retry({**item, 'attempts': 1}, code=4)['code'], 'item:not_failed')
        item = self.prepared_item(source_state='canceled')
        self.assertEqual(self.retry(item, code=4)['code'], 'source:canceled')
        item = self.prepared_item()
        self.assertEqual(self.retry({**item, 'attempts': 2}, code=4)['code'], 'item:attempt_conflict')

    def test_retry_preserves_running_or_paused_source_without_dispatch(self):
        for state in ('running', 'paused'):
            item = self.prepared_item(source_state=state)
            result = self.retry(item)
            self.assertEqual(result['attempts'], 2)
            self.assertEqual(self.target('status', item['source_id'], code=7)['status'], state)

    def test_retry_import_failure_can_resume_to_complete_without_changing_frozen_input(self):
        with patch('stylelora.exchange.importer.copy_verified', side_effect=ExchangeError('image:decode')):
            report = self.import_inputs()
        sid = report['source_id']
        self.assertEqual(report['failed'], 1)
        item = self.target('items', sid)['items'][0]
        self.assertEqual(item['version'], self.target('item', item['item_id'])['version'])
        self.assertEqual(self.retry(item)['attempts'], 2)
        self.assertEqual(self.target('status', sid, code=7)['status'], 'paused')
        resumed = self.target('resume', sid)
        self.assertEqual(resumed['result']['imported'], 1)
        final = self.target('item', item['item_id'])
        self.assertEqual((final['status'], final['attempts']), ('done', 2))
        self.assertEqual(self.retry(final, code=4)['code'], 'item:not_failed')

    def test_retry_cannot_reauthorize_after_second_failure_with_old_attempt(self):
        item = self.prepared_item()
        self.retry(item)
        self.write(commands.transition_source, item['source_id'], expected='paused', target='running', reason='fixture')
        self.write(commands.mark_sent, item['item_id'])
        self.write(commands.fail_item, item['item_id'], attempt=2, error='fixture:second')
        current = self.target('item', item['item_id'])
        self.assertEqual(self.retry({**current, 'attempts': 1}, code=4)['code'], 'item:attempt_conflict')
        self.assertEqual(self.retry(current)['attempts'], 3)

    def test_http_retry_strict_body_missing_precondition_and_missing_key(self):
        item = self.prepared_item()
        path = self.p+f"/source-items/{item['item_id']}/retry"
        for body in ({'expected_attempt': True}, {'expected_attempt': 0}, {'expected_attempt': '1'}, {'expected_attempt': 1, 'run': True}):
            self.request('POST', path, body, status=422, version=item['version'])
        self.assertEqual(self.request('POST', path, {'expected_attempt': 1}, status=428)['error']['code'], 'concurrency:precondition_required')
        response = self.client.post(path, json={'expected_attempt': 1}, headers={'If-Match': '"'+item['version']+'"'})
        self.assertEqual(response.status_code, 428)
        self.assertEqual(self.request('GET', path[:-6])['attempts'], 1)

    def test_item_missing_and_unknown_queries_do_not_mutate(self):
        self.request('GET', self.p+'/source-items/999999', status=404)
        self.request('POST', self.p+'/source-items/999999/retry', {'expected_attempt': 1}, version='a'*64, status=404)
        item = self.prepared_item()
        path = self.p+f"/source-items/{item['item_id']}"
        self.request('GET', path+'?extra=1', status=422)
        self.request('POST', path+'/retry?extra=1', {'expected_attempt': 1}, version=item['version'], status=422)
        self.assertEqual(self.target('item', item['item_id'])['attempts'], 1)

    def test_concurrent_retry_transactions_increment_once(self):
        item = self.prepared_item()
        context = self.runtime.context
        def retry():
            try:
                return self.runtime.dataset.retry_item(context, item['item_id'], 1, item['version'])
            except Exception as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: retry(), range(2)))
        self.assertEqual(len([v for v in values if isinstance(v, dict)]), 1)
        self.assertEqual(self.target('item', item['item_id'])['attempts'], 2)
        with read_snapshot(context.handle) as db:
            self.assertEqual(sources.get_source(db, item['source_id']).status, 'paused')
