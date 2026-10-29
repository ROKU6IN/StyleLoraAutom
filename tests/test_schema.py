import math
import sqlite3
from dataclasses import replace

from stylelora.project_db import commands
from stylelora.project_db.errors import ConstraintViolation, StateConflict, UnsafePath, ValidationError
from stylelora.project_db.migrations import verify_integrity
from stylelora.project_db.repositories import annotations, images, schemes, sources
from stylelora.project_db.types import (
    AnnotationInput, CurrentRef, ExportSchemeInput, ImportSchemeInput, RatingValue, SourceDraft, Tag,
)
from stylelora.project_db.validation import encode, relative_path, utc_now
from .support import DatabaseTest


class SchemaTests(DatabaseTest):
    def test_D01_exact_schema_indexes_versions_and_pragmas(self):
        columns = dict(self.sql("SELECT name, ncol FROM pragma_table_list WHERE schema='main' AND name<>'sqlite_schema'"))
        self.assertEqual(columns, {"sources": 13, "source_items": 10, "images": 16, "annotations": 12,
                                  "jobs": 17, "operation_log": 9, "export_schemes": 9, "import_schemes": 7})
        self.assertEqual(sum(columns.values()), 93)
        self.assertEqual(self.sql("SELECT count(*),sum(sql IS NOT NULL) FROM sqlite_master WHERE type='index'"), ((17, 15),))
        self.assertEqual(self.sql("PRAGMA user_version"), ((1,),))
        self.assertEqual(self.sql("PRAGMA journal_mode"), (("wal",),))
        self.assertEqual(self.sql("PRAGMA foreign_keys"), ((1,),))
        self.assertEqual(self.sql("PRAGMA query_only"), ((1,),))
        self.assertEqual(self.write(lambda db: db.execute("PRAGMA synchronous").fetchone()[0]), 2)
        self.assertEqual(self.read(verify_integrity, full=True), ())

    def test_D02_every_not_null_constraint(self):
        image = self.image()
        self.model_result(image.image_id, ("B",))
        self.apply({image.image_id: ("C",)})
        tables = dict(self.sql("SELECT name, ncol FROM pragma_table_list WHERE schema='main' AND name<>'sqlite_schema'"))
        for table in tables:
            for _, column, _, required, _, primary in self.sql(f"PRAGMA table_info({table})"):
                if required and not primary:
                    with self.subTest(table=table, column=column), self.assertRaises(ConstraintViolation):
                        self.execute(f"UPDATE {table} SET {column}=NULL")

    def test_D02_unknown_enums_and_detector_reserved(self):
        image = self.image()
        self.model_result(image.image_id, ("B",))
        self.apply({image.image_id: ("C",)})
        for table, column in (
            ("sources", "type"), ("sources", "status"), ("sources", "cost_policy"),
            ("source_items", "status"), ("source_items", "aspect"),
            ("images", "review_status"), ("images", "rating"), ("images", "rating_source"),
            ("annotations", "source"), ("annotations", "type"), ("jobs", "status"),
            ("jobs", "executor"), ("jobs", "mode"), ("operation_log", "status"),
            ("export_schemes", "exporter"), ("import_schemes", "reader"),
        ):
            with self.subTest(table=table, column=column), self.assertRaises(ConstraintViolation):
                self.execute(f"UPDATE {table} SET {column}='unknown'")
        self.execute("UPDATE images SET rating='general',rating_source='detector',rating_verified=0,rated_at=?",
                     (utc_now(),))
        with self.assertRaises(ValidationError):
            self.write(images.set_rating, image.image_id, expected=None,
                       value=RatingValue("general", "detector", False, utc_now()))

    def test_D03_foreign_keys_restrict_and_orphans(self):
        image = self.image()
        for statement, params in (
            ("DELETE FROM sources WHERE source_id=?", (image.source_id,)),
            ("DELETE FROM images WHERE image_id=?", (image.image_id,)),
            ("UPDATE images SET source_id=999999", ()),
            ("UPDATE source_items SET image_id=999999", ()),
            ("UPDATE annotations SET image_id=999999", ()),
        ):
            with self.subTest(statement=statement), self.assertRaises(ConstraintViolation):
                self.execute(statement, params)
        self.model_result(image.image_id, ("B",))
        with self.assertRaises(ConstraintViolation):
            self.execute("UPDATE annotations SET job_id=999999 WHERE source='wd'")

    def test_D04_uniqueness_and_partial_indexes(self):
        image = self.image()
        source, _ = self.source(2)
        with self.assertRaises(ConstraintViolation):
            self.execute("UPDATE source_items SET seq=1 WHERE source_id=?", (source.source_id,))
        with self.assertRaises(ConstraintViolation):
            self.execute("UPDATE source_items SET status='done',image_id=? WHERE source_id=?",
                         (image.image_id, source.source_id))
        second = self.write(schemes.create_import_scheme, ImportSchemeInput("second"))
        with self.assertRaises(ConstraintViolation):
            self.execute("UPDATE import_schemes SET is_default=1 WHERE scheme_id=?", (second.scheme_id,))
        current = self.read(annotations.get_current, image.image_id, "original", "tags")
        with self.assertRaises(ConstraintViolation):
            self.execute(
                "INSERT INTO annotations(image_id,source,type,content,params_json,created_at) "
                "SELECT image_id,source,type,content,params_json,created_at FROM annotations WHERE annotation_id=?",
                (current.annotation_id,),
            )

    def test_D04a_import_duplicate_skip_and_summary(self):
        image = self.image()
        source, _ = self.source(importing=True)
        item = self.item(source.source_id)
        result = self.write(commands.skip_duplicate, item.item_id, sha256=image.sha256)
        self.assertEqual((result.value.status, result.value.error, result.value.image_id, result.value.attempts),
                         ("skipped", "duplicate_sha256", None, 0))
        self.assertFalse(self.write(commands.skip_duplicate, item.item_id, sha256=image.sha256).changed)
        self.assertEqual(self.read(sources.count_item_states, source.source_id)["skipped"], 1)
        self.write(commands.transition_source, source.source_id, expected="running", target="done", reason="finished")
        nai, _ = self.source()
        with self.assertRaises(StateConflict):
            self.write(commands.skip_duplicate, self.item(nai.source_id).item_id, sha256=image.sha256)
        with self.assertRaises(StateConflict):
            self.write(commands.retry_failed_item, item.item_id, expected_attempt=1)

    def test_D05_tags_shape_json_and_confidence(self):
        image = self.image()
        for content in ("{}", "invalid json", "null", "42"):
            with self.subTest(content=content), self.assertRaises(ConstraintViolation):
                self.execute("UPDATE annotations SET content=?", (content,))
        for conf in (-0.01, 1.01, float("inf")):
            with self.subTest(conf=conf), self.assertRaises(ConstraintViolation):
                self.execute("UPDATE annotations SET confidence=?", (conf,))
        from stylelora.project_db.rules import capture_read_set
        for bad in (Tag("", 0.1), Tag("A", math.nan), Tag("A", True), Tag("A", -1)):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.write(commands.edit_manual_tags, image.image_id, tags=(bad,),
                           read_set=self.read(capture_read_set, (image.image_id,)))

    def test_D06_duplicates_allowed_and_invalid_values(self):
        first, second = self.image(), self.image()
        self.assertNotEqual(first.image_id, second.image_id)
        self.assertEqual(len(self.read(images.find_by_sha256, first.sha256)), 2)
        for column, bad in (("sha256", "ABC"), ("width", 0), ("height", -1), ("file_size", 0),
                            ("filename", "../x.png"), ("filename", "C:\\x.png"), ("file_missing", 2)):
            with self.subTest(column=column), self.assertRaises(ConstraintViolation):
                self.execute(f"UPDATE images SET {column}=? WHERE image_id=?", (bad, second.image_id))
        with self.assertRaises(ConstraintViolation):
            self.execute("UPDATE images SET filename=? WHERE image_id=?", (first.filename, second.image_id))
        for value in (float("nan"), float("inf"), {1: "bad"}, {"x": object()}):
            with self.assertRaises(ValidationError):
                encode(value)
        for path in ("../escape", "C:/escape", "//server/share", "images\\x.png",
                     "images/../x.png", "images//x.png", "images/NUL", "/tmp/x", "images/name."):
            with self.subTest(path=path), self.assertRaises(UnsafePath):
                relative_path(self.root, path)

    def test_D07_rating_atomicity_and_reserved_semantics(self):
        image = self.image()
        for sql in (
            "UPDATE images SET rating='general'",
            "UPDATE images SET rating='general',rating_source='fallback',rating_verified=1,rated_at='x'",
            "UPDATE images SET rating='general',rating_source='manual',rating_verified=0,rated_at='x'",
        ):
            with self.assertRaises(ConstraintViolation):
                self.execute(sql)
        for rating in ("general", "sensitive", "questionable", "explicit"):
            old = images.rating_value(self.read(images.get_image, image.image_id))
            value = RatingValue(rating, "manual", True, utc_now())
            self.write(images.set_rating, image.image_id, expected=old, value=value)
        old = self.read(images.get_image, image.image_id)
        self.write(images.set_rating, image.image_id, expected=images.rating_value(old), value=None)
        image = self.read(images.get_image, image.image_id)
        self.assertEqual((image.rating, image.rating_source, image.rating_verified, image.rated_at), (None, None, False, None))

    def test_E02_default_schemes_switch_delete_and_edit_tokens(self):
        first = self.read(schemes.get_default_import_scheme)
        self.assertEqual(first.params_json["encoding"], "utf-8-sig")
        second = self.write(schemes.create_import_scheme, ImportSchemeInput("second"))
        self.write(schemes.set_default_import_scheme, second.scheme_id)
        self.assertEqual(self.read(schemes.get_default_import_scheme).scheme_id, second.scheme_id)
        with self.assertRaises(StateConflict):
            self.write(schemes.delete_import_scheme, second.scheme_id)
        self.write(schemes.delete_import_scheme, first.scheme_id)
        export = self.read(schemes.get_export_scheme, 1)
        self.assertEqual(export.filter_json["review_statuses"], ["keep"])
        value = ExportSchemeInput("changed")
        changed = self.write(schemes.update_export_scheme, 1, value, expected_updated_at=export.updated_at)
        self.assertGreater(changed.value.updated_at, export.updated_at)
        self.assertFalse(self.write(schemes.update_export_scheme, 1, value,
                                    expected_updated_at=export.updated_at).changed)
        with self.assertRaises(StateConflict):
            self.write(schemes.update_export_scheme, 1, replace(value, name="stale"),
                       expected_updated_at=export.updated_at)
        with self.assertRaises(StateConflict):
            self.write(schemes.delete_export_scheme, 1)
