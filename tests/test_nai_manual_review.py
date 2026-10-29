import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import httpx

from stylelora.backends.nai_direct.client import NaiDirectBackend
from stylelora.generation import manual_review
from stylelora.generation.direct import journal_path
from stylelora.project_db.storage import atomic_json
from stylelora.project_db.validation import digest
from stylelora.runtime.errors import ServiceError
from .api_support import APITest
from . import test_nai_direct as direct


CONFIRM = {'confirm_stop_source': True, 'acknowledge_unknown_charge': True}


class ManualReviewTests(APITest):
    create = direct.DirectGenerationTests.create
    item = direct.DirectGenerationTests.item
    view = direct.DirectGenerationTests.view
    cycle = direct.DirectGenerationTests.cycle
    control = direct.DirectGenerationTests.control
    fail_at = direct.DirectGenerationTests.fail_at

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'STYLELORA_NAI_TOKEN': 'fixture-token'})
        env.start()
        self.addCleanup(env.stop)
        from stylelora.tagging.library import CatalogSnapshot
        self.runtime.catalog.state['catalog'] = {'complete': True, 'tags': [{'tag': 'forest', 'aliases': [], 'category': 'general'}]}
        self.runtime.catalog._snapshot = CatalogSnapshot(self.runtime.catalog.state)
        self.service = self.runtime.nai_direct
        self.driver = self.service.driver
        self.project, self.context = self.runtime.context.handle, self.runtime.context
        self.calls = []
        self.success = False
        def respond(request):
            self.calls.append(request)
            if not self.success:
                raise httpx.ReadTimeout('fixture lost response', request=request)
            return httpx.Response(201, content=direct.zipped(direct.png_bytes(json.loads(request.content))),
                                  headers={'content-type': 'application/zip'})
        self.service.backend = NaiDirectBackend(transport=httpx.MockTransport(respond))

    def unknown(self):
        self.create(count=2)
        self.cycle()
        self.assertEqual(self.view()['status'], 'paused')
        self.assertEqual(self.item().status, 'sent')

    def review(self):
        return self.request('GET', self.path+'/unknown-disposition')

    def stop(self, **kwargs):
        return self.request('POST', self.path+'/unknown-disposition', CONFIRM,
                            version=kwargs.pop('version', self.review()['version']), **kwargs)

    def evidence(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in (self.root/'images').rglob('*')
                if p.is_file() and 'reviews' not in p.parts}

    def assert_untouched(self, before):
        self.assertEqual(self.evidence(), before)
        self.assertEqual((self.item().status, self.item().attempts, self.item().image_id), ('sent', 1, None))
        self.assertEqual((self.item(2).status, self.item(2).attempts), ('pending', 0))
        self.assertEqual(len(self.calls), 1)

    def reopen(self):
        self.project._fault_hook = None
        self.runtime._close_current()
        self.pid = self.runtime.open_now(self.root)['project_id']
        self.p = '/api/projects/'+self.pid
        self.path = self.p+f'/sources/{self.sid}'
        self.project, self.context = self.runtime.context.handle, self.runtime.context

    def test_stop_preserves_attempts_cost_and_receipts_without_token_or_network(self):
        self.unknown()
        before = self.evidence()
        with patch.object(self.service, 'token', side_effect=AssertionError('must not read credentials')):
            value = self.review()
            self.assertEqual((value['unknown_count'], value['pending_count']), (1, 1))
            self.assertTrue(value['can_stop'])
            result = self.stop()
        self.assertEqual(result['source_status'], 'canceled')
        self.assertEqual(result['decision']['phase'], 'completed')
        self.assertEqual(result['upstream_outcome'], 'unknown')
        self.assertFalse(result['can_stop'])
        self.assert_untouched(before)
        self.control('resume', status=409)
        self.assertEqual(len(self.calls), 1)

    def test_get_never_writes_a_decision(self):
        self.unknown()
        before = self.evidence()
        for _ in range(3):
            self.assertTrue(self.review()['can_stop'])
        self.assertFalse(manual_review.record_path(self.project, self.sid).exists())
        self.assertEqual(self.view()['status'], 'paused')
        self.assert_untouched(before)

    def test_both_confirmations_are_required_strict_booleans(self):
        self.unknown()
        for body in ({}, {'confirm_stop_source': True}, {**CONFIRM, 'confirm_stop_source': False},
                     {**CONFIRM, 'acknowledge_unknown_charge': False},
                     {**CONFIRM, 'confirm_stop_source': 1}, {**CONFIRM, 'acknowledge_unknown_charge': 'true'},
                     {**CONFIRM, 'force_failed': True}):
            with self.subTest(body=body):
                self.request('POST', self.path+'/unknown-disposition', body, version=self.review()['version'], status=422)
        self.assertIsNone(self.review()['decision'])
        self.assertEqual(self.view()['status'], 'paused')

    def test_requires_current_version_and_if_match(self):
        self.unknown()
        self.request('POST', self.path+'/unknown-disposition', CONFIRM, status=428)
        self.stop(version='0'*64, status=409)
        self.assertIsNone(self.review()['decision'])

    def test_authentication_query_and_project_scope(self):
        self.unknown()
        self.request('GET', self.path+'/unknown-disposition', headers={'Authorization': 'Bearer wrong'}, status=401)
        self.request('GET', self.path+'/unknown-disposition?force=true', status=422)
        self.request('GET', '/api/projects/'+str(uuid4())+'/sources/1/unknown-disposition', status=410)
        self.request('GET', self.p+'/sources/999/unknown-disposition', status=404)

    def test_draft_and_completed_source_cannot_stop(self):
        self.create(start=False)
        self.assertFalse(self.review()['can_stop'])
        self.stop(status=409)
        self.success = True
        self.create()
        self.cycle()
        self.assertFalse(self.review()['can_stop'])
        self.stop(status=409)

    def test_launcher_source_is_rejected(self):
        source = self.request('POST', self.p+'/sources', direct.handoff.draft(1), status=201)
        self.request('GET', self.p+f'/sources/{source["source_id"]}/unknown-disposition', status=422)

    def test_complete_response_requires_existing_reconcile_path(self):
        self.success = True
        self.create()
        self.fail_at('nai_after_receipt')
        self.cycle()
        self.project._fault_hook = None
        self.assertEqual(self.review()['reason'], 'nai:local_response_available')
        self.stop(status=409)
        value = self.request('GET', self.path+f'/recovery/{self.item().item_id}')
        self.assertTrue(value['can_reconcile'])

    def test_missing_receipt_blocks_instead_of_inferring_absence(self):
        self.unknown()
        journal_path(self.project, self.item().item_id, 1).unlink()
        self.assertEqual(self.review()['reason'], 'nai:manual_review_evidence_missing')
        self.stop(status=409)
        self.assertEqual(self.item().status, 'sent')
        self.assertEqual(len(self.calls), 1)

    def test_corrupt_receipt_is_preserved(self):
        self.unknown()
        path = journal_path(self.project, self.item().item_id, 1)
        path.write_bytes(b'{broken')
        self.assertEqual(self.review()['reason'], 'nai:invalid_receipt')
        self.stop(status=409)
        self.assertEqual(path.read_bytes(), b'{broken')

    def test_busy_driver_and_recovery_reservation_block_action(self):
        self.unknown()
        version = self.review()['version']
        with patch.object(self.driver, 'recovery_busy', return_value=True):
            self.assertEqual(self.review()['reason'], 'recovery:busy')
            self.stop(version=version, status=409)
            self.stop(status=409)
        with patch.object(self.runtime.generation_recovery, 'pending', object()):
            self.assertFalse(self.review()['can_stop'])
            self.stop(status=409)
        self.assertIsNone(self.review()['decision'])

    def test_idempotent_replay_and_new_key_do_not_repeat_changes(self):
        self.unknown()
        value = self.review()
        key = str(uuid4())
        first = self.stop(version=value['version'], key=key)
        second = self.stop(version=value['version'], key=key)
        third = self.stop()
        self.assertEqual(first, second)
        self.assertEqual(first['decision'], third['decision'])
        self.assertEqual(len(self.calls), 1)

    def test_completed_record_survives_reopen(self):
        self.unknown()
        first = self.stop()
        before = self.evidence()
        self.reopen()
        second = self.review()
        self.assertEqual(second['decision'], first['decision'])
        self.assertEqual(second['source_status'], 'canceled')
        self.assert_untouched(before)

    def recover_interruption(self, phase, expected_status, expected_phase):
        self.unknown()
        before = self.evidence()
        self.fail_at(phase)
        result = self.stop(status=503)
        self.assertEqual(result['error']['outcome'], 'unknown')
        self.project._fault_hook = None
        state = self.review()
        self.assertEqual(state['source_status'], expected_status)
        self.assertEqual(state['decision']['phase'], expected_phase)
        self.reopen()
        fresh = self.review()
        self.assertEqual(fresh['decision'], state['decision'])
        result = self.stop()
        self.assertEqual(result['decision']['decision_id'], state['decision']['decision_id'])
        self.assertEqual(result['decision']['phase'], 'completed')
        self.assert_untouched(before)

    def test_interruption_after_prepare_needs_explicit_completion(self):
        self.recover_interruption('nai_review_after_prepare', 'paused', 'prepared')

    def test_interruption_after_cancel_needs_explicit_completion(self):
        self.recover_interruption('nai_review_after_cancel', 'canceled', 'prepared')

    def test_lost_confirmation_after_complete_is_readable(self):
        self.recover_interruption('nai_review_after_complete', 'canceled', 'completed')

    def test_atomic_replace_succeeds_then_raises_is_recoverable(self):
        self.unknown()
        original = manual_review.atomic_json
        def lost(path, value):
            original(path, value)
            raise OSError('lost durable confirmation')
        with patch.object(manual_review, 'atomic_json', side_effect=lost):
            self.stop(status=503)
        self.assertEqual(self.review()['decision']['phase'], 'prepared')
        self.assertEqual(self.view()['status'], 'paused')
        self.assertEqual(self.stop()['decision']['phase'], 'completed')
        self.assertEqual(len(self.calls), 1)

    def test_write_failure_does_not_cancel_source(self):
        self.unknown()
        with patch.object(manual_review, 'atomic_json', side_effect=OSError('disk full')):
            self.stop(status=503)
        self.assertIsNone(self.review()['decision'])
        self.assertEqual(self.view()['status'], 'paused')

    def test_corrupt_review_record_blocks_and_is_not_replaced(self):
        self.unknown()
        path = manual_review.record_path(self.project, self.sid)
        path.parent.mkdir(parents=True)
        for raw in (b'broken', b'x'*8193, b'{"content":{},"digest":"bad"}'):
            with self.subTest(size=len(raw)):
                path.write_bytes(raw)
                self.assertEqual(self.review()['reason'], 'nai:manual_review_invalid')
                self.stop(status=409)
                self.assertEqual(path.read_bytes(), raw)

    def test_prepared_basis_change_is_not_silently_accepted(self):
        self.unknown()
        self.fail_at('nai_review_after_prepare')
        self.stop(status=503)
        self.project._fault_hook = None
        path = manual_review.record_path(self.project, self.sid)
        row = manual_review.read_record(self.project, self.sid)
        row['basis_digest'] = '0'*64
        atomic_json(path, {'content': row, 'digest': digest(row)})
        self.assertEqual(self.review()['reason'], 'nai:manual_review_changed')
        self.stop(status=409)
        self.assertEqual(self.view()['status'], 'paused')

    def test_evidence_change_after_prepare_blocks_database_transition(self):
        self.unknown()
        def change(name):
            if name == 'nai_review_after_prepare':
                journal_path(self.project, self.item().item_id, 1).write_bytes(b'changed')
        self.project._fault_hook = change
        self.stop(status=503)
        self.project._fault_hook = None
        self.assertEqual(self.view()['status'], 'paused')
        self.assertEqual(self.review()['reason'], 'nai:invalid_receipt')
        self.assertEqual(len(self.calls), 1)

    def test_closing_project_cannot_begin_decision(self):
        self.unknown()
        version = self.review()['version']
        with patch.object(self.runtime, 'draining', True):
            with self.assertRaises(ServiceError) as raised:
                manual_review.stop(self.runtime, self.context, self.sid, version)
        self.assertEqual(raised.exception.code, 'project:closing')
        self.assertIsNone(self.review()['decision'])

    def test_concurrent_decisions_commit_only_one_record(self):
        self.unknown()
        version = self.review()['version']
        def apply():
            try:
                return manual_review.stop(self.runtime, self.context, self.sid, version)
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: apply(), range(2)))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(self.review()['decision']['phase'], 'completed')
        self.assertEqual(len(self.calls), 1)

    def test_unclaimed_saved_image_is_preserved_without_adoption(self):
        self.success = True
        self.create(count=2)
        self.fail_at('nai_after_save')
        self.cycle()
        self.project._fault_hook = None
        before = self.evidence()
        self.assertTrue(any(name.endswith('.png') for name in before))
        self.stop()
        self.assert_untouched(before)

    def test_completed_basis_change_is_not_reported_as_settled(self):
        self.unknown()
        self.stop()
        path = manual_review.record_path(self.project, self.sid)
        row = manual_review.read_record(self.project, self.sid)
        row['basis_digest'] = 'f'*64
        atomic_json(path, {'content': row, 'digest': digest(row)})
        self.assertEqual(self.review()['reason'], 'nai:manual_review_changed')
        self.assertEqual(self.review()['upstream_outcome'], 'unknown')
        self.stop(status=409)

    def test_record_schema_is_checked_even_with_a_valid_digest(self):
        self.unknown()
        self.stop()
        path = manual_review.record_path(self.project, self.sid)
        original = manual_review.read_record(self.project, self.sid)
        for key, value in (('source_id', self.sid+1), ('schema_version', True), ('unknown_count', 0),
                           ('pending_count', -1), ('action', 'force_failed'), ('phase', 'settled'),
                           ('decision_id', 'bad'), ('created_at', 'bad')):
            with self.subTest(field=key):
                row = {**original, key: value}
                atomic_json(path, {'content': row, 'digest': digest(row)})
                self.assertEqual(self.review()['reason'], 'nai:manual_review_invalid')
                self.stop(status=409)
