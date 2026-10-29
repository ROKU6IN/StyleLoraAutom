import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from stylelora.generation.settings import SettingsLeases
from stylelora.project_db.storage import atomic_json
from stylelora.project_db.validation import digest
from stylelora.runtime.errors import ServiceError
from .api_support import APITest
from .settings_support import FakeSettings
from .support import TEMP_ROOT
from .test_generation_handoff import draft, FakeDriver

CHANGES = {'steps': 28, 'quality_toggle': False, 'uc_preset': None}


class SettingsLeaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.manager = SettingsLeases(self.root, notify=lambda: self.events.append('changed'))
        self.adapter = FakeSettings()
        self.owner = {'project_id':str(uuid4()), 'source_id':1, 'cycle_id':str(uuid4())}

    async def acquire(self, changes=None):
        return await self.manager.acquire(self.adapter, **self.owner, changes=CHANGES if changes is None else changes)

    def row(self):
        return self.manager.journal.load()['record']

    async def test_snapshot_is_durable_before_apply_and_restores_only_changed_keys(self):
        async def write(values, key):
            row = self.row()
            self.assertEqual(row['write_id'], key)
            self.assertEqual(row['phase'], 'applying' if len(self.adapter.writes) == 1 else 'restoring')
            self.assertEqual(row['original']['steps'], 14)
            self.adapter.values.update(values)
            self.adapter.settled[key] = True
        self.adapter.on_write = write
        lease = await self.acquire()
        self.assertEqual(self.adapter.writes[0][0], CHANGES)
        self.adapter.values['sampler'] = 'external-untouched-key'
        await self.manager.restore(self.adapter, lease)
        self.assertEqual(self.adapter.values['steps'], 14)
        self.assertEqual(self.adapter.values['sampler'], 'external-untouched-key')
        self.assertEqual(set(self.adapter.writes[1][0]), set(CHANGES))
        self.assertFalse(self.manager.status()['blocked'])
        self.assertGreater(len(self.events), 1)

    async def test_every_cycle_takes_a_new_original_snapshot(self):
        first = await self.acquire()
        await self.manager.restore(self.adapter, first)
        self.adapter.values['steps'] = 22
        self.owner['cycle_id'] = str(uuid4())
        second = await self.acquire()
        self.assertNotEqual(first, second)
        self.assertEqual(self.row()['original']['steps'], 22)
        await self.manager.restore(self.adapter, second)
        self.assertEqual(self.adapter.values['steps'], 22)

    async def test_second_source_cannot_interleave_even_across_event_loops(self):
        entered, release = threading.Event(), threading.Event()
        async def delayed():
            entered.set()
            await asyncio.to_thread(release.wait, 5)
        self.adapter.on_read = delayed
        task = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(self.acquire())))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        try:
            with self.assertRaises(ServiceError) as error:
                await self.acquire()
            self.assertEqual(error.exception.code, 'profile:lease_busy')
        finally:
            release.set()
        lease = await task
        with self.assertRaises(ServiceError):
            await self.acquire()
        self.adapter.on_read = None
        await self.manager.restore(self.adapter, lease)

    async def test_noop_lease_does_not_write_but_still_excludes_others(self):
        lease = await self.acquire({'steps':14})
        self.assertEqual(self.adapter.writes, [])
        self.assertTrue(self.manager.status()['blocked'])
        await self.manager.restore(self.adapter, lease)
        self.assertEqual(self.adapter.writes, [])
        self.assertFalse(self.manager.status()['blocked'])

    async def test_apply_response_lost_is_not_replayed_and_requires_settlement(self):
        async def lost(values, key):
            self.adapter.values.update(values)
            raise OSError('private response')
        self.adapter.on_write = lost
        with self.assertRaises(ServiceError) as error:
            await self.acquire()
        self.assertEqual(error.exception.code, 'profile:apply_unknown')
        self.assertNotIn('private', str(error.exception))
        row = self.row()
        await self.assert_unresolved(row['lease_id'])
        self.adapter.settled[row['write_id']] = True
        await self.manager.reconcile(self.adapter, row['lease_id'])
        self.assertEqual(len(self.adapter.writes), 1)
        self.adapter.on_write = None
        await self.manager.restore(self.adapter, row['lease_id'])
        self.assertEqual(len(self.adapter.writes), 2)

    async def assert_unresolved(self, lease):
        for method in (self.manager.restore, self.manager.reconcile):
            with self.assertRaises(ServiceError) as error:
                await method(self.adapter, lease)
            self.assertEqual(error.exception.code, 'profile:write_unresolved')
        self.assertTrue(self.manager.status()['blocked'])

    async def test_unchanged_read_cannot_clear_unknown_that_may_apply_later(self):
        async def delayed_write(values, key):
            raise TimeoutError()
        self.adapter.on_write = delayed_write
        with self.assertRaises(ServiceError):
            await self.acquire()
        row = self.row()
        self.assertEqual(self.adapter.values['steps'], 14)
        await self.assert_unresolved(row['lease_id'])
        # The old remote call completes AFTER those unchanged reads.
        self.adapter.values.update(CHANGES)
        self.adapter.settled[row['write_id']] = True
        await self.manager.reconcile(self.adapter, row['lease_id'])
        self.assertEqual(self.manager.status()['phase'], 'needs_restore')
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_known_settled_no_effect_reconciles_without_restore_write(self):
        async def failed(values, key):
            self.adapter.settled[key] = True
            raise OSError()
        self.adapter.on_write = failed
        with self.assertRaises(ServiceError):
            await self.acquire()
        await self.manager.reconcile(self.adapter, self.row()['lease_id'])
        self.assertFalse(self.manager.status()['blocked'])
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_cancelled_apply_keeps_durable_fence_across_restart(self):
        async def cancelled(values, key):
            self.adapter.values.update(values)
            raise asyncio.CancelledError()
        self.adapter.on_write = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.acquire()
        self.manager = SettingsLeases(self.root)
        row = self.row()
        self.assertEqual(self.manager.status()['phase'], 'applying')
        await self.assert_unresolved(row['lease_id'])
        self.adapter.settled[row['write_id']] = True
        await self.manager.reconcile(self.adapter, row['lease_id'])
        self.adapter.on_write = None
        await self.manager.restore(self.adapter, row['lease_id'])

    async def test_external_edit_blocks_all_restore_writes_without_overwriting_it(self):
        lease = await self.acquire()
        self.adapter.values['steps'] = 99
        with self.assertRaises(ServiceError) as error:
            await self.manager.restore(self.adapter, lease)
        self.assertEqual(error.exception.code, 'profile:restore_conflict')
        self.assertEqual(len(self.adapter.writes), 1)
        self.assertEqual(self.adapter.values['steps'], 99)
        self.assertEqual(self.manager.status()['phase'], 'conflict')
        self.adapter.values['steps'] = 28  # Explicit outside resolution, then fresh read.
        await self.manager.restore(self.adapter, lease)
        self.assertEqual(self.adapter.values['steps'], 14)

    async def test_partially_original_settings_restore_only_remaining_owned_fields(self):
        lease = await self.acquire()
        self.adapter.values['steps'] = 14
        await self.manager.restore(self.adapter, lease)
        self.assertNotIn('steps', self.adapter.writes[-1][0])
        self.assertEqual(self.adapter.writes[-1][0], {'quality_toggle':True,'uc_preset':3})

    async def test_restore_response_loss_is_reconciled_read_only_without_second_write(self):
        lease = await self.acquire()
        async def lost(values, key):
            self.adapter.values.update(values)
            self.adapter.settled[key] = True
            raise OSError()
        self.adapter.on_write = lost
        with self.assertRaises(ServiceError) as error:
            await self.manager.restore(self.adapter, lease)
        self.assertEqual(error.exception.code, 'profile:restore_unknown')
        self.manager = SettingsLeases(self.root)
        with self.assertRaises(ServiceError):
            await self.manager.restore(self.adapter, lease)
        await self.manager.reconcile(self.adapter, lease)
        self.assertFalse(self.manager.status()['blocked'])
        self.assertEqual(len(self.adapter.writes), 2)

    async def test_restart_blocks_new_source_and_identity_or_contract_changes(self):
        lease = await self.acquire()
        self.manager = SettingsLeases(self.root)
        with self.assertRaises(ServiceError):
            await self.acquire()
        original = self.adapter.identity
        for identity in (replace(original, instance_id=digest('new-instance')),
                         replace(original, contract_digest=digest('new-contract')),
                         replace(original, writable_keys=('steps',))):
            self.adapter.identity = identity
            for method in (self.manager.restore, self.manager.reconcile):
                with self.assertRaises(ServiceError) as error:
                    await method(self.adapter, lease)
                self.assertEqual(error.exception.code, 'profile:backend_changed')
        self.assertEqual(len(self.adapter.writes), 1)
        self.adapter.identity = original
        await self.manager.restore(self.adapter, lease)

    async def test_unresolved_generation_defers_restore_across_restart(self):
        lease = await self.acquire()
        operation = digest('generation attempt')
        self.manager.mark_inflight(lease, operation)
        self.manager = SettingsLeases(self.root)
        for method in (self.manager.restore, self.manager.reconcile, self.manager.settle_inflight):
            with self.assertRaises(ServiceError) as error:
                await method(self.adapter, lease)
            self.assertEqual(error.exception.code, 'profile:generation_unresolved')
        self.assertEqual(len(self.adapter.writes), 1)
        self.adapter.generations[operation] = True
        await self.manager.settle_inflight(self.adapter, lease)
        await self.manager.restore(self.adapter, lease)
        self.assertFalse(self.manager.status()['blocked'])

    async def test_effective_prepare_drift_cannot_change_locked_desired_values(self):
        lease = await self.acquire()
        await self.manager.verify_effective(self.adapter, lease, self.adapter.values)
        with self.assertRaises(ServiceError) as error:
            await self.manager.verify_effective(self.adapter, lease, {**self.adapter.values, 'steps':14})
        self.assertEqual(error.exception.code, 'profile:drift')
        self.assertEqual(self.row()['desired']['steps'], 28)
        with self.assertRaises(ServiceError):
            self.manager.mark_inflight(lease, digest('attempt'))
        await self.manager.restore(self.adapter, lease)

    async def test_durable_write_failure_prevents_external_apply(self):
        with patch('stylelora.generation.settings.atomic_json', side_effect=OSError('private-path')):
            with self.assertRaises(ServiceError) as error:
                await self.acquire()
        self.assertEqual(error.exception.code, 'profile:journal_write_failed')
        self.assertEqual(self.adapter.writes, [])
        self.assertNotIn('private-path', str(error.exception))

    async def test_missing_null_effective_field_is_not_evidence_of_disabled_preset(self):
        lease = await self.acquire()
        effective = dict(self.adapter.values)
        del effective['uc_preset']
        with self.assertRaises(ServiceError) as error:
            await self.manager.verify_effective(self.adapter, lease, effective)
        self.assertEqual(error.exception.code, 'profile:drift')
        self.assertEqual(self.row()['phase'], 'conflict')
        with self.assertRaises(ServiceError):
            self.manager.mark_inflight(lease, digest('attempt'))

    async def test_error_after_atomic_replace_is_proven_and_does_not_duplicate_writes(self):
        def after(path, value):
            atomic_json(path, value)
            raise OSError()
        with patch('stylelora.generation.settings.atomic_json', side_effect=after):
            lease = await self.acquire()
            await self.manager.restore(self.adapter, lease)
        self.assertEqual(len(self.adapter.writes), 2)
        self.assertFalse(self.manager.status()['blocked'])

    async def test_clear_failure_keeps_fence_and_reconciliation_does_not_repeat_restore(self):
        lease = await self.acquire()
        def fail_clear(path, envelope):
            if envelope['content']['record'] is None:
                raise OSError()
            atomic_json(path, envelope)
        with patch('stylelora.generation.settings.atomic_json', side_effect=fail_clear), self.assertRaises(ServiceError):
            await self.manager.restore(self.adapter, lease)
        self.assertTrue(self.manager.status()['blocked'])
        self.assertEqual(self.adapter.values['steps'], 14)
        await self.manager.reconcile(self.adapter, lease)
        self.assertEqual(len(self.adapter.writes), 2)

    async def test_read_failure_does_not_apply_and_read_failure_after_apply_stays_blocked(self):
        async def failed():
            raise OSError('secret raw response')
        self.adapter.on_read = failed
        with self.assertRaises(ServiceError):
            await self.acquire()
        self.assertEqual(self.adapter.writes, [])
        self.assertFalse(self.manager.journal.path.exists())
        async def fail_second():
            if self.adapter.writes:
                raise OSError()
        self.adapter.on_read = fail_second
        with self.assertRaises(ServiceError):
            await self.acquire()
        self.assertTrue(self.manager.status()['blocked'])
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_only_allowlisted_scalar_settings_are_persisted_or_written(self):
        await self.acquire()
        raw = self.manager.journal.path.read_text(encoding='utf-8')
        for private in ('private prompt', 'private-token', 'readonly', 'do-not-write'):
            self.assertNotIn(private, raw)
            self.assertNotIn(private, json.dumps(self.manager.status()))
        self.assertNotIn('original', self.manager.status())
        self.assertNotIn('desired', self.manager.status())

    async def test_bad_changes_and_contracts_fail_before_network_read(self):
        for values in ({'prompt':'secret'}, {'steps':True}, {'steps':28.0}, {'quality_toggle':True},
                       {'uc_preset':1}, {'scale':float('nan')}, {'model':'https://host'}, {'steps':{}}, {}):
            with self.assertRaises(ServiceError):
                await self.acquire(values)
        self.assertEqual(self.adapter.reads, 0)
        self.adapter.identity = replace(self.adapter.identity, writable_keys=('steps','prompt'))
        with self.assertRaises(ServiceError):
            await self.acquire()
        self.assertEqual(self.adapter.writes, [])

    async def test_corrupt_duplicate_or_oversized_journal_is_fail_closed_and_untouched(self):
        await self.acquire()
        good = json.loads(self.manager.journal.path.read_bytes())
        payloads = [b'{bad', b'{"content":{},"content":{}}', b'x'*65537]
        for key, value in (('revision',True), ('schema_version',2), ('record',{})):
            state = {**deepcopy(good['content']), key:value}
            payloads.append(json.dumps({'content':state,'digest':digest(state)}).encode())
        for raw in payloads:
            self.manager.journal.path.write_bytes(raw)
            self.assertEqual(self.manager.status()['phase'], 'invalid')
            with self.assertRaises(ServiceError):
                await self.acquire()
            self.assertEqual(self.manager.journal.path.read_bytes(), raw)
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_deleted_live_journal_and_stale_handles_cannot_release_or_reacquire(self):
        lease = await self.acquire()
        with self.assertRaises(ServiceError):
            await self.manager.restore(self.adapter, str(uuid4()))
        self.manager.journal.path.unlink()
        self.assertEqual(self.manager.status()['reason'], 'profile:journal_changed')
        with self.assertRaises(ServiceError):
            await self.acquire()
        with self.assertRaises(ServiceError):
            await self.manager.restore(self.adapter, lease)
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_type_change_is_external_conflict_not_boolean_numeric_equivalence(self):
        lease = await self.acquire({'steps':1})
        self.adapter.values['steps'] = True
        with self.assertRaises(ServiceError) as error:
            await self.manager.restore(self.adapter, lease)
        self.assertEqual(error.exception.code, 'profile:restore_conflict')
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_requested_patch_is_frozen_before_waiting_for_original_snapshot(self):
        changes = {'steps':28}
        async def mutate_input():
            changes['steps'] = 90
            changes['prompt'] = 'must not be written'
        self.adapter.on_read = mutate_input
        lease = await self.acquire(changes)
        self.assertEqual(self.adapter.writes[0][0], {'steps':28})
        self.assertEqual(self.row()['desired']['steps'], 28)
        await self.manager.restore(self.adapter, lease)

    async def test_backend_restart_during_apply_leaves_unknown_without_restoring_new_instance(self):
        async def restart(values, key):
            self.adapter.values.update(values)
            self.adapter.identity = replace(self.adapter.identity, instance_id=digest('restarted'))
        self.adapter.on_write = restart
        with self.assertRaises(ServiceError) as error:
            await self.acquire()
        self.assertEqual(error.exception.code, 'profile:backend_changed')
        self.assertEqual(self.manager.status()['phase'], 'applying')
        with self.assertRaises(ServiceError):
            await self.manager.restore(self.adapter, self.row()['lease_id'])
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_success_reply_with_wrong_echo_keeps_expected_profile_and_blocks_dispatch(self):
        async def no_effect(values, key):
            self.adapter.settled[key] = True
        self.adapter.on_write = no_effect
        with self.assertRaises(ServiceError) as error:
            await self.acquire()
        self.assertEqual(error.exception.code, 'profile:drift')
        row = self.row()
        self.assertEqual(row['desired']['steps'], 28)
        self.assertEqual(row['phase'], 'conflict')
        await self.manager.restore(self.adapter, row['lease_id'])
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_probe_failure_and_truthy_nonboolean_evidence_never_release_fence(self):
        async def lost(values, key):
            raise OSError()
        self.adapter.on_write = lost
        with self.assertRaises(ServiceError):
            await self.acquire()
        row = self.row()
        self.adapter.settled[row['write_id']] = 1
        with self.assertRaises(ServiceError):
            await self.manager.reconcile(self.adapter, row['lease_id'])
        async def broken(key):
            raise OSError('secret server response')
        self.adapter.settings_write_settled = broken
        with self.assertRaises(ServiceError) as error:
            await self.manager.reconcile(self.adapter, row['lease_id'])
        self.assertEqual(error.exception.code, 'profile:read_failed')
        self.assertNotIn('secret', str(error.exception))
        self.assertTrue(self.manager.status()['blocked'])
        self.assertEqual(len(self.adapter.writes), 1)

    async def test_unsafe_storage_path_and_stale_journal_version_are_rejected(self):
        with patch('stylelora.generation.settings.safe_path', side_effect=ValueError('unsafe link')):
            self.assertEqual(self.manager.status()['phase'], 'invalid')
            with self.assertRaises(ServiceError):
                await self.acquire()
        old = self.manager.journal.load()
        await self.acquire()
        before = self.manager.journal.path.read_bytes()
        with self.assertRaises(ServiceError) as error:
            self.manager.journal.save(old, None)
        self.assertEqual(error.exception.code, 'profile:journal_changed')
        self.assertEqual(self.manager.journal.path.read_bytes(), before)

    async def test_external_edit_between_restore_read_and_write_is_not_overwritten(self):
        lease = await self.acquire()
        async def edit():
            self.adapter.values['steps'] = 99
        self.adapter.before_write = edit
        with self.assertRaises(ServiceError) as error:
            await self.manager.restore(self.adapter, lease)
        self.assertEqual(error.exception.code, 'profile:restore_conflict')
        self.assertEqual(self.adapter.values['steps'], 99)
        self.assertFalse(self.adapter.values['quality_toggle'])
        self.assertEqual(self.row()['phase'], 'conflict')
        self.assertIsNone(self.row()['write_id'])

    async def test_external_edit_between_apply_read_and_write_rejects_whole_patch(self):
        async def edit():
            self.adapter.values['steps'] = 99
        self.adapter.before_write = edit
        with self.assertRaises(ServiceError) as error:
            await self.acquire()
        self.assertEqual(error.exception.code, 'profile:restore_conflict')
        self.assertEqual(self.adapter.values['steps'], 99)
        self.assertTrue(self.adapter.values['quality_toggle'])
        self.assertEqual(self.adapter.values['uc_preset'], 3)
        self.assertEqual(self.row()['phase'], 'conflict')


class SettingsLeaseAPITests(APITest):
    def setUp(self):
        super().setUp()
        self.runtime.settings_leases = SettingsLeases(self.base/'recovery')

    def test_read_only_status_auth_query_and_no_network_or_clear_endpoint(self):
        self.runtime.backends.client_factory = lambda: self.fail('read contacted backend')
        path = '/api/backends/nai_launcher/settings-lease'
        first = self.request('GET', path)
        self.assertFalse(first['blocked'])
        self.assertFalse(first['recovery_adapter_available'])
        self.assertFalse(self.runtime.settings_leases.journal.path.exists())
        self.request('GET', path+'?force=true', status=422)
        self.request('GET', '/api/backends/arbitrary/settings-lease', status=404)
        self.request('POST', path, {}, status=405)
        self.assertEqual(self.client.get(path, headers={'Authorization':'Bearer bad'}).status_code, 401)

    def test_persisted_conflict_redacts_snapshots_and_blocks_source_gate(self):
        adapter = FakeSettings()
        manager = self.runtime.settings_leases
        lease = asyncio.run(manager.acquire(adapter, project_id=self.pid, source_id=1, cycle_id=str(uuid4()), changes=CHANGES))
        adapter.values['steps'] = 99
        with self.assertRaises(ServiceError):
            asyncio.run(manager.restore(adapter, lease))
        status = self.request('GET', '/api/backends/nai_launcher/settings-lease')
        self.assertEqual((status['blocked'],status['phase']), (True,'conflict'))
        self.assertNotIn('original', status)
        self.assertNotIn('desired', status)
        self.assertNotIn('current', status)
        self.assertNotIn('private', json.dumps(status))
        source = self.request('POST', self.p+'/sources', draft(1), status=201)
        from types import SimpleNamespace
        self.runtime.generation_sources.driver = FakeDriver()
        with self.assertRaises(ServiceError) as error:
            self.runtime.generation_sources._gate(SimpleNamespace(config_json=source['config_json'], negative_prompt=''))
        self.assertEqual(error.exception.code, 'profile:restore_pending')

    def test_invalid_journal_does_not_break_project_and_is_reported_without_overwrite(self):
        path = self.runtime.settings_leases.journal.path
        path.parent.mkdir(parents=True)
        path.write_bytes(b'bad file contains secret')
        view = self.request('GET', '/api/backends/nai_launcher/settings-lease')
        self.assertTrue(view['blocked'])
        self.assertEqual(view['reason'], 'profile:invalid_journal')
        self.assertNotIn('secret', json.dumps(view))
        self.assertEqual(self.request('GET', '/api/projects/current')['project_id'], self.pid)
        self.assertEqual(path.read_bytes(), b'bad file contains secret')
