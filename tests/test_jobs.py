from dataclasses import replace

from stylelora.project_db import commands
from stylelora.project_db.errors import OutcomeConflict, SnapshotMismatch, StateConflict, ValidationError
from stylelora.project_db.repositories import annotations, images, jobs
from stylelora.project_db.types import (
    AnnotationInput, ImageFilter, JobOutcome, JobRequest, JobSnapshot, RatingValue, Scope, Tag,
)
from stylelora.project_db.validation import utc_now
from .support import DatabaseTest


class JobTests(DatabaseTest):
    def test_I03_current_cache_A_B_A_and_all_new_job(self):
        image = self.image()
        _, _, first = self.model_result(image.image_id, ("A",), cache="A")
        original = self.read(annotations.get_current, image.image_id, "wd", "tags")
        self.model_result(image.image_id, ("A",), cache="A")
        self.assertEqual(self.read(annotations.get_current, image.image_id, "wd", "tags").annotation_id,
                         original.annotation_id)
        self.model_result(image.image_id, ("B",), cache="B")
        self.model_result(image.image_id, ("A",), cache="A")
        self.assertEqual(self.sql("SELECT count(*) FROM annotations WHERE source='wd'"), ((3,),))
        self.model_result(image.image_id, ("A",), cache="A", mode="all")
        self.assertEqual(self.sql("SELECT count(*) FROM annotations WHERE source='wd'"), ((4,),))
        self.assertTrue(self.read(annotations.is_covered, image.image_id, {("wd", "tags"): "A:tags"}))

    def test_I04_multilayer_failure_rolls_back_annotations_and_progress(self):
        image = self.image()
        job = self.job((image.image_id,), executor="llm_vision", target_types=("tags", "nl"))
        tags = AnnotationInput(image.image_id, "llm", "tags", (Tag("B"),), model_name="fixture-model",
                               params={"schema_version": 1, "requested": {}, "actual": {}},
                               job_id=job.job_id, cache_key="tags")
        invalid_nl = replace(tags, type="nl", content=123, cache_key="nl")
        with self.assertRaises(ValidationError):
            self.write(commands.commit_job_outcome, job.job_id, JobOutcome(image.image_id, "done", (tags, invalid_nl)))
        self.assertIsNone(self.read(annotations.get_current, image.image_id, "llm", "tags"))
        self.assertEqual(self.read(jobs.get_job, job.job_id).done_count, 0)
        valid = replace(invalid_nl, content="A visible subject.")
        outcome = JobOutcome(image.image_id, "done", (tags, valid))
        result = self.write(commands.commit_job_outcome, job.job_id, outcome)
        self.assertEqual(result.value.done_count, 1)
        self.assertEqual(len(result.value.progress_json["outcomes"][str(image.image_id)]["annotation_ids"]), 2)
        self.assertFalse(self.write(commands.commit_job_outcome, job.job_id, outcome).changed)
        self.write(commands.transition_job, job.job_id, expected="running", target="done")
        self.assertFalse(self.write(commands.commit_job_outcome, job.job_id, outcome).changed)

    def test_I05_failed_skipped_replay_and_conflict(self):
        first, second = self.image(), self.image()
        job = self.job((first.image_id, second.image_id))
        failed = JobOutcome(first.image_id, "failed", reason="model:failed")
        skipped = JobOutcome(second.image_id, "skipped", reason="user:skip")
        for outcome in (failed, skipped, failed, skipped):
            self.write(commands.commit_job_outcome, job.job_id, outcome)
        row = self.read(jobs.get_job, job.job_id)
        self.assertEqual((row.done_count, row.failed_count, row.skipped_count), (0, 1, 1))
        with self.assertRaises(OutcomeConflict):
            self.write(commands.commit_job_outcome, job.job_id, replace(failed, reason="different"))
        self.write(commands.transition_job, job.job_id, expected="running", target="done")
        self.assertEqual(self.read(jobs.get_job, job.job_id).status, "done")

    def test_I05_frozen_scope_survives_new_images_and_review_changes(self):
        image = self.image()
        job = self.job((image.image_id,), mode="all")
        later = self.image()
        self.write(images.set_review, image.image_id, expected="unreviewed", target="reject")
        self.reopen()
        row = self.read(jobs.get_job, job.job_id)
        self.assertEqual(row.progress_json["image_ids"], [image.image_id])
        with self.assertRaises(StateConflict):
            self.write(commands.commit_job_outcome, job.job_id, JobOutcome(image.image_id, "skipped", reason="late"))
        self.write(commands.transition_job, job.job_id, expected="paused", target="running")
        with self.assertRaises(StateConflict):
            self.write(commands.commit_job_outcome, job.job_id, JobOutcome(later.image_id, "skipped", reason="outside"))
        self.write(commands.commit_job_outcome, job.job_id, JobOutcome(image.image_id, "skipped", reason="user:skip"))

    def test_I05_paused_inflight_and_canceled_late_result(self):
        image = self.image()
        job = self.job((image.image_id,))
        self.write(commands.transition_job, job.job_id, expected="running", target="paused")
        self.write(commands.commit_job_outcome, job.job_id, JobOutcome(image.image_id, "failed", reason="inflight"))
        self.write(commands.transition_job, job.job_id, expected="paused", target="done")
        another = self.job((image.image_id,))
        self.write(commands.transition_job, another.job_id, expected="running", target="canceled")
        result = self.write(commands.commit_job_outcome, another.job_id,
                            JobOutcome(image.image_id, "failed", reason="late"))
        self.assertFalse(result.changed)
        self.assertEqual(result.value.failed_count, 0)

    def test_I05_all_mode_rejects_cache_skip_and_partial_cache(self):
        image = self.image()
        self.model_result(image.image_id, ("A",))
        cached = self.read(annotations.get_current, image.image_id, "wd", "tags")
        job = self.job((image.image_id,), mode="all")
        with self.assertRaises(StateConflict):
            self.write(commands.commit_job_outcome, job.job_id,
                       JobOutcome(image.image_id, "skipped", reason="cache_hit", annotation_ids=(cached.annotation_id,)))
        llm = self.job((image.image_id,), executor="llm_vision", target_types=("tags", "nl"))
        with self.assertRaises(ValidationError):
            self.write(commands.commit_job_outcome, llm.job_id,
                       JobOutcome(image.image_id, "skipped", reason="cache_hit", annotation_ids=(cached.annotation_id,)))

    def test_I06_background_rating_cannot_overwrite_manual(self):
        image = self.image()
        manual = RatingValue("sensitive", "manual", True, utc_now())
        self.write(images.set_rating, image.image_id, expected=None, value=manual)
        with self.assertRaises(StateConflict):
            self.write(images.set_rating, image.image_id, expected=None,
                       value=RatingValue("general", "model", True, utc_now()))
        second = self.image()
        self.write(images.set_rating, second.image_id, expected=None,
                   value=RatingValue("questionable", "fallback", False, utc_now()))
        self.assertEqual(self.write(images.clear_recomputable_ratings, (image.image_id, second.image_id)), 1)
        self.assertEqual(self.read(images.get_image, image.image_id).rating_source, "manual")
        self.assertIsNone(self.read(images.get_image, second.image_id).rating)

    def test_batch_order_idempotency_and_snapshot_conflict(self):
        image = self.image()
        requests = (JobRequest("llm_vision", "selected", Scope(selected_ids=(image.image_id,))),
                    JobRequest("wd", "selected", Scope(selected_ids=(image.image_id,))))
        result = self.write(jobs.create_batch, "stable", requests)
        self.assertEqual([job.executor for job in result], ["wd", "llm_vision"])
        self.assertEqual(result, self.write(jobs.create_batch, "stable", requests))
        with self.assertRaises(SnapshotMismatch):
            self.write(jobs.create_batch, "stable", requests[:1])
        with self.assertRaises(StateConflict):
            self.write(jobs.freeze_job, result[1].job_id, JobSnapshot("fixture-model"), image_ids=(image.image_id,))
        self.write(jobs.freeze_job, result[0].job_id, JobSnapshot("fixture-model"), image_ids=(image.image_id,))
        with self.assertRaises(SnapshotMismatch):
            self.write(jobs.freeze_job, result[0].job_id, JobSnapshot("changed-model"), image_ids=(image.image_id,))

    def test_snapshot_secret_rejection(self):
        image = self.image()
        job = self.write(jobs.create_batch, "credentials",
                         (JobRequest("wd", "selected", Scope(selected_ids=(image.image_id,))),))[0]
        with self.assertRaises(ValidationError):
            self.write(jobs.freeze_job, job.job_id, JobSnapshot("x", params={"token": "secret"}),
                       image_ids=(image.image_id,))
