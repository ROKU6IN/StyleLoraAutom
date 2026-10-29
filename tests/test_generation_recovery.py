import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import threading
from unittest.mock import patch
from uuid import uuid4

from stylelora.generation.operations import path_for, read_operation, write_operation
from stylelora.generation.runner import SerialDriver
from stylelora.generation.settings import SettingsLeases
from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.repositories import sources
from stylelora.project_db.types import ItemSeed
from .api_support import APITest
from .generation_support import FakeCycleAdapter
from .test_generation_handoff import draft


class RecoveryTests(APITest):
    def setUp(self):
        super().setUp()
        self.context, self.project = self.runtime.context, self.runtime.context.handle
        self.runtime.settings_leases = SettingsLeases(self.base/'recovery')
        self.adapter = FakeCycleAdapter()
        self.driver = SerialDriver(self.runtime, self.adapter)
        self.runtime.generation_sources.driver = self.driver
        source = self.request('POST', self.p+'/sources', draft(2), status=201)
        self.sid = source['source_id']
        self.path = self.p+f'/sources/{self.sid}/recovery'
        self.write(commands.start_source, self.sid, [ItemSeed(1,'forest'), ItemSeed(2,'river')])
        with read_snapshot(self.project) as db:
            self.ids = [r[0] for r in db.execute('SELECT item_id FROM source_items WHERE source_id=? ORDER BY seq', (self.sid,))]

    def write(self, fn, *args, **kwargs):
        return submit_write(self.project, lambda db: fn(db,*args,**kwargs)).result()

    def item(self):
        with read_snapshot(self.project) as db:
            return sources.get_item(db,self.ids[0])

    def view(self, index=0):
        return self.request('GET', self.path+f'/{self.ids[index]}')

    def lost(self, *, preparation=False):
        async def submit(row, grant):
            self.adapter.complete(row,grant)
            raise ConnectionError('fixture private response')
        async def prepare(row):
            raise ConnectionError('fixture preparation response')
        if preparation:
            self.adapter.prepare_hook = prepare
        else:
            self.adapter.submit_hook = submit
        self.driver.dispatch(self.context,self.sid); self.driver.wait()

    def action(self, action='reconcile', *, value=None, status=202, key=None):
        row = value or self.view()
        return self.request('POST',self.path, {'item_id':row['item_id'], 'action':action, 'confirm_restore':action=='restore'},
                            version=row['version'],status=status,key=key)

    def reconcile(self):
        return self.wait_operation(self.action())

    def test_http_probe_then_explicit_restore_without_resubmit(self):
        self.lost()
        before = len(self.adapter.writes)
        self.assertTrue(self.view()['can_reconcile'])
        self.reconcile()
        self.assertEqual(self.item().status,'done')
        self.assertEqual(len(self.adapter.writes),before)
        self.assertTrue(self.view()['can_restore'])
        self.wait_operation(self.action('restore'))
        self.assertEqual(self.view()['phase'],'settled')
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])
        self.assertEqual(len(self.adapter.submitted),1)
        self.assertEqual(self.request('GET', self.p+f'/sources/{self.sid}')['status'],'paused')

    def test_get_is_local_readonly_and_redacts_evidence(self):
        self.lost()
        reads = self.adapter.reads
        record = path_for(self.project,self.ids[0],1).read_bytes()
        for _ in range(3):
            data = self.request('GET',self.path)
        self.assertEqual(self.adapter.reads,reads)
        self.assertEqual(self.adapter.queried,[])
        self.assertEqual(path_for(self.project,self.ids[0],1).read_bytes(),record)
        payload = json.dumps(data)
        row = read_operation(self.project,self.ids[0],1)
        for forbidden in ('forest','geometric style','private-token',str(self.root),row['operation_key'],row['instance_id'],row['lease_id']):
            self.assertNotIn(forbidden,payload)

    def test_signed_paging_filter_and_cross_source_cursor(self):
        first = self.request('GET',self.path+'?limit=1')
        second = self.request('GET',self.path+'?limit=1&cursor='+first['next_cursor'])
        self.assertEqual([first['items'][0]['item_id'],second['items'][0]['item_id']],self.ids)
        self.assertFalse(second['has_more'])
        self.request('GET',self.path+'?status=pending&cursor='+first['next_cursor'],status=422)
        self.request('GET',self.path+'?limit=1001',status=422)
        self.request('GET',self.path+'?status=unknown',status=422)
        self.request('GET',self.path+'?force=true',status=422)

    def test_auth_and_version_and_explicit_confirmation_required(self):
        self.lost()
        body = {'item_id':self.ids[0], 'action':'reconcile'}
        self.request('POST',self.path,body,status=428)
        self.request('POST',self.path,body,version='old',status=409)
        self.assertEqual(self.client.get(self.path,headers={'Authorization':'Bearer wrong'}).status_code,401)
        self.reconcile()
        row = self.view()
        self.request('POST',self.path,{'item_id':self.ids[0],'action':'restore'},version=row['version'],status=422)
        self.request('POST',self.path,{'item_id':self.ids[0],'action':'restore','confirm_restore':1},version=row['version'],status=422)

    def test_same_key_replays_operation_without_second_probe(self):
        self.lost()
        row,key = self.view(),str(uuid4())
        first = self.action(value=row,key=key)
        self.wait_operation(first)
        again = self.action(value=row,key=key)
        self.assertEqual(first['task'],again['task'])
        self.assertEqual(len(self.adapter.queried),1)
        self.action(value=row,status=409)

    def test_closed_or_wrong_project_and_source_are_rejected(self):
        self.lost()
        other = self.request('POST',self.p+'/sources',draft(1),status=201)
        self.request('GET',self.p+f"/sources/{other['source_id']}/recovery/{self.ids[0]}",status=404)
        self.request('GET',self.path.replace(self.pid,str(uuid4())),status=410)
        row = self.view()
        self.runtime._close_current()
        self.action(value=row,status=410)

    def test_production_unavailable_disables_action_without_touching_records(self):
        self.lost()
        self.runtime.generation_sources.driver = None
        row = self.view()
        self.assertFalse(row['can_reconcile'])
        self.assertEqual(row['reason'],'generation:adapter_unavailable')
        self.action(value=row,status=409)
        self.assertEqual(self.adapter.queried,[])

    def test_backend_replacement_invalidates_version(self):
        self.lost()
        row = self.view()
        self.adapter.identity = replace(self.adapter.identity,instance_id='b'*64)
        self.action(value=row,status=409)
        self.assertEqual(self.view()['reason'],'profile:backend_changed')
        self.assertEqual(self.adapter.queried,[])

    def test_damaged_operation_and_journal_stay_blocked(self):
        self.lost()
        path = path_for(self.project,self.ids[0],1)
        original = path.read_bytes()
        path.write_text('{}')
        self.assertEqual(self.view()['reason'],'generation:invalid_operation')
        self.action(status=409)
        path.write_bytes(original)
        self.runtime.settings_leases.journal.path.write_text('{}')
        self.assertEqual(self.view()['phase'],'blocked')
        self.assertEqual(self.adapter.queried,[])

    def test_missing_cost_or_operation_never_assumed_unexecuted(self):
        self.lost()
        cost = self.root/f'images/.costs/{self.ids[0]}/1.json'
        cost.unlink()
        self.assertEqual(self.view()['reason'],'recovery:cost_evidence_missing')
        path_for(self.project,self.ids[0],1).unlink()
        self.assertEqual(self.view()['reason'],'recovery:operation_missing')
        self.assertEqual(self.item().status,'sent')

    def test_foreign_lease_and_changed_request_are_blocked(self):
        self.lost()
        original = read_operation(self.project,self.ids[0],1)
        contradictory = {**original,'closed':True}
        write_operation(self.project,contradictory,original)
        self.assertFalse(self.view()['can_restore'])
        self.assertEqual(self.view()['reason'],'recovery:other_inflight')
        write_operation(self.project,original,contradictory)
        changed = {**original,'lease_id':str(uuid4())}
        write_operation(self.project,changed,original)
        self.assertEqual(self.view()['reason'],'recovery:lease_unbound')
        write_operation(self.project,{**changed,'request_digest':'b'*64},changed)
        self.assertEqual(self.view()['reason'],'generation:evidence_mismatch')

    def test_unknown_prepare_requires_readonly_terminal_proof(self):
        self.lost(preparation=True)
        self.assertTrue(self.view()['can_reconcile'])
        self.wait_operation(self.action(),expected='failed')
        self.assertEqual(self.item().attempts,0)
        row = self.adapter.prepared[0]
        self.adapter.results[row['operation_key']] = self.adapter.outcome(row,None,'canceled')
        self.adapter.generations[row['operation_key']] = True
        self.reconcile()
        self.wait_operation(self.action('restore'))
        self.assertFalse(self.runtime.settings_leases.status()['blocked'])
        self.assertEqual(self.adapter.submitted,[])
        self.assertEqual(self.adapter.canceled,[])

    def test_restore_conflict_preserves_external_values(self):
        self.lost(); self.reconcile()
        self.adapter.values['steps'] = 99
        result = self.wait_operation(self.action('restore'),expected='failed')
        self.assertEqual(result['error']['code'],'profile:restore_conflict')
        self.assertEqual(self.adapter.values['steps'],99)
        self.assertTrue(self.runtime.settings_leases.status()['blocked'])

    def test_restore_response_loss_requires_reconcile_before_another_write(self):
        self.lost(); self.reconcile()
        original = self.adapter.update_settings
        async def unknown(*args,**kwargs):
            await original(*args,**kwargs)
            raise ConnectionError()
        self.adapter.update_settings = unknown
        self.wait_operation(self.action('restore'),expected='failed')
        self.assertFalse(self.view()['can_restore'])
        self.assertTrue(self.view()['can_reconcile'])
        count = len(self.adapter.writes)
        self.reconcile()
        self.assertEqual(len(self.adapter.writes),count)
        self.assertEqual(self.view()['phase'],'settled')

    def test_active_probe_blocks_other_actions_and_user_cancel(self):
        self.lost()
        entered,release = threading.Event(),threading.Event()
        original = self.adapter.probe
        async def blocked(*args):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.005)
            return await original(*args)
        self.adapter.probe = blocked
        try:
            accepted = self.action()
            self.assertTrue(entered.wait(3))
            self.assertTrue(self.view()['busy'])
            self.action(status=409)
            operation = self.request('GET',accepted['status_url'])
            self.request('POST',accepted['status_url']+'/cancel',{},version=operation['version'],status=409)
        finally:
            release.set()
        self.wait_operation(accepted)
        self.assertEqual(len(self.adapter.queried),1)

    def test_queue_rechecks_evidence_before_remote_io(self):
        self.lost()
        enter,release = threading.Event(),threading.Event()
        def hold(task):
            enter.set(); release.wait(5)
        blockers = [self.runtime.supervisor.submit('fixture',hold) for _ in range(2)]
        self.assertTrue(enter.wait(2))
        accepted = self.action()
        row = read_operation(self.project,self.ids[0],1)
        write_operation(self.project,{**row,'request_digest':'b'*64},row)
        release.set()
        result = self.wait_operation(accepted,expected='failed')
        self.assertEqual(result['error']['code'],'concurrency:version_conflict')
        self.assertEqual(self.adapter.queried,[])

    def test_queue_admission_failure_releases_recovery_reservation(self):
        self.lost()
        with patch.object(self.runtime.supervisor,'submit',side_effect=RuntimeError('fixture')):
            self.action(status=500)
        self.assertIsNone(self.runtime.generation_recovery.pending)
        self.assertTrue(self.view()['can_reconcile'])

    def test_close_while_probe_active_drains_without_restore_or_submit(self):
        self.lost()
        entered,release = threading.Event(),threading.Event()
        original = self.adapter.probe
        async def blocked(*args):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.005)
            return await original(*args)
        self.adapter.probe = blocked
        accepted = self.action()
        self.assertTrue(entered.wait(3))
        thread = threading.Thread(target=self.runtime._close_current)
        thread.start()
        release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.adapter.submitted),1)
        self.assertEqual(len(self.adapter.writes),1)
        self.assertEqual(self.context.users,0)

    def test_reopen_rebinds_handle_but_does_not_restore_consent(self):
        self.lost()
        old = self.view()
        self.runtime._close_current(); self.runtime.open_now(self.root)
        self.context,self.project = self.runtime.context,self.runtime.context.handle
        self.pid = self.context.project_id
        self.p = '/api/projects/'+self.pid
        self.path = self.p+f'/sources/{self.sid}/recovery'
        self.assertNotEqual(old['version'],self.view()['version'])
        self.assertIsNone(commands.cost_cycle_id(self.project,self.sid))
        self.reconcile()
        self.wait_operation(self.action('restore'))
        self.assertEqual(len(self.adapter.submitted),1)

    def test_completed_operation_replay_does_not_restore_twice(self):
        self.lost(); self.reconcile()
        row,key = self.view(),str(uuid4())
        first = self.action('restore',value=row,key=key)
        self.wait_operation(first)
        count = len(self.adapter.writes)
        replay = self.action('restore',value=row,key=key)
        self.assertEqual(first['task'],replay['task'])
        self.assertEqual(len(self.adapter.writes),count)

    def test_running_source_requires_explicit_pause(self):
        self.lost(preparation=True)
        self.write(commands.transition_source,self.sid,expected='paused',target='running',reason='fixture')
        self.assertEqual(self.view()['reason'],'recovery:pause_required')
        self.action(status=409)

    def test_unknown_legacy_sent_has_diagnostic_but_no_force_action(self):
        self.write(commands.mark_sent,self.ids[0])
        self.write(commands.transition_source,self.sid,expected='running',target='paused',reason='fixture')
        row = self.view()
        self.assertFalse(row['can_reconcile'])
        self.assertFalse(row['can_restore'])
        self.assertEqual(row['reason'],'recovery:operation_missing')
        self.request('POST',self.path,{'item_id':self.ids[0],'action':'force'},version=row['version'],status=422)
