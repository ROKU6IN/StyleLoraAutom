from stylelora.annotations.editing import bulk_edit_manual, edit_manual, get_annotation_view, review_images
from stylelora.exchange.importer import import_images
from stylelora.project_db.errors import PreviewStale
from stylelora.project_db.repositories import annotations
from stylelora.project_db.types import Tag
from .exchange_support import ExchangeTest


class EditingTests(ExchangeTest):
    def test_manual_edits_preserve_original_and_repeat_is_noop(self):
        image = self.imported()
        original = self.read(annotations.get_current, image.image_id, "original", "tags")
        result = edit_manual(self.project, image.image_id, tags=("green_hair", "Smile"), natural_language="新描述", write_nl=True)
        self.assertTrue(result.changed)
        self.assertEqual(self.read(annotations.get_current, image.image_id, "original", "tags"), original)
        view = get_annotation_view(self.project, image.image_id)
        self.assertEqual((view.tags, view.natural_language), (("green hair", "smile"), "新描述"))
        again = edit_manual(self.project, image.image_id, tags=("green_hair", "smile"), natural_language="新描述", write_nl=True)
        self.assertFalse(again.changed)

    def test_nl_only_and_clear_fallback_do_not_create_tag_snapshot(self):
        image = self.imported()
        edit_manual(self.project, image.image_id, natural_language="人工", write_nl=True)
        self.assertIsNone(self.read(annotations.get_current, image.image_id, "manual", "tags"))
        edit_manual(self.project, image.image_id, write_nl=True)
        self.assertEqual(get_annotation_view(self.project, image.image_id).natural_language, "原始描述。")

    def test_tags_and_nl_are_one_atomic_transaction(self):
        image = self.imported()
        before = self.database_contents()
        self.fail_at("edit_after_tags")
        with self.assertRaises(OSError):
            edit_manual(self.project, image.image_id, tags=("changed",), natural_language="changed", write_nl=True)
        self.assertEqual(self.database_contents(), before)

    def test_stale_revision_and_changed_read_set_are_rejected(self):
        image = self.imported()
        view = get_annotation_view(self.project, image.image_id)
        edit_manual(self.project, image.image_id, tags=("changed",))
        with self.assertRaises(PreviewStale):
            edit_manual(self.project, image.image_id, tags=("overwritten",), expected_revision=view.revision)
        with self.assertRaises(PreviewStale):
            edit_manual(self.project, image.image_id, tags=("overwritten",), read_set=view.refs)
        self.assertEqual(get_annotation_view(self.project, image.image_id).tags, ("changed",))

    def test_bulk_add_remove_normalized_and_atomic(self):
        self.input_image("a.png")
        self.input_image("b.png", color="blue")
        report = import_images(self.project, self.inputs)
        ids = tuple(item.image_id for item in report.items)
        bulk_edit_manual(self.project, ids, add=("new_tag", "Smile"), remove=("Blue_Hair",))
        for image_id in ids:
            self.assertEqual(get_annotation_view(self.project, image_id).tags, ("smile", "new tag"))
        before = self.database_contents()
        calls = 0
        def hook(stage):
            nonlocal calls
            if stage == "edit_after_tags":
                calls += 1
                if calls == 2:
                    raise OSError("second image edit failed")
        self.project._fault_hook = hook
        with self.assertRaises(OSError):
            bulk_edit_manual(self.project, ids, add=("rollback",))
        self.assertEqual(self.database_contents(), before)

    def test_bulk_revision_validation_before_any_changes(self):
        image = self.imported()
        revision = get_annotation_view(self.project, image.image_id).revision
        self.model_result(image.image_id, ("wd tag",))
        before = self.database_contents()
        with self.assertRaises(PreviewStale):
            bulk_edit_manual(self.project, (image.image_id,), add=("new",), expected={image.image_id: revision})
        self.assertEqual(self.database_contents(), before)

    def test_edit_view_keeps_null_confidence_wd_and_overlap_semantics(self):
        image = self.imported(caption=None)
        self.model_result(image.image_id, (Tag("unknown", None),))
        view = get_annotation_view(self.project, image.image_id)
        self.assertTrue(view.missing_original)
        self.assertEqual(view.tags, ("unknown",))
        self.assertTrue(view.wd_overlap["unknown"])
        empty = self.imported(name="b.png", color="blue", caption="")
        self.model_result(empty.image_id, (Tag("unknown", None),), cache="second")
        view = get_annotation_view(self.project, empty.image_id)
        self.assertFalse(view.missing_original)
        self.assertFalse(view.wd_overlap["unknown"])

    def test_review_expected_state_and_clear_all_tags(self):
        image = self.imported()
        review_images(self.project, (image.image_id,), "keep", expected={image.image_id: "unreviewed"})
        with self.assertRaises(PreviewStale):
            review_images(self.project, (image.image_id,), "reject", expected={image.image_id: "unreviewed"})
        edit_manual(self.project, image.image_id, tags=())
        view = get_annotation_view(self.project, image.image_id)
        self.assertEqual((view.tags, view.review_status), ((), "keep"))
