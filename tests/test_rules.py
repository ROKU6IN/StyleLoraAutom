from dataclasses import replace
from unittest.mock import patch

from stylelora.project_db import commands
from stylelora.project_db.errors import BatchTooLarge, PreviewStale, UndoConflict, UnsupportedScheme, UnsupportedSorter
from stylelora.project_db.merge import merge_caption, preview_caption
from stylelora.project_db.repositories import annotations, images, operations, schemes
from stylelora.project_db.rules import capture_read_set, edit_manual_nl
from stylelora.project_db.types import ExportSchemeInput, OriginalInput, Tag
from .support import DatabaseTest


class RuleTests(DatabaseTest):
    def merged(self, image_id, **kwargs):
        return self.read(merge_caption, image_id, **kwargs)

    def manual(self, image_id, values):
        return self.write(commands.edit_manual_tags, image_id, tags=tuple(Tag(value) for value in values),
                          read_set=self.read(capture_read_set, (image_id,)))

    def test_U01_rule_failure_and_undo_failure_are_atomic(self):
        first, second = self.image(), self.image()
        self.fail_at("rule_after_annotation")
        with self.assertRaises(OSError):
            self.apply({first.image_id: ("B",), second.image_id: ("C",)})
        self.project._fault_hook = None
        self.assertEqual(self.sql("SELECT count(*) FROM annotations WHERE source='rule'"), ((0,),))
        self.assertEqual(self.sql("SELECT count(*) FROM operation_log"), ((0,),))
        op = self.apply({first.image_id: ("B",), second.image_id: ("C",)})
        self.fail_at("undo_after_annotation")
        with self.assertRaises(OSError):
            self.write(commands.undo_rule, op.op_id)
        self.project._fault_hook = None
        self.assertEqual(self.merged(first.image_id).tags, ("b",))
        self.assertEqual(self.read(operations.get_operation, op.op_id).status, "applied")

    def test_U02_repeated_result_no_log_and_new_WD_does_not_update_rule(self):
        image = self.image()
        first = self.apply({image.image_id: ("B",)})
        second = self.apply({image.image_id: ("B",)})
        self.assertIsNone(second.op_id)
        self.assertEqual(second.affected_count, 0)
        rule_id = self.read(annotations.get_current, image.image_id, "rule", "tags").annotation_id
        self.model_result(image.image_id, ("A",))
        self.assertEqual(self.read(annotations.get_current, image.image_id, "rule", "tags").annotation_id, rule_id)
        self.assertEqual(self.sql("SELECT count(*) FROM operation_log"), ((1,),))

    def test_U03_undo_to_absence_and_idempotency(self):
        image = self.image()
        op = self.apply({image.image_id: ("B",)})
        result = self.write(commands.undo_rule, op.op_id)
        self.assertTrue(result.changed)
        self.assertIsNone(self.read(annotations.get_current, image.image_id, "rule", "tags"))
        self.assertEqual(self.merged(image.image_id).tags, ("a",))
        self.assertFalse(self.write(commands.undo_rule, op.op_id).changed)
        self.assertEqual(self.sql("SELECT count(*) FROM annotations WHERE source='rule'"), ((1,),))

    def test_U04_latest_only_and_sequential_undo(self):
        image = self.image()
        a = self.apply({image.image_id: ("B",)})
        b = self.apply({image.image_id: ("C",)})
        with self.assertRaises(UndoConflict):
            self.write(commands.undo_rule, a.op_id)
        self.write(commands.undo_rule, b.op_id)
        self.assertEqual(self.merged(image.image_id).tags, ("b",))
        self.write(commands.undo_rule, a.op_id)
        self.assertEqual(self.merged(image.image_id).tags, ("a",))

    def test_U04_later_manual_or_model_prevents_undo_but_review_does_not(self):
        image = self.image()
        op = self.apply({image.image_id: ("B",)})
        self.write(images.set_review, image.image_id, expected="unreviewed", target="keep")
        self.manual(image.image_id, ("B", "C"))
        with self.assertRaises(UndoConflict):
            self.write(commands.undo_rule, op.op_id)
        other = self.image()
        op2 = self.apply({other.image_id: ("B",)})
        self.write(images.set_review, other.image_id, expected="unreviewed", target="reject")
        self.write(commands.undo_rule, op2.op_id)

    def test_U05_preview_detects_absent_layer_added_and_nonmatching_scope_change(self):
        first, second = self.image(), self.image()
        plan = self.rule_plan({first.image_id: ("B",)}, scope_ids=(first.image_id, second.image_id))
        self.manual(second.image_id, ("A", "C"))
        with self.assertRaises(PreviewStale):
            self.write(commands.apply_rule, plan)
        stale = self.read(capture_read_set, (first.image_id,))
        self.model_result(first.image_id, ("Z",))
        with self.assertRaises(PreviewStale):
            self.write(commands.edit_manual_tags, first.image_id, tags=(Tag("B"),), read_set=stale)

    def test_U06_evidence_cutoff_new_WD_and_repeat_removal(self):
        image = self.image()
        self.apply({image.image_id: ("B",)})
        self.assertEqual(self.merged(image.image_id).tags, ("b",))
        self.model_result(image.image_id, ("A",))
        self.assertEqual(self.merged(image.image_id).tags, ("b", "a"))
        second = self.apply({image.image_id: ("B",)})
        self.assertEqual(self.merged(image.image_id).tags, ("b",))
        self.write(commands.undo_rule, second.op_id)
        self.assertEqual(self.merged(image.image_id).tags, ("b", "a"))

    def test_U07_full_union_normalization_NL_and_sort_adapter(self):
        originals = (OriginalInput("tags", (Tag("Long_Hair"), Tag("BLUE_EYES"))),
                     OriginalInput("nl", "Original description."))
        image = self.image(originals=originals)
        self.model_result(image.image_id, ("Blue_Eyes", "smile"))
        self.model_result(image.image_id, ("dress",), executor="llm_vision", natural_language="Vision description.")
        self.manual(image.image_id, ("long hair", "blue eyes", "smile", "dress", "outdoors"))
        self.apply({image.image_id: ("long hair", "blue eyes", "smile", "dress", "outdoors", "sun")})
        config = dict(ExportSchemeInput("preview").caption_config_json)
        config["include_nl"] = True
        merged = self.merged(image.image_id, config=config, trigger_word="MY_STYLE")
        self.assertEqual(merged.tags[0], "my style")
        self.assertEqual(set(merged.tags[1:]), {"long hair", "blue eyes", "smile", "dress", "outdoors", "sun"})
        self.assertEqual(merged.natural_language, "Vision description.")
        ref = self.read(annotations.current_ref, image.image_id, "manual", "nl")
        self.write(edit_manual_nl, image.image_id, content="Manual description.", expected=ref)
        self.assertEqual(self.merged(image.image_id, config=config).natural_language, "Manual description.")
        config["tag_sort"] = "prompt_pyramid"
        with self.assertRaises(UnsupportedSorter):
            self.merged(image.image_id, config=config)
        self.assertEqual(self.merged(image.image_id, config=config, sorter=sorted).tags,
                         tuple(sorted(set(merged.tags[1:]))))
        with self.assertRaises(UnsupportedSorter):
            self.merged(image.image_id, config=config, sorter=lambda _: ("invented",))

    def test_U08_copy_keeps_origin_and_only_actual_removals_advance_cutoff(self):
        image = self.image(tag_values=("A", "C"))
        self.apply({image.image_id: ("B", "C")})
        original_rule = self.read(annotations.get_current, image.image_id, "rule", "tags")
        cutoff = original_rule.params_json["merge_meta"]["removals"][0]["cutoff_annotation_id"]
        self.model_result(image.image_id, ("A",))
        self.apply({image.image_id: ("B", "A", "D")})
        current = self.read(annotations.get_current, image.image_id, "rule", "tags")
        removals = {r["tag"]: r for r in current.params_json["merge_meta"]["removals"]}
        self.assertEqual(removals["a"]["cutoff_annotation_id"], cutoff)
        self.assertIn("a", self.merged(image.image_id).tags)
        self.assertLess(current.params_json["merge_meta"]["tag_origins"]["a"][0]["annotation_id"],
                        current.annotation_id)
        self.apply({image.image_id: ("B", "D")})
        self.manual(image.image_id, ("B", "D", "A"))
        self.assertIn("a", self.merged(image.image_id).tags)

    def test_U08_same_timestamp_uses_ids_and_disabled_layer_disables_removals(self):
        image = self.image()
        with patch("stylelora.project_db.rules.utc_now", return_value="2026-01-01T00:00:00.000000Z"):
            self.apply({image.image_id: ("B",)})
            self.apply({image.image_id: ("C",)})
        config = dict(ExportSchemeInput("x").caption_config_json)
        config["tag_layers"] = ["original"]
        self.assertEqual(self.merged(image.image_id, config=config).tags, ("a",))
        self.assertEqual(self.merged(image.image_id).tags, ("c",))

    def test_U09_WD_origin_confidence_and_null_filtering(self):
        image = self.image(tag_values=("stable",))
        self.model_result(image.image_id, (Tag("low", 0.2), Tag("high", 0.95), Tag("unknown", None)))
        self.apply({image.image_id: ("stable", "low", "high", "unknown")})
        config = dict(ExportSchemeInput("x").caption_config_json)
        config["wd_min_confidence"] = 0.8
        self.assertEqual(set(self.merged(image.image_id, config=config).tags), {"stable", "high"})
        config["wd_null_confidence"] = "include"
        self.assertEqual(set(self.merged(image.image_id, config=config).tags), {"stable", "high", "unknown"})

    def test_P01_large_rule_is_rejected_without_partial_change(self):
        image = self.image()
        plan = self.rule_plan({image.image_id: ("B",)})
        with patch("stylelora.project_db.rules.MAX_RULE_BYTES", 8), self.assertRaises(BatchTooLarge):
            self.write(commands.apply_rule, plan)
        self.assertIsNone(self.read(annotations.get_current, image.image_id, "rule", "tags"))

    def test_E02_read_only_caption_and_unsupported_pipeline(self):
        image = self.image()
        before = self.sql("SELECT * FROM annotations")
        scheme = self.read(schemes.get_export_scheme, 1)
        self.read(preview_caption, image.image_id, scheme)
        self.assertEqual(before, self.sql("SELECT * FROM annotations"))
        unsupported = replace(ExportSchemeInput("pipeline"), pipeline_json=({"processor": "future"},))
        saved = self.write(schemes.create_export_scheme, unsupported)
        with self.assertRaises(UnsupportedScheme):
            self.read(preview_caption, image.image_id, saved)
