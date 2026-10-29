from dataclasses import replace

from stylelora.project_db import commands
from stylelora.project_db.errors import FileMissing, LockedSource, SeedConflict, StateConflict, ValidationError
from stylelora.project_db.repositories import images, jobs, sources
from stylelora.project_db.types import ItemSeed, JobRequest, JobSnapshot, Scope, SourcePatch
from stylelora.project_db.validation import utc_now
from .support import DatabaseTest


class StateMachineTests(DatabaseTest):
    def test_S01_all_source_edges(self):
        states = ("draft", "running", "paused", "done", "canceled")
        expected_edges = {
            ("running", "paused"), ("paused", "running"), ("running", "done"), ("paused", "done"),
            ("running", "canceled"), ("paused", "canceled"), ("draft", "canceled"),
        }
        for before in states:
            for after in states:
                with self.subTest(before=before, after=after):
                    source, _ = self.source()
                    now = utc_now()
                    self.execute("UPDATE source_items SET status='failed',attempts=1 WHERE source_id=?", (source.source_id,))
                    self.execute("UPDATE sources SET status=?,locked=?,started_at=?,finished_at=? WHERE source_id=?",
                                 (before, int(before != "draft"), now if before != "draft" else None,
                                  now if before in ("done", "canceled") else None, source.source_id))
                    if before == after or (before, after) in expected_edges:
                        result = self.write(commands.transition_source, source.source_id, expected=before,
                                            target=after, reason="fixture")
                        self.assertEqual(result.value.status, after)
                        self.assertEqual(result.changed, before != after)
                    else:
                        with self.assertRaises(StateConflict):
                            self.write(commands.transition_source, source.source_id, expected=before,
                                       target=after, reason="fixture")

    def test_S01_all_job_edges(self):
        states = ("pending", "running", "paused", "done", "failed", "canceled")
        allowed = {
            ("pending", "running"),
            ("running", "paused"), ("paused", "running"), ("running", "done"), ("running", "failed"),
            ("running", "canceled"), ("pending", "canceled"), ("paused", "canceled"),
            ("pending", "failed"), ("paused", "done"), ("paused", "failed"),
        }
        for index, (before, after) in enumerate((a, b) for a in states for b in states):
            with self.subTest(before=before, after=after):
                job = self.write(jobs.create_batch, f"matrix-{index}",
                                 (JobRequest("wd", "selected", Scope(selected_ids=())),))[0]
                # Valid frozen, empty jobs isolate status edges from unresolved outcomes.
                self.write(jobs.freeze_job, job.job_id, JobSnapshot('fixture'), image_ids=(), start=False)
                now = utc_now()
                self.execute("UPDATE jobs SET status=?,started_at=?,finished_at=?,error=? WHERE job_id=?",
                             (before, None if before == "pending" else now,
                              now if before in ("done", "failed", "canceled") else None,
                              "fixture" if before == "failed" else None, job.job_id))
                if before == after or (before, after) in allowed:
                    result = self.write(commands.transition_job, job.job_id, expected=before, target=after,
                                        error="fixture" if after == "failed" else None)
                    self.assertEqual(result.value.status, after)
                else:
                    with self.assertRaises(StateConflict):
                        self.write(commands.transition_job, job.job_id, expected=before, target=after,
                                   error="fixture" if after == "failed" else None)

    def test_S01_pending_requires_snapshot_and_preserves_it_until_start(self):
        job = self.write(jobs.create_batch, 'staged-freeze',
                         (JobRequest('wd', 'selected', Scope(selected_ids=())),))[0]
        with self.assertRaises(StateConflict):
            self.write(commands.transition_job, job.job_id, expected='pending', target='running')
        frozen = self.write(jobs.freeze_job, job.job_id, JobSnapshot('fixture'), image_ids=(), start=False).value
        self.assertEqual(frozen.status, 'pending')
        self.assertIsNone(frozen.started_at)
        self.assertNotIn(job.job_id, self.project._job_generations)
        from stylelora.project_db.errors import SnapshotMismatch
        with self.assertRaises(SnapshotMismatch):
            self.write(jobs.freeze_job, job.job_id, JobSnapshot('different'), image_ids=(), start=False)
        started = self.write(commands.transition_job, job.job_id, expected='pending', target='running').value
        self.assertEqual(started.config_snapshot_json, frozen.config_snapshot_json)
        self.assertIsNotNone(started.started_at)
        self.assertIn(job.job_id, self.project._job_generations)

    def test_S01_review_edges_and_same_value(self):
        image = self.image()
        for before in ("unreviewed", "keep", "reject"):
            for after in ("unreviewed", "keep", "reject"):
                self.execute("UPDATE images SET review_status=? WHERE image_id=?", (before, image.image_id))
                result = self.write(images.set_review, image.image_id, expected=before, target=after)
                self.assertEqual(result.value.review_status, after)
                self.assertEqual(result.changed, before != after)
        self.assertTrue((self.root / "images" / image.filename).is_file())

    def test_S02_start_atomic_seeds_replay_and_lock(self):
        source, seeds = self.source(2, start=False)
        with self.assertRaises(ValidationError):
            self.write(commands.start_source, source.source_id, (seeds[0], replace(seeds[1], seq=1)))
        self.assertEqual(self.sql("SELECT count(*) FROM source_items"), ((0,),))
        self.assertEqual(self.read(sources.get_source, source.source_id).status, "draft")
        self.write(commands.start_source, source.source_id, seeds)
        ids = self.sql("SELECT item_id FROM source_items ORDER BY item_id")
        self.write(commands.start_source, source.source_id, seeds)
        self.assertEqual(ids, self.sql("SELECT item_id FROM source_items ORDER BY item_id"))
        with self.assertRaises(SeedConflict):
            self.write(commands.start_source, source.source_id, (replace(seeds[0], prompt="other"), seeds[1]))
        for patch in (SourcePatch(config={}), SourcePatch(type="import"), SourcePatch(target_count=3),
                      SourcePatch(style_prompt="x"), SourcePatch(negative_prompt="x")):
            with self.assertRaises(LockedSource):
                self.write(sources.update_source, source.source_id, patch)
        self.assertTrue(self.write(sources.update_source, source.source_id, SourcePatch(name="renamed")).changed)

    def test_S02_invalid_start_snapshots_and_secret_rejection(self):
        source, seeds = self.source(start=False)
        for mutation in (
            {"default_aspect": "invalid"}, {"sizes": {}}, {"schema_version": 99},
            {"profile": {**source.config_json["profile"], "quality_toggle": True}},
            {"profile": {**source.config_json["profile"], "steps": True}},
        ):
            config = {**source.config_json, **mutation}
            self.write(sources.update_source, source.source_id, SourcePatch(config=config))
            with self.assertRaises(ValidationError):
                self.write(commands.start_source, source.source_id, seeds)
        for config in ({"endpoint": "http://localhost"}, {"nested": {"token": "secret"}},
                       {"import_root": "C:/images"}, {"path": "C:/images"}):
            with self.assertRaises(ValidationError):
                self.write(sources.update_source, source.source_id, SourcePatch(config=config))

    def test_S03_deny_ask_allow_and_no_attempt_increment(self):
        for policy in ("deny", "ask", "allow"):
            source, _ = self.source(cost_policy=policy)
            self.assertEqual(commands.evaluate_cost(self.project, source.source_id, estimated_anlas=0), "submit")
            outcome = commands.evaluate_cost(self.project, source.source_id, estimated_anlas=3)
            self.assertEqual(outcome, "submit" if policy == "allow" else "paused")
            self.assertEqual(self.item(source.source_id).attempts, 0)
            token = commands.cost_cycle_id(self.project, source.source_id)
            if policy == "deny":
                with self.assertRaises(StateConflict):
                    commands.confirm_cost_and_resume(self.project, source.source_id, expected_cycle_id=token)
            if policy == "ask":
                commands.confirm_cost_and_resume(self.project, source.source_id, expected_cycle_id=token)
                commands.confirm_cost_and_resume(self.project, source.source_id, expected_cycle_id=token)
                self.assertEqual(commands.evaluate_cost(self.project, source.source_id, estimated_anlas=5), "submit")

    def test_S03a_cycles_pause_resume_and_stale_confirmations(self):
        source, _ = self.source()
        commands.evaluate_cost(self.project, source.source_id, estimated_anlas=1)
        token = commands.cost_cycle_id(self.project, source.source_id)
        commands.confirm_cost_and_resume(self.project, source.source_id, expected_cycle_id=token)
        self.write(commands.transition_source, source.source_id, expected="running", target="paused", reason="user")
        with self.assertRaises(StateConflict):
            commands.confirm_cost_and_resume(self.project, source.source_id, expected_cycle_id=token)
        self.write(commands.transition_source, source.source_id, expected="paused", target="running", reason="user")
        self.assertEqual(commands.evaluate_cost(self.project, source.source_id, estimated_anlas=1), "paused")
        self.reopen()
        self.assertIsNone(commands.cost_cycle_id(self.project, source.source_id))
        with self.assertRaises(StateConflict):
            commands.confirm_cost_and_resume(self.project, source.source_id, expected_cycle_id=token)

    def test_S03b_policy_change_clears_ack_does_not_retract_sent(self):
        source, _ = self.source(2)
        commands.evaluate_cost(self.project, source.source_id, estimated_anlas=1)
        commands.confirm_cost_and_resume(self.project, source.source_id,
                                        expected_cycle_id=commands.cost_cycle_id(self.project, source.source_id))
        item = self.write(commands.mark_sent, self.item(source.source_id).item_id).value
        self.write(commands.set_cost_policy, source.source_id, policy="allow")
        self.write(commands.set_cost_policy, source.source_id, policy="ask")
        self.assertEqual(commands.evaluate_cost(self.project, source.source_id, estimated_anlas=1), "paused")
        self.assertEqual(self.read(sources.get_item, item.item_id).status, "sent")
        self.assertNotIn("runtime_json", [row[1] for row in self.sql("PRAGMA table_info(sources)")])
        with self.assertRaises(ValidationError):
            commands.evaluate_cost(self.project, source.source_id, estimated_anlas=True)

    def test_S04_attempts_retries_cas_and_terminal_source_reopen(self):
        source, _ = self.source()
        item = self.item(source.source_id)
        sent = self.write(commands.mark_sent, item.item_id)
        self.assertEqual(sent.value.attempts, 1)
        self.assertFalse(self.write(commands.mark_sent, item.item_id).changed)
        self.write(commands.fail_item, item.item_id, attempt=1, error="generation:rejected")
        self.write(commands.transition_source, source.source_id, expected="running", target="done", reason="finished")
        result = self.write(commands.retry_failed_item, item.item_id, expected_attempt=1)
        self.assertEqual((result.value.status, result.value.attempts), ("pending", 2))
        self.assertEqual(self.read(sources.get_source, source.source_id).status, "paused")
        self.assertFalse(self.write(commands.retry_failed_item, item.item_id, expected_attempt=1).changed)
        self.write(commands.transition_source, source.source_id, expected="paused", target="running", reason="user")
        self.assertEqual(self.write(commands.mark_sent, item.item_id).value.attempts, 2)
        with self.assertRaises(StateConflict):
            self.write(commands.fail_item, item.item_id, attempt=1, error="stale")
        with self.assertRaises(StateConflict):
            self.write(commands.retry_failed_item, item.item_id, expected_attempt=2)

    def test_S05_canceled_source_accepts_sent_result_but_never_dispatches_pending(self):
        source, _ = self.source(2)
        artifact, originals = self.stage(self.item(source.source_id))
        self.write(commands.transition_source, source.source_id, expected="running", target="canceled", reason="user")
        self.write(commands.complete_item, artifact, originals)
        self.assertEqual(self.item(source.source_id).status, "done")
        with self.assertRaises(StateConflict):
            self.write(commands.mark_sent, self.item(source.source_id, 2).item_id)

    def test_S06_missing_images_cannot_be_kept_until_verified(self):
        image = self.image()
        self.write(images.set_review, image.image_id, expected="unreviewed", target="keep")
        path = self.root / "images" / image.filename
        content = path.read_bytes()
        path.unlink()
        self.reopen()
        current = self.read(images.get_image, image.image_id)
        self.assertEqual((current.review_status, current.file_missing), ("keep", True))
        for target in ("keep", "unreviewed"):
            with self.assertRaises(FileMissing):
                self.write(images.set_review, image.image_id, expected="keep", target=target)
        self.write(images.set_review, image.image_id, expected="keep", target="reject")
        path.write_bytes(b"wrong")
        with self.assertRaises(FileMissing):
            self.write(images.set_file_missing, image.image_id, missing=False, verified_sha256=image.sha256)
        path.write_bytes(content)
        self.write(images.set_file_missing, image.image_id, missing=False, verified_sha256=image.sha256)
        self.write(images.set_review, image.image_id, expected="reject", target="keep")

    def test_S06_bulk_review_is_atomic(self):
        first, second = self.image(), self.image()
        self.write(images.set_review, second.image_id, expected="unreviewed", target="reject")
        with self.assertRaises(StateConflict):
            self.write(commands.bulk_review, (first.image_id, second.image_id),
                       expected={first.image_id: "unreviewed", second.image_id: "unreviewed"}, target="keep")
        self.assertEqual(self.read(images.get_image, first.image_id).review_status, "unreviewed")
