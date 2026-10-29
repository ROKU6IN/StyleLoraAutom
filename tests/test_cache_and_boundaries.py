import subprocess
import sys
from dataclasses import replace
from unittest.mock import patch

from stylelora.project_db import commands
from stylelora.project_db.cache import make_cache_key
from stylelora.project_db.errors import ArtifactConflict, ConstraintViolation, Corrupt, ValidationError
from stylelora.project_db.project import close_project, open_project
from stylelora.project_db.repositories import annotations, sources
from stylelora.project_db.types import AnnotationInput, CurrentRef, OriginalInput, Tag
from .support import DatabaseTest, ROOT


class BoundaryTests(DatabaseTest):
    def test_cache_identity_ignores_scheduling_but_tracks_output_inputs(self):
        args = dict(source="llm", type="tags", image_sha256="a" * 64, model="provider/model",
                    protocol="openai_chat", skill_hash="skill-v1", actual_params={"width": 1000, "timeout": 10},
                    route="vision", keyword_hash="keywords-v1")
        original = make_cache_key(**args)
        self.assertEqual(original, make_cache_key(**{**args, "actual_params": {"timeout": 100, "width": 1000}}))
        for change in (
            {"actual_params": {"width": 800}}, {"model": "another/model"}, {"skill_hash": "skill-v2"},
            {"keyword_hash": "keywords-v2"}, {"type": "nl"}, {"image_sha256": "b" * 64},
            {"evidence_digest": "new-evidence"},
        ):
            self.assertNotEqual(original, make_cache_key(**{**args, **change}))
        self.assertRegex(original, r"^annotation-cache-v1:[0-9a-f]{64}$")
        with self.assertRaises(ValidationError):
            make_cache_key(**{**args, "actual_params": {"endpoint": "http://localhost"}})

    def test_raw_annotation_repository_cannot_bypass_owning_commands(self):
        image = self.image()
        for layer in ("original", "manual", "rule", "wd", "llm"):
            with self.subTest(layer=layer), self.assertRaises(ConstraintViolation):
                self.write(annotations.append_version,
                           AnnotationInput(image.image_id, layer, "tags", (Tag("bad"),), params={"schema_version": 1}),
                           expected=CurrentRef(image.image_id, layer, "tags", None), policy="content")

    def test_completion_replay_rejects_changed_original_annotations(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        self.write(commands.complete_item, artifact, originals)
        with self.assertRaises(ArtifactConflict):
            self.write(commands.complete_item, artifact, (OriginalInput("tags", (Tag("different"),)),))

    def test_receipt_cannot_move_file_already_registered_to_another_owner(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        self.fail_at("artifact_after_rename")
        with self.assertRaises(OSError):
            self.write(commands.complete_item, artifact, originals)
        self.project._fault_hook = None
        other_source, _ = self.source()
        self.execute(
            "INSERT INTO images(image_id,filename,width,height,sha256,file_size,source_id,created_at) "
            "VALUES(1,'1.png',1,1,?,?,?,?)",
            (artifact.sha256, artifact.file_size, other_source.source_id, "2026-01-01T00:00:00.000000Z"),
        )
        with self.assertRaises(ArtifactConflict):
            self.write(commands.complete_item, artifact, originals)
        self.assertTrue((self.root / "images/1.png").is_file())
        self.assertEqual(self.sql("SELECT image_id,source_id FROM images"), ((1, other_source.source_id),))

    def test_receipt_candidate_id_collision_with_distinct_filename_gets_new_id(self):
        source, _ = self.source()
        artifact, originals = self.stage(self.item(source.source_id))
        self.fail_at("artifact_after_rename")
        with self.assertRaises(OSError):
            self.write(commands.complete_item, artifact, originals)
        self.project._fault_hook = None
        other_source, _ = self.source()
        (self.root / "images/1.jpg").write_bytes(b"other file")
        from stylelora.project_db.validation import file_hash
        self.execute(
            "INSERT INTO images(image_id,filename,width,height,sha256,file_size,source_id,created_at) "
            "VALUES(1,'1.jpg',1,1,?,10,?,?)",
            (file_hash(self.root / "images/1.jpg"), other_source.source_id, "2026-01-01T00:00:00.000000Z"),
        )
        completed = self.write(commands.complete_item, artifact, originals)
        self.assertEqual(completed.value.image_id, 2)
        self.assertTrue((self.root / "images/1.jpg").is_file())
        self.assertTrue((self.root / "images/2.png").is_file())

    def test_modified_schema_rejected_even_if_index_counts_match(self):
        close_project(self.project)
        import sqlite3
        db = sqlite3.connect(self.root / "project.db")
        try:
            db.execute("DROP INDEX ix_images_phash")
            db.execute("CREATE INDEX ix_images_phash ON images(width)")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(Corrupt):
            open_project(self.root)

    def test_cli_info_check_are_read_only_and_report_valid_project(self):
        close_project(self.project)
        before = (self.root / "project.db").read_bytes()
        import os
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
        for command in ("info", "check"):
            result = subprocess.run([sys.executable, "-m", "stylelora", command, str(self.root)],
                                    env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.root / "project.db").read_bytes(), before)
        self.project = open_project(self.root)
