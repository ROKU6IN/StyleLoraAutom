from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import patch
from uuid import uuid4

from stylelora.generation import costs
from stylelora.generation.costs import CostEvidence
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import sources
from stylelora.project_db.storage import atomic_json
from stylelora.project_db.types import ItemSeed
from stylelora.project_db.validation import digest
from stylelora.runtime.errors import ServiceError
from .api_support import APITest
from .test_generation_handoff import draft, FakeDriver


class CostTests(APITest):
    def setUp(self):
        super().setUp()
        self.context = self.runtime.context
        self.project = self.context.handle
        self.service = self.runtime.generation_costs
        self.runtime.generation_sources.driver = FakeDriver()
        self.source = self.request('POST', self.p + '/sources', draft(2), status=201)
        self.sid = self.source['source_id']
        self.path = self.p + f'/sources/{self.sid}'
        self.write(commands.start_source, self.sid, [ItemSeed(1, 'forest'), ItemSeed(2, 'river')])
        with read_snapshot(self.project) as db:
            self.ids = [r[0] for r in db.execute('SELECT item_id FROM source_items WHERE source_id=? ORDER BY seq', (self.sid,))]
        self.account = f'/api/backends/nai_launcher/account-status?project_id={self.pid}&source_id={self.sid}'

    def write(self, fn, *args, **kwargs):
        return submit_write(self.project, lambda db: fn(db, *args, **kwargs)).result()

    def item(self, index=0):
        with read_snapshot(self.project) as db:
            return sources.get_item(db, self.ids[index])

    def state(self):
        return self.request('GET', self.path + '/cost-state')

    def reserve(self, value=0, index=0, cycle=None):
        return self.service.reserve_dispatch(self.context, self.ids[index], estimated_anlas=value,
            expected_cycle_id=cycle or commands.cost_cycle_id(self.project, self.sid),
            instance_id=digest('instance'), contract_digest=digest('contract'))

    def policy(self, value):
        state = self.state()
        return self.request('PATCH', self.path, {'cost_policy': value, 'confirm_allow': value == 'allow'},
            version=state['source_version'], headers={'Idempotency-Key': str(uuid4())})

    def evidence(self, row, outcome):
        return CostEvidence(row['operation_key'], row['instance_id'], row['contract_digest'], outcome, digest(outcome))

    def fail(self, index=0):
        item = self.item(index)
        self.write(commands.fail_item, item.item_id, attempt=item.attempts, error='fixture:failed')

    def test_zero_estimate_records_before_sent_and_never_claims_free(self):
        original = commands.mark_sent
        def checked(db, item_id):
            self.assertEqual(costs._read(self.project, item_id, 1)['estimated_anlas'], 0)
            return original(db, item_id)
        with patch.object(commands, 'mark_sent', checked):
            row = self.reserve()
        self.assertEqual(self.item().status, 'sent')
        view = self.request('GET', self.account)
        self.assertEqual(view['counts']['unknown'], 1)
        self.assertEqual(view['estimated_anlas'], 0)
        self.assertFalse(view['zero_estimate_is_free'])
        self.assertIsNone(view['actual_charged_anlas'])
        self.assertIsNone(view['balance'])
        self.assertNotIn('forest', json.dumps(row))

    def test_ask_pause_does_not_increment_attempts_or_write_receipt(self):
        self.assertIsNone(self.reserve(4))
        self.assertEqual(self.item().attempts, 0)
        self.assertEqual(self.item().status, 'pending')
        self.assertFalse((self.root/'images/.costs').exists())
        state = self.state()
        self.assertTrue(state['can_confirm'])
        self.assertEqual(state['pending']['estimated_anlas'], 4)
        self.assertEqual(self.request('GET', self.account)['attempts'], 0)

    def test_deny_has_no_confirmation_and_cannot_be_bypassed(self):
        self.policy('deny')
        self.assertIsNone(self.reserve(4))
        state = self.state()
        self.assertFalse(state['confirmation_required'])
        result = self.request('POST', self.path+'/confirm-cost', {'cycle_id': state['cycle_id']}, version=state['source_version'], status=409)
        self.assertEqual(result['error']['code'], 'cost:denied')

    def test_confirmation_is_cycle_scoped_and_same_request_never_redispatches(self):
        self.reserve(4)
        state, key = self.state(), str(uuid4())
        body = {'cycle_id': state['cycle_id']}
        first = self.request('POST', self.path+'/confirm-cost', body, version=state['source_version'], key=key)
        self.assertEqual(first, self.request('POST', self.path+'/confirm-cost', body, version=state['source_version'], key=key))
        self.assertEqual(len(self.runtime.generation_sources.driver.received), 1)
        self.assertTrue(self.state()['acknowledged'])
        self.assertIsNotNone(self.reserve(8))
        self.request('POST', self.path+'/confirm-cost', body, version=first['version'], status=409)

    def test_pause_resume_clears_ack_and_reprepares_cost(self):
        self.reserve(4)
        state = self.state()
        self.request('POST', self.path+'/confirm-cost', {'cycle_id': state['cycle_id']}, version=state['source_version'])
        self.write(commands.transition_source, self.sid, expected='running', target='paused', reason='user')
        self.write(commands.transition_source, self.sid, expected='paused', target='running', reason='user')
        self.assertFalse(self.state()['acknowledged'])
        self.assertIsNone(self.reserve(9))
        self.assertEqual(self.state()['pending']['estimated_anlas'], 9)

    def test_policy_change_clears_ack_and_rejects_stale_cycle(self):
        self.policy('allow')
        old = self.state()['cycle_id']
        self.policy('ask')
        with self.assertRaises(ServiceError):
            self.reserve(3, cycle=old)
        self.assertIsNone(self.reserve(3))

    def test_policy_update_requires_version_and_explicit_allow_confirmation(self):
        self.request('PATCH', self.path, {'cost_policy': 'deny'}, headers={'Idempotency-Key':str(uuid4())}, status=428)
        current = self.state()
        self.request('PATCH', self.path, {'cost_policy':'allow'}, version=current['source_version'], headers={'Idempotency-Key':str(uuid4())}, status=422)
        self.policy('deny')
        self.request('PATCH', self.path, {'cost_policy':'ask'}, version=current['source_version'], headers={'Idempotency-Key':str(uuid4())}, status=409)
        self.assertEqual(self.state()['cost_policy'], 'deny')

    def test_allow_policy_update_response_loss_has_session_idempotency(self):
        state, key = self.state(), str(uuid4())
        args = {'version':state['source_version'], 'headers':{'Idempotency-Key':key}}
        body = {'cost_policy':'allow', 'confirm_allow':True}
        first = self.request('PATCH', self.path, body, **args)
        self.assertEqual(first, self.request('PATCH', self.path, body, **args))
        self.request('PATCH', self.path, {'cost_policy':'deny'}, status=409, **args)

    def test_invalid_estimates_pause_before_mark_sent(self):
        for value in (-1, True, 1.0, None, '5', float('inf'), costs.MAX_ESTIMATE+1):
            with self.subTest(value=value):
                with self.assertRaises(ServiceError) as error:
                    self.reserve(value)
                self.assertEqual(error.exception.code, 'cost:invalid_estimate')
                self.assertEqual(self.item().attempts, 0)
                self.write(commands.transition_source, self.sid, expected='paused', target='running', reason='fixture')
        self.assertFalse((self.root/'images/.costs').exists())

    def test_receipt_failure_never_marks_sent(self):
        with patch.object(costs, 'atomic_json', side_effect=OSError('secret-path')):
            with self.assertRaises(ServiceError) as error:
                self.reserve()
        self.assertEqual(error.exception.code, 'cost:receipt_write_failed')
        self.assertEqual(self.item().status, 'pending')
        self.assertEqual(self.request('GET', self.account)['attempts'], 0)

    def test_replace_then_error_is_verified_without_duplicate_dispatch(self):
        def after(path, value):
            atomic_json(path, value)
            raise OSError()
        with patch.object(costs, 'atomic_json', after):
            self.reserve()
        with self.assertRaises(ServiceError):
            self.reserve()
        self.assertEqual(self.request('GET', self.account)['attempts'], 1)

    def test_mark_commit_unknown_never_returns_permission_to_send(self):
        original = commands.mark_sent
        def fault(db, item_id):
            result = original(db, item_id)
            db.after_commit.append(lambda: (_ for _ in ()).throw(OSError()))
            return result
        with patch.object(commands, 'mark_sent', fault):
            with self.assertRaises(ServiceError) as error:
                self.reserve()
        self.assertEqual(error.exception.code, 'cost:dispatch_commit_unknown')
        self.assertEqual(self.item().status, 'sent')
        self.assertEqual(self.state()['status'], 'paused')
        self.assertEqual(self.request('GET', self.account)['counts']['unknown'], 1)

    def test_policy_race_after_receipt_prevents_dispatch(self):
        self.policy('allow')
        def change(path, value):
            atomic_json(path, value)
            self.write(commands.set_cost_policy, self.sid, policy='deny')
        with patch.object(costs, 'atomic_json', change):
            with self.assertRaises(ServiceError):
                self.reserve(4)
        self.assertEqual(self.item().status, 'pending')
        self.assertEqual(self.request('GET', self.account)['attempts'], 0)

    def test_failed_mark_before_commit_can_freshly_prepare_pending_intent(self):
        with patch.object(commands, 'mark_sent', side_effect=OSError()):
            with self.assertRaises(ServiceError):
                self.reserve()
        old = costs._read(self.project, self.ids[0], 1)
        self.write(commands.transition_source, self.sid, expected='paused', target='running', reason='fixture')
        current = self.reserve()
        self.assertNotEqual(current['operation_key'], old['operation_key'])

    def test_exact_evidence_deduplicates_and_rejected_is_excluded(self):
        self.policy('allow')
        row = self.reserve(7)
        accepted = self.evidence(row, 'submitted')
        self.service.observe(self.context, row['item_id'], 1, accepted)
        self.service.observe(self.context, row['item_id'], 1, accepted)
        self.assertEqual(self.request('GET', self.account)['estimated_anlas'], 7)
        self.service.observe(self.context, row['item_id'], 1, self.evidence(row, 'rejected'))
        view = self.request('GET', self.account)
        self.assertEqual(view['estimated_anlas'], 0)
        self.assertEqual(view['rejected_estimated_anlas'], 7)
        with self.assertRaises(ServiceError):
            self.service.observe(self.context, row['item_id'], 1, accepted)
        self.assertEqual(self.item().status, 'sent')  # accounting is not item settlement

    def test_unmatched_identity_attempt_or_evidence_never_changes_record(self):
        row = self.reserve()
        evidence = self.evidence(row, 'submitted')
        before = costs._path(self.project, self.ids[0], 1).read_bytes()
        for value in (replace(evidence, operation_key=digest('other')), replace(evidence, instance_id=digest('other')),
                      replace(evidence, contract_digest=digest('other')), replace(evidence, evidence_digest=''),
                      replace(evidence, outcome='timeout'), {'outcome':'rejected'}):
            with self.assertRaises(ServiceError):
                self.service.observe(self.context, row['item_id'], 1, value)
        self.assertEqual(costs._path(self.project, self.ids[0], 1).read_bytes(), before)

    def test_retry_has_separate_attempt_and_does_not_count_allocated_pending(self):
        self.policy('allow')
        first = self.reserve(3)
        self.service.observe(self.context, first['item_id'], 1, self.evidence(first, 'rejected'))
        self.fail()
        self.write(commands.retry_failed_item, self.ids[0], expected_attempt=1)
        self.assertEqual(self.request('GET', self.account)['attempts'], 1)
        self.write(commands.transition_source, self.sid, expected='paused', target='running', reason='fixture')
        second = self.reserve(5)
        self.assertEqual(second['attempt'], 2)
        view = self.request('GET', self.account)
        self.assertEqual(view['attempts'], 2)
        self.assertEqual(view['estimated_anlas'], 5)
        self.assertEqual(view['counts']['rejected'], 1)

    def test_corrupt_missing_or_oversize_receipts_show_incomplete_not_zero(self):
        self.reserve()
        path = costs._path(self.project, self.ids[0], 1)
        original = path.read_bytes()
        for data in (b'{', b'x'*(costs.MAX_RECORD+1), b'{"content":{},"content":{}}'):
            path.write_bytes(data)
            view = self.request('GET', self.account)
            self.assertIsNone(view['estimated_anlas'])
            self.assertFalse(view['estimate_complete'])
            self.assertEqual(view['counts']['invalid'], 1)
            self.assertEqual(path.read_bytes(), data)
        path.write_bytes(original)
        path.unlink()
        self.assertEqual(self.request('GET', self.account)['counts']['missing'], 1)

    def test_old_attempt_without_receipt_is_not_assumed_free(self):
        self.write(commands.mark_sent, self.ids[0])
        view = self.request('GET', self.account)
        self.assertFalse(view['estimate_complete'])
        self.assertIsNone(view['estimated_anlas'])

    def test_summary_scan_limit_is_explicit(self):
        self.reserve()
        with patch.object(costs, 'MAX_ATTEMPTS', 0):
            view = self.request('GET', self.account)
        self.assertEqual(view['counts']['unscanned'], 1)
        self.assertIsNone(view['estimated_anlas'])

    def test_read_only_summary_does_not_create_files_or_contact_driver(self):
        with patch.object(self.runtime.generation_sources.driver, 'capabilities', side_effect=AssertionError()):
            self.assertEqual(self.request('GET', self.account)['estimated_anlas'], 0)
        self.assertFalse((self.root/'images/.costs').exists())
        self.request('GET', self.path+'/cost-state?force=1', status=422)
        self.request('POST', self.account.split('?')[0], {}, status=405)
        self.assertEqual(self.client.get(self.account, headers={'Authorization':'Bearer wrong'}).status_code, 401)

    def test_runtime_restart_discards_ack_but_preserves_estimate(self):
        from fastapi.testclient import TestClient
        from stylelora.api.app import create_app
        from stylelora.runtime.lifecycle import Runtime
        self.policy('allow')
        self.reserve(5)
        view = self.request('GET', self.account)
        self.client.__exit__(None, None, None)
        moved = self.base/'moved.slproj'
        self.root.rename(moved)
        self.runtime = Runtime(authority='127.0.0.1:8301')
        self.client = TestClient(create_app(self.runtime), base_url='http://127.0.0.1:8301',
            headers={'Authorization':'Bearer '+self.runtime.token})
        self.client.__enter__()
        opened = self.runtime.open_now(moved)
        self.pid, self.p = opened['project_id'], '/api/projects/'+opened['project_id']
        self.path = self.p+f'/sources/{self.sid}'
        self.account = f'/api/backends/nai_launcher/account-status?project_id={self.pid}&source_id={self.sid}'
        self.assertEqual(self.request('GET', self.account), view)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertFalse(self.state()['acknowledged'])
        self.assertFalse(self.state()['can_confirm'])

    def test_production_adapter_unavailable_never_confirms_or_dispatches(self):
        self.reserve(3)
        self.runtime.generation_sources.driver = None
        state = self.state()
        self.assertFalse(state['can_confirm'])
        self.request('POST', self.path+'/confirm-cost', {'cycle_id':state['cycle_id']}, version=state['source_version'], status=503)
        self.assertEqual(self.state()['status'], 'paused')
        self.assertFalse(self.state()['acknowledged'])

    def test_confirmation_dispatch_failure_pauses_and_revokes_ack(self):
        self.reserve(3)
        state = self.state()
        with patch.object(self.runtime.generation_sources.driver, 'dispatch', side_effect=OSError()):
            self.request('POST', self.path+'/confirm-cost', {'cycle_id':state['cycle_id']}, version=state['source_version'])
        self.assertEqual(self.state()['status'], 'paused')
        self.assertFalse(self.state()['acknowledged'])

    def test_active_dispatch_cannot_restore_confirmation_or_grant_second_item(self):
        self.reserve()
        with self.assertRaises(ServiceError):
            self.reserve(index=1)
        self.assertEqual(self.item(1).attempts, 0)

    def test_estimate_sum_overflow_is_not_rounded_in_json(self):
        self.policy('allow')
        self.reserve(costs.MAX_ESTIMATE)
        self.fail()
        self.reserve(1, index=1)
        view = self.request('GET', self.account)
        self.assertTrue(view['amount_overflow'])
        self.assertIsNone(view['estimated_anlas'])

    def test_receipt_from_other_source_is_invalid_even_with_valid_digest(self):
        row = self.reserve()
        changed = {**row, 'source_id':self.sid+1}
        atomic_json(costs._path(self.project, self.ids[0], 1), {'content':changed,'digest':digest(changed)})
        self.assertEqual(self.request('GET', self.account)['counts']['invalid'], 1)

    def test_confirmation_rechecks_version_inside_writer(self):
        from stylelora.project_db.types import SourcePatch
        self.reserve(3)
        state = self.state()
        original = commands.confirm_cost_and_resume
        def changed(*args, **kwargs):
            self.write(sources.update_source, self.sid, SourcePatch(name='changed before commit'))
            return original(*args, **kwargs)
        with patch.object(commands, 'confirm_cost_and_resume', changed):
            self.request('POST', self.path+'/confirm-cost', {'cycle_id':state['cycle_id']}, version=state['source_version'], status=409)
        self.assertFalse(self.state()['acknowledged'])
        self.assertEqual(self.runtime.generation_sources.driver.received, [])

    def test_concurrent_reservation_returns_exactly_one_dispatch_grant(self):
        cycle = commands.cost_cycle_id(self.project, self.sid)
        def reserve():
            try:
                return self.reserve(cycle=cycle)
            except ServiceError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(sum(row is not None for row in results), 1)
        self.assertEqual(self.item().attempts, 1)

    def test_foreign_pending_intent_cannot_be_overwritten(self):
        with patch.object(commands, 'mark_sent', side_effect=OSError()):
            with self.assertRaises(ServiceError):
                self.reserve()
        path = costs._path(self.project, self.ids[0], 1)
        row = costs._read(self.project, self.ids[0], 1)
        row['source_id'] += 1
        atomic_json(path, {'content':row, 'digest':digest(row)})
        before = path.read_bytes()
        self.write(commands.transition_source, self.sid, expected='paused', target='running', reason='fixture')
        with self.assertRaises(ServiceError):
            self.reserve()
        self.assertEqual(path.read_bytes(), before)
