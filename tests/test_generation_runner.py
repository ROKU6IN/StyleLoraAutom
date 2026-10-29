import asyncio
from copy import deepcopy
from dataclasses import replace
import threading
from unittest.mock import patch

from stylelora.generation import operations
from stylelora.generation.runner import SerialDriver, Preparation
from stylelora.generation.settings import SettingsLeases
from stylelora.project_db import commands
from stylelora.project_db.artifacts import receipt_path
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import sources, images
from stylelora.project_db.types import ItemSeed
from stylelora.runtime.errors import ServiceError
from .api_support import APITest
from .generation_support import FakeCycleAdapter
from .test_generation_handoff import draft


class RunnerTests(APITest):
    def setUp(self):
        super().setUp()
        self.context, self.project = self.runtime.context, self.runtime.context.handle
        self.runtime.settings_leases = SettingsLeases(self.base/'recovery')
        self.adapter = FakeCycleAdapter()
        self.driver = SerialDriver(self.runtime, self.adapter)
        self.runtime.generation_sources.driver = self.driver
        self.source = self.request('POST', self.p+'/sources', draft(2), status=201)
        self.sid = self.source['source_id']
        self.path = self.p+f'/sources/{self.sid}'
        self.write(commands.start_source, self.sid, [ItemSeed(1, 'forest'), ItemSeed(2, 'river')])
        with read_snapshot(self.project) as db:
            self.ids = [r[0] for r in db.execute('SELECT item_id FROM source_items WHERE source_id=? ORDER BY seq', (self.sid,))]

    def write(self, fn, *args, **kwargs):
        return submit_write(self.project, lambda db: fn(db, *args, **kwargs)).result()

    def item(self, index=0):
        with read_snapshot(self.project) as db:
            return sources.get_item(db, self.ids[index])

    def view(self):
        return self.request('GET', self.path)

    def control(self, action):
        return self.request('POST', self.path+'/control', {'action':action}, version=self.view()['version'])

    def run_cycle(self):
        self.driver.dispatch(self.context, self.sid)
        self.driver.wait()

    def reason(self):
        return self.runtime.generation_sources.reasons.get((self.pid, self.sid))

    def test_ordered_generation_registers_and_restores_with_http_export(self):
        original = deepcopy(self.adapter.values)
        self.run_cycle()
        self.assertEqual(self.view()['status'], 'done')
        self.assertEqual([x[0]['item_id'] for x in self.adapter.submitted], self.ids)
        self.assertGreaterEqual(self.adapter.submitted[1][2]-self.adapter.submitted[0][2], .99)
        self.assertEqual(self.adapter.values, original)
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])
        self.assertEqual(self.context.users, 0)
        self.keep([self.item(0).image_id, self.item(1).image_id])
        result = self.run_export(self.export_preview())
        self.assertEqual(result['status'], 'finished')

    def test_pause_during_prepare_cancels_without_sent(self):
        async def hook(row):
            self.control('pause')
        self.adapter.prepare_hook = hook
        self.run_cycle()
        self.assertEqual((self.item().status, self.item().attempts), ('pending', 0))
        self.assertEqual(len(self.adapter.canceled), 1)
        self.assertEqual(len(self.adapter.submitted), 0)
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])

    def test_pause_after_sent_accepts_late_image_and_stops_next(self):
        async def hook(row, grant):
            self.assertEqual(self.item().status, 'sent')
            self.control('pause')
            return self.adapter.complete(row, grant)
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual((self.item().status, self.item(1).status, self.view()['status']), ('done', 'pending', 'paused'))
        self.assertEqual(len(self.adapter.submitted), 1)

    def test_cancel_accepts_late_image_and_remains_canceled(self):
        async def hook(row, grant):
            self.control('cancel')
            return self.adapter.complete(row, grant)
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual((self.item().status, self.view()['status']), ('done', 'canceled'))
        self.assertEqual(len(self.adapter.submitted), 1)

    def test_ask_cancels_then_http_cycle_confirmation_reprepares(self):
        self.adapter.estimates = [3, 4, 5]
        self.run_cycle()
        self.assertEqual(len(self.adapter.submitted), 0)
        state = self.request('GET', self.path+'/cost-state')
        self.assertTrue(state['can_confirm'])
        self.request('POST', self.path+'/confirm-cost', {'cycle_id': state['cycle_id']}, version=state['source_version'])
        self.driver.wait()
        self.assertEqual(self.view()['status'], 'done')
        self.assertEqual(len(self.adapter.prepared), 3)
        self.assertEqual(len(self.adapter.canceled), 1)
        summary = self.runtime.generation_costs.summary(self.context, self.sid)
        self.assertEqual(summary['estimated_anlas'], 9)
        self.assertEqual(summary['counts']['submitted'], 2)

    def test_deny_nonzero_never_submits(self):
        self.write(commands.set_cost_policy, self.sid, policy='deny')
        # Runtime policy service supplies a fresh cycle after policy changes.
        self.write(commands.transition_source, self.sid, expected='running', target='paused', reason='fixture')
        self.write(commands.transition_source, self.sid, expected='paused', target='running', reason='fixture')
        self.adapter.estimates = [3]
        self.run_cycle()
        self.assertEqual(self.view()['status'], 'paused')
        self.assertEqual(len(self.adapter.submitted), 0)
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])

    def test_policy_change_during_prepare_cancels_old_cycle(self):
        async def hook(row):
            state = self.runtime.generation_costs.state(self.context, self.sid)
            self.runtime.generation_costs.set_policy(self.context, self.sid, {'cost_policy':'deny'}, state['source_version'])
        self.adapter.prepare_hook = hook
        self.run_cycle()
        self.assertEqual(len(self.adapter.submitted), 0)
        self.assertEqual(len(self.adapter.canceled), 1)
        self.assertEqual(self.view()['status'], 'paused')

    def test_timeout_keeps_sent_fence_and_no_automatic_retry(self):
        self.driver.timeout = .03
        async def hook(row, grant):
            await asyncio.sleep(1)
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual((self.item().status, self.view()['status']), ('sent', 'paused'))
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])
        self.run_cycle()  # Even an invalid internal dispatch cannot resend.
        self.assertEqual(len(self.adapter.submitted), 1)
        self.assertEqual(self.runtime.generation_costs.summary(self.context, self.sid)['counts']['unknown'], 1)

    def test_lost_response_probe_registers_without_resubmitting_and_restore_is_explicit(self):
        async def hook(row, grant):
            self.adapter.complete(row, grant)
            raise OSError('private-token and private prompt')
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.reason(), 'generation:outcome_unknown')
        writes = len(self.adapter.writes)
        self.driver.reconcile(self.context, self.ids[0]); self.driver.wait()
        self.assertEqual(self.item().status, 'done')
        self.assertEqual(len(self.adapter.writes), writes)
        self.assertEqual(self.runtime.settings_leases.status()['phase'], 'needs_restore')
        self.driver.restore(self.context, self.ids[0]); self.driver.wait()
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])
        self.assertEqual(len(self.adapter.submitted), 1)
        self.assertEqual(self.view()['status'], 'paused')

    def test_missing_remote_result_remains_unknown(self):
        async def hook(row, grant):
            raise ConnectionError()
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.driver.reconcile(self.context, self.ids[0]); self.driver.wait()
        self.assertEqual(self.item().status, 'sent')
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])
        self.assertEqual(len(self.adapter.submitted), 1)

    def test_unknown_prepare_fences_pending_until_readonly_proof(self):
        async def hook(row):
            raise asyncio.CancelledError()
        self.adapter.prepare_hook = hook
        self.run_cycle()
        self.assertEqual((self.item().status, self.item().attempts), ('pending', 0))
        self.assertEqual(self.view()['status'], 'paused')
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])
        row = self.adapter.prepared[0]
        self.adapter.results[row['operation_key']] = self.adapter.outcome(row, None, 'canceled')
        self.adapter.generations[row['operation_key']] = True
        self.driver.reconcile(self.context, self.ids[0]); self.driver.wait()
        self.assertEqual(len(self.adapter.canceled), 0)
        self.driver.restore(self.context, self.ids[0]); self.driver.wait()
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])

    def test_unknown_cancel_preserves_fence_and_cost_confirmation_block(self):
        self.adapter.estimates = [5]
        async def hook(row):
            raise ConnectionError()
        self.adapter.cancel_hook = hook
        self.run_cycle()
        state = self.request('GET', self.path+'/cost-state')
        self.assertFalse(state['can_confirm'])
        self.assertEqual(self.item().attempts, 0)
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])

    def test_terminal_rejection_fails_once_excludes_estimate_and_pauses(self):
        async def hook(row, grant):
            self.adapter.generations[row['operation_key']] = True
            return self.adapter.outcome(row, grant, 'rejected')
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual((self.item().status, self.item(1).status, self.view()['status']), ('failed','pending','paused'))
        self.assertEqual(self.runtime.generation_costs.summary(self.context, self.sid)['counts']['rejected'], 1)
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])

    def test_manual_retry_uses_new_attempt_and_unique_path(self):
        async def hook(row, grant):
            self.adapter.generations[row['operation_key']] = True
            return self.adapter.outcome(row, grant, 'rejected')
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.write(commands.retry_failed_item, self.ids[0], expected_attempt=1)
        self.adapter.submit_hook = None
        self.control('resume'); self.driver.wait()
        self.assertEqual((self.item().status, self.item().attempts), ('done', 2))
        self.assertEqual(len(set(self.adapter.paths.values())), 3)

    def test_forged_result_binding_cannot_register_or_clear_fence(self):
        async def hook(row, grant):
            return replace(self.adapter.complete(row, grant), request_digest='b'*64)
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertFalse(receipt_path(self.project.root, self.ids[0], 1).exists())
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])

    def test_wrong_owned_hash_never_creates_recoverable_receipt(self):
        async def hook(row, grant):
            return replace(self.adapter.complete(row, grant), artifact_sha256='b'*64)
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.reason(), 'artifact:ownership_conflict')
        self.assertFalse(receipt_path(self.project.root, self.ids[0], 1).exists())
        self.runtime._close_current()
        self.runtime.open_now(self.root)
        self.context, self.project = self.runtime.context, self.runtime.context.handle
        self.assertEqual(self.item().status, 'sent')

    def test_metadata_mismatch_registers_rejected_image_without_retry(self):
        async def hook(row, grant):
            return self.adapter.complete(row, grant, prompt='other, masterpiece')
        self.adapter.submit_hook = hook
        self.run_cycle()
        with read_snapshot(self.project) as db:
            image = images.get_image(db, self.item().image_id)
        self.assertEqual(image.review_status, 'reject')
        self.assertEqual(self.view()['status'], 'done')
        self.assertEqual(len(self.adapter.submitted), 2)

    def test_incomplete_png_keeps_sent(self):
        async def hook(row, grant):
            result = self.adapter.complete(row, grant)
            self.adapter.paths[row['operation_key']].write_bytes(b'bad')
            return result
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertTrue(self.runtime.settings_leases.status()['blocked'])

    def test_terminal_response_without_settlement_does_not_register(self):
        async def hook(row, grant):
            result = self.adapter.complete(row, grant)
            self.adapter.generations[row['operation_key']] = False
            return result
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertFalse(receipt_path(self.project.root, self.ids[0], 1).exists())

    def test_effective_drift_stops_before_cost_and_submit(self):
        original = self.adapter.prepare
        async def wrong(*args):
            prepared = await original(*args)
            return Preparation(0, {**prepared.effective, 'steps':28})
        self.adapter.prepare = wrong
        self.run_cycle()
        self.assertEqual(self.reason(), 'profile:drift')
        self.assertEqual(self.item().attempts, 0)
        self.assertEqual(len(self.adapter.submitted), 0)

    def test_invalid_estimate_blocks_sent_and_keeps_preparation_fence(self):
        self.adapter.estimates = [True]
        self.run_cycle()
        self.assertEqual(self.reason(), 'cost:invalid_estimate')
        self.assertEqual(self.item().attempts, 0)
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])

    def test_instance_change_after_prepare_never_submits(self):
        async def hook(row):
            self.adapter.identity = replace(self.adapter.identity, instance_id='b'*64)
        self.adapter.prepare_hook = hook
        self.run_cycle()
        self.assertEqual(len(self.adapter.submitted), 0)
        self.assertEqual(self.item().attempts, 0)

    def test_unknown_mark_sent_commit_never_submits(self):
        original = commands.mark_sent
        def unknown(db, item_id):
            result = original(db, item_id)
            db.after_commit.append(lambda: (_ for _ in ()).throw(OSError('commit response lost')))
            return result
        with patch.object(commands, 'mark_sent', unknown):
            self.run_cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertEqual(len(self.adapter.submitted), 0)
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])

    def test_prepare_intent_write_failure_prevents_prepare(self):
        with patch.object(operations, 'atomic_json', side_effect=OSError('disk full')):
            self.run_cycle()
        self.assertEqual(len(self.adapter.prepared), 0)
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])

    def test_intent_replace_then_error_does_not_duplicate_prepare(self):
        original = operations.atomic_json
        def unknown(path, data):
            original(path, data)
            raise OSError()
        with patch.object(operations, 'atomic_json', unknown):
            self.run_cycle()
        self.assertEqual(self.view()['status'], 'done')
        self.assertEqual(len(self.adapter.submitted), 2)

    def test_corrupt_operation_prevents_prepare(self):
        path = operations.path_for(self.project, self.ids[0], 1)
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        self.run_cycle()
        self.assertEqual(self.reason(), 'generation:invalid_operation')
        self.assertEqual(len(self.adapter.prepared), 0)

    def test_preexisting_file_is_preserved_without_prepare(self):
        path = self.root/f'images/.pending/{self.ids[0]}/1/out.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'existing')
        self.run_cycle()
        self.assertEqual(path.read_bytes(), b'existing')
        self.assertEqual(len(self.adapter.prepared), 0)
        self.assertEqual(self.reason(), 'artifact:save_path_exists')

    def test_duplicate_dispatch_while_active_is_rejected_and_project_retained(self):
        entered, release = threading.Event(), threading.Event()
        async def hook(row):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.005)
        self.adapter.prepare_hook = hook
        try:
            self.driver.dispatch(self.context, self.sid)
            self.assertTrue(entered.wait(3))
            self.assertGreater(self.context.users, 0)
            with self.assertRaises(ServiceError):
                self.driver.dispatch(self.context, self.sid)
            self.control('pause')
        finally:
            release.set()
            self.driver.wait()
        self.assertEqual(len(self.adapter.prepared), 1)
        self.assertEqual(self.context.users, 0)

    def test_project_close_drains_authorized_item_without_starting_next(self):
        entered, release = threading.Event(), threading.Event()
        async def hook(row, grant):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.005)
            return self.adapter.complete(row, grant)
        self.adapter.submit_hook = hook
        self.driver.dispatch(self.context, self.sid)
        self.assertTrue(entered.wait(3))
        closed = threading.Thread(target=self.runtime._close_current)
        closed.start()
        try:
            for _ in range(100):
                if self.context.closing:
                    break
                threading.Event().wait(.005)
            self.assertTrue(self.context.closing)
        finally:
            release.set()
            closed.join(5)
        self.assertFalse(closed.is_alive())
        self.assertIsNone(self.runtime.context)
        self.assertEqual(len(self.adapter.submitted), 1)
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])

    def test_restore_conflict_preserves_user_change(self):
        async def hook(row, grant):
            self.control('pause')
            result = self.adapter.complete(row, grant)
            self.adapter.values['steps'] = 99
            return result
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.adapter.values['steps'], 99)
        self.assertEqual(self.reason(), 'profile:restore_conflict')
        self.assertEqual(self.item().status, 'done')

    def test_reopen_clears_consent_and_probe_uses_persisted_identity(self):
        async def hook(row, grant):
            self.adapter.complete(row, grant)
            raise ConnectionError()
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.runtime._close_current()
        self.runtime.open_now(self.root)
        self.context, self.project = self.runtime.context, self.runtime.context.handle
        self.runtime.settings_leases = SettingsLeases(self.base/'recovery')
        self.driver = SerialDriver(self.runtime, self.adapter)
        self.runtime.generation_sources.driver = self.driver
        self.assertIsNone(commands.cost_cycle_id(self.project, self.sid))
        self.driver.reconcile(self.context, self.ids[0]); self.driver.wait()
        self.assertEqual(self.item().status, 'done')
        self.driver.restore(self.context, self.ids[0]); self.driver.wait()
        self.assertEqual(len(self.adapter.submitted), 1)

    def test_production_runtime_still_has_no_adapter(self):
        self.runtime.generation_sources.driver = None
        self.assertFalse(self.runtime.generation_sources.capabilities()['generation'])
        self.assertFalse(self.runtime.session()['capabilities']['nai'])

    def test_http_handoff_start_completes_and_replay_never_dispatches(self):
        from . import test_generation_handoff as handoff
        from stylelora.tagging.library import CatalogSnapshot
        self.runtime.catalog.state['catalog'] = {'complete': True, 'tags': [{'tag':'forest', 'aliases':[], 'category':'general'}]}
        self.runtime.catalog._snapshot = CatalogSnapshot(self.runtime.catalog.state)
        source = self.request('POST', self.p+'/sources', draft(1), status=201)
        task = handoff.GenerationTests.task(self, 1)
        path = self.p+f"/sources/{source['source_id']}"
        preview = self.request('POST', path+'/handoff-preview', {'task_id':task['task_id'],
            'expected_task_revision':task['revision']}, version=source['version'])
        self.assertTrue(preview['can_start'])
        body = {k:preview[k] for k in ('preview_id','digest','source_digest','seeds_digest','count')}
        started = self.request('POST', path+'/start', body, version=source['version'])
        self.assertTrue(started['dispatched'])
        self.driver.wait()
        self.assertEqual(self.request('GET', path)['status'], 'done')
        replay = self.request('POST', path+'/start', body, version='obsolete')
        self.assertFalse(replay['dispatched'])
        self.assertEqual(len(self.adapter.submitted), 1)

    def test_complete_commit_response_loss_reconciles_without_duplicate_image(self):
        def fail(point):
            if point == 'artifact_after_commit':
                raise OSError('commit response lost')
        self.project._fault_hook = fail
        self.run_cycle()
        self.project._fault_hook = None
        self.assertEqual(self.item().status, 'done')
        self.assertTrue(self.runtime.settings_leases.status()['inflight'])
        self.driver.reconcile(self.context, self.ids[0]); self.driver.wait()
        self.driver.restore(self.context, self.ids[0]); self.driver.wait()
        with read_snapshot(self.project) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM images').fetchone()[0], 1)
        self.assertEqual(len(self.adapter.submitted), 1)
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])

    def test_cost_evidence_for_other_grant_cannot_register_image(self):
        async def hook(row, grant):
            return replace(self.adapter.complete(row, grant), cost_operation_key='b'*64)
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertFalse(receipt_path(self.project.root, self.ids[0], 1).exists())

    def test_rejection_contradicting_saved_file_stays_unknown(self):
        async def hook(row, grant):
            self.adapter.complete(row, grant)
            return self.adapter.outcome(row, grant, 'rejected')
        self.adapter.submit_hook = hook
        self.run_cycle()
        self.assertEqual(self.item().status, 'sent')
        self.assertEqual(self.runtime.generation_costs.summary(self.context, self.sid)['counts']['rejected'], 0)

    def test_second_source_cannot_use_global_driver_slot(self):
        entered, release = threading.Event(), threading.Event()
        async def hook(row):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.005)
        self.adapter.prepare_hook = hook
        try:
            self.driver.dispatch(self.context, self.sid)
            self.assertTrue(entered.wait(3))
            other = self.request('POST', self.p+'/sources', draft(1), status=201)
            with self.assertRaises(ServiceError):
                self.driver.dispatch(self.context, other['source_id'])
            self.control('pause')
        finally:
            release.set()
            self.driver.wait()
        self.assertEqual(len(self.adapter.prepared), 1)

    def test_settings_apply_unknown_never_prepares(self):
        async def unknown(values, operation_id):
            self.adapter.values.update(values)
            raise ConnectionError('secret')
        self.adapter.on_write = unknown
        self.run_cycle()
        self.assertEqual(len(self.adapter.prepared), 0)
        self.assertEqual(self.item().attempts, 0)
        self.assertEqual(self.runtime.settings_leases.status()['phase'], 'applying')

    def test_restore_unknown_blocks_new_cycle_after_completed_items(self):
        original = self.adapter.update_settings
        async def unknown(values, operation_id, *, expected):
            await original(values, operation_id, expected=expected)
            if len(self.adapter.writes) == 2:
                raise ConnectionError()
        self.adapter.update_settings = unknown
        self.run_cycle()
        self.assertEqual(self.item(1).status, 'done')
        self.assertEqual(self.view()['status'], 'paused')
        self.assertEqual(self.runtime.settings_leases.status()['phase'], 'restoring')

    def test_operation_receipt_contains_no_prompt_path_or_secrets(self):
        self.run_cycle()
        row = operations.path_for(self.project, self.ids[0], 1).read_text(encoding='utf-8')
        for forbidden in ('forest', 'geometric style', 'private-token', str(self.root), 'fixture-original'):
            self.assertNotIn(forbidden, row)
        self.assertTrue(operations.read_operation(self.project, self.ids[0], 1)['closed'])
