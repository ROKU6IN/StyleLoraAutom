from dataclasses import replace

from stylelora.project_db import commands
from stylelora.project_db.artifacts import receipt_path
from stylelora.project_db.errors import ArtifactConflict, StateConflict
from stylelora.project_db.project import close_project, open_project
from stylelora.project_db.recovery import inspect_sent, reconcile_item, recover_project
from stylelora.project_db.repositories import annotations, images, jobs, sources
from stylelora.project_db.types import RecoveryEvidence
from .support import DatabaseTest, PNG


class RecoveryTests(DatabaseTest):
    def test_I02_complete_item_idempotent_and_conflicting_attempt_hash(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        first = self.write(commands.complete_item, artifact, originals)
        self.assertFalse(self.write(commands.complete_item, artifact, originals).changed)
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((1,),))
        self.assertEqual(self.sql("SELECT count(*) FROM annotations"), ((1,),))
        for bad in (replace(artifact, attempt=2), replace(artifact, sha256="0" * 64)):
            with self.assertRaises(ArtifactConflict):
                self.write(commands.complete_item, bad, originals)

    def test_R01_running_sources_jobs_pause_once_and_never_auto_resume(self):
        image = self.image()
        job = self.job((image.image_id,))
        self.reopen()
        self.assertEqual(self.read(sources.get_source, image.source_id).status, "paused")
        self.assertEqual(self.read(jobs.get_job, job.job_id).status, "paused")
        self.assertIn(job.job_id, self.project.recovery_report.paused_job_ids)
        repeated = recover_project(self.project)
        self.assertEqual((repeated.paused_source_ids, repeated.paused_job_ids), ((), ()))

    def test_R02_no_receipt_unknown_absent_requires_explicit_evidence(self):
        source, _ = self.source()
        item = self.write(commands.mark_sent, self.item(source.source_id).item_id).value
        self.assertEqual(inspect_sent(self.project, item).verdict, "unknown")
        self.reopen()
        self.assertEqual(self.read(sources.get_item, item.item_id).status, "sent")
        self.assertIn(item.item_id, self.project.recovery_report.unresolved_item_ids)
        with self.assertRaises(StateConflict):
            self.write(commands.transition_source, source.source_id, expected="paused", target="running", reason="user")
        self.write(reconcile_item, RecoveryEvidence(item.item_id, 1, "absent", reason="probe:confirmed_absent"))
        self.assertEqual(self.read(sources.get_item, item.item_id).status, "failed")
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((0,),))

    def test_R03_partial_file_without_receipt_is_never_adopted(self):
        source, _ = self.source()
        item = self.write(commands.mark_sent, self.item(source.source_id).item_id).value
        path = self.root / f"images/.pending/{item.item_id}/1/out.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(PNG[:10])
        self.reopen()
        self.assertEqual(self.read(sources.get_item, item.item_id).status, "sent")
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((0,),))

    def test_R03_complete_receipt_before_rename_recovers(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        self.fail_at("artifact_before_rename")
        with self.assertRaises(OSError):
            self.write(commands.complete_item, artifact, originals)
        self.project._fault_hook = None
        self.reopen()
        self.assertEqual(self.read(sources.get_item, artifact.item_id).status, "done")
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((1,),))
        self.assertFalse(receipt_path(self.root, artifact.item_id, 1).exists())
        self.assertIn(artifact.item_id, self.project.recovery_report.recovered_item_ids)

    def test_R03_renamed_file_before_db_commit_recovers(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        self.fail_at("artifact_after_rename")
        with self.assertRaises(OSError):
            self.write(commands.complete_item, artifact, originals)
        self.project._fault_hook = None
        self.assertTrue((self.root / "images/1.png").is_file())
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((0,),))
        self.reopen()
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((1,),))
        self.assertEqual(self.sql("SELECT count(*) FROM annotations"), ((1,),))
        self.reopen()
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((1,),))

    def test_R03_db_committed_before_receipt_cleanup(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        self.fail_at("artifact_after_commit")
        with self.assertRaises(OSError):
            self.write(commands.complete_item, artifact, originals)
        self.project._fault_hook = None
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((1,),))
        self.assertTrue(receipt_path(self.root, artifact.item_id, 1).is_file())
        self.reopen()
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((1,),))
        self.assertFalse(receipt_path(self.root, artifact.item_id, 1).exists())

    def test_R03_orphan_target_is_never_overwritten(self):
        unrelated = self.root / "images/1.png"
        unrelated.write_bytes(b"unrelated orphan")
        image = self.image()
        self.assertEqual(image.image_id, 2)
        self.assertEqual(unrelated.read_bytes(), b"unrelated orphan")
        report = recover_project(self.project)
        self.assertEqual(report.orphan_filenames, ("1.png",))

    def test_R03_rolled_back_candidate_id_reassigned_without_wrong_ownership(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        self.fail_at("artifact_after_rename")
        with self.assertRaises(OSError):
            self.write(commands.complete_item, artifact, originals)
        self.project._fault_hook = None
        # A separate completion must skip the durable uncommitted candidate filename.
        other = self.image()
        self.assertEqual(other.image_id, 2)
        self.reopen()
        recovered = self.read(sources.get_item, artifact.item_id)
        self.assertEqual(recovered.image_id, 1)
        self.assertEqual(self.read(images.get_image, 1).source_id, source.source_id)
        self.assertEqual(self.read(images.get_image, 2).source_id, other.source_id)

    def test_R03_tampered_receipt_or_bytes_stay_unresolved(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        (self.root / artifact.relative_staging_path).write_bytes(b"modified")
        with self.assertRaises(ArtifactConflict):
            self.write(commands.complete_item, artifact, originals)
        self.reopen()
        self.assertEqual(self.read(sources.get_item, artifact.item_id).status, "sent")
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((0,),))

    def test_R04_deleted_reject_preserves_annotations_and_operation_history(self):
        image = self.image()
        op = self.apply({image.image_id: ("B",)})
        self.write(images.set_review, image.image_id, expected="unreviewed", target="reject")
        (self.root / "images" / image.filename).unlink()
        self.reopen()
        self.assertTrue(self.read(images.get_image, image.image_id).file_missing)
        self.assertEqual(self.sql("SELECT count(*) FROM annotations"), ((2,),))
        self.write(commands.undo_rule, op.op_id)
        self.assertIsNotNone(self.read(annotations.get_current, image.image_id, "original", "tags"))

    def test_E01_move_project_and_delete_cache_preserve_evidence(self):
        source, _ = self.source()
        artifact, _ = self.stage(self.item(source.source_id))
        close_project(self.project)
        (self.root / "cache").rmdir()
        moved = self.root.with_name("移动后.slproj")
        self.root.rename(moved)
        self.root = moved
        self.project = open_project(moved)
        self.assertTrue((moved / "cache").is_dir())
        self.assertEqual(self.read(sources.get_item, artifact.item_id).status, "done")
        metadata = (moved / "project.json").read_text(encoding="utf-8")
        self.assertNotIn(str(moved), metadata)
