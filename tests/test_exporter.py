import errno
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from stylelora.annotations.editing import edit_manual, get_annotation_view, review_images
from stylelora.exchange.errors import ExchangeError, ExportInterrupted
from stylelora.exchange.exporter import ExportOptions, export_images, preview_export
from stylelora.exchange.importer import import_images
from stylelora.project_db.errors import UnsupportedScheme
from stylelora.project_db.project import close_project, create_project, update_metadata
from stylelora.project_db.repositories import images, schemes, sources
from stylelora.project_db.types import ExportSchemeInput, SourceDraft
from .exchange_support import ExchangeTest


class ExportTests(ExchangeTest):
    def test_full_loop_report_and_byte_preservation_without_database_writes(self):
        original = self.input_image(metadata={"Description": "嵌入元数据"})
        report = import_images(self.project, self.inputs)
        image_id = report.items[0].image_id
        edit_manual(self.project, image_id, tags=("smile", "green_hair", "my_style"), natural_language="编辑后的描述。", write_nl=True)
        review_images(self.project, (image_id,), "keep")
        before = self.database_contents()
        report = export_images(self.project, options=ExportOptions(include_nl=True, include_json=True))
        self.assertEqual(report["counts"], {"selected": 1, "exported": 1, "skipped": 0, "failed": 0})
        self.assertEqual(self.database_contents(), before)
        directory = self.root / report["directory"]
        image = self.read(images.get_image, image_id)
        self.assertEqual((directory / image.filename).read_bytes(), original.read_bytes())
        caption = directory / (Path(image.filename).stem + ".txt")
        self.assertEqual(caption.read_bytes(), "my style, smile, green hair\n\n编辑后的描述。\n".encode())
        self.assertEqual(report["tag_frequencies"], {"my style": 1, "smile": 1, "green hair": 1})
        self.assertEqual(report["trigger_count"], 1)
        self.assertEqual(report["trigger_check"], "unverified_no_catalog")
        self.assertNotIn("编辑后的描述", json.dumps(report, ensure_ascii=False))
        metadata = json.loads((directory / (Path(image.filename).stem + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(metadata["png_metadata"]["Description"], "嵌入元数据")
        for record in report["images"][0]["files"]:
            data = (directory / record["path"]).read_bytes()
            self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (record["bytes"], record["sha256"]))
        self.assertEqual(json.loads((directory / "export_report.json").read_text(encoding="utf-8")), report)

    def test_keep_only_reject_missing_never_exported_and_counts_balance(self):
        for index in range(4):
            self.input_image(f"{index}.png", color=(index * 50, 20, 40))
        imported = import_images(self.project, self.inputs)
        ids = tuple(item.image_id for item in imported.items)
        review_images(self.project, (ids[0], ids[3]), "keep")
        review_images(self.project, (ids[2],), "reject")
        self.write(images.set_file_missing, ids[3], missing=True)
        report = export_images(self.project)
        self.assertEqual(report["counts"], {"selected": 4, "exported": 1, "skipped": 3, "failed": 0})
        self.assertEqual(report["reason_counts"], {"image:missing": 1, "review:filtered": 1, "review:reject": 1})
        report = export_images(self.project, options=ExportOptions(include_unreviewed=True))
        self.assertEqual((report["counts"]["exported"], report["counts"]["skipped"]), (2, 2))

    def test_repeated_exports_publish_new_directories_and_never_change_previous(self):
        image = self.kept()
        first = export_images(self.project)
        directory = self.root / first["directory"]
        files = {path.name: path.read_bytes() for path in directory.iterdir()}
        edit_manual(self.project, image.image_id, tags=("changed",))
        second = export_images(self.project)
        self.assertNotEqual(first["directory"], second["directory"])
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, files)

    def test_annotation_changes_after_snapshot_only_affect_next_export(self):
        image = self.kept()
        def hook(stage):
            if stage == "export_after_snapshot":
                edit_manual(self.project, image.image_id, tags=("edited later",))
        self.project._fault_hook = hook
        report = export_images(self.project)
        txt = next((self.root / report["directory"]).glob("*.txt")).read_text(encoding="utf-8")
        self.assertEqual(txt, "my style, blue hair, smile\n")
        self.assertEqual(get_annotation_view(self.project, image.image_id).tags, ("edited later",))

    def test_natural_language_punctuation_is_not_validated_as_a_tag(self):
        self.kept(caption="smile\n\n___")
        report = export_images(self.project, options=ExportOptions(include_nl=True))
        self.assertEqual(report["counts"]["exported"], 1)
        caption = next((self.root / report["directory"]).glob("*.txt")).read_text(encoding="utf-8")
        self.assertEqual(caption, "my style, smile\n\n___\n")

    def test_source_changed_or_disappears_is_failed_and_pair_removed(self):
        image = self.kept()
        path = self.root / "images" / image.filename
        path.write_bytes(b"changed")
        before = self.database_contents()
        report = export_images(self.project)
        self.assertEqual(report["counts"]["failed"], 1)
        directory = self.root / report["directory"]
        self.assertEqual(list(directory.glob("*.png")) + list(directory.glob("*.txt")), [])
        self.assertEqual(self.database_contents(), before)
        path.unlink()
        report = export_images(self.project)
        self.assertEqual(report["reason_counts"], {"input:missing": 1})

    def test_caption_write_failure_rolls_back_image_txt_pair(self):
        self.kept()
        self.fail_at("export_after_caption")
        report = export_images(self.project)
        directory = self.root / report["directory"]
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertFalse(list(directory.glob("*.png")) or list(directory.glob("*.txt")) or list(directory.glob(".pair-*")))

    def test_disk_full_interrupts_without_publishing_directory(self):
        self.kept()
        def hook(stage):
            if stage == "export_after_image_copy":
                raise OSError(errno.ENOSPC, "injected full disk")
        self.project._fault_hook = hook
        with self.assertRaises(ExportInterrupted) as caught:
            export_images(self.project)
        partial = self.root / caught.exception.relative_directory
        state = json.loads((partial / ".export-state.json").read_text(encoding="utf-8"))
        self.assertEqual((state["status"], state["reason"]), ("interrupted", "storage:full"))
        self.assertFalse((self.root / state["directory"]).exists())
        self.assertEqual(list(partial.glob("*.png")), [])

    def test_cancellation_and_publish_failure_leave_recoverable_receipts(self):
        self.kept()
        with self.assertRaises(ExportInterrupted) as caught:
            export_images(self.project, cancelled=lambda: True)
        self.assertEqual(caught.exception.code, "export:canceled")
        self.fail_at("export_before_publish")
        with self.assertRaises(ExportInterrupted) as caught:
            export_images(self.project)
        partial = self.root / caught.exception.relative_directory
        state = json.loads((partial / ".export-state.json").read_text())
        self.assertFalse((self.root / state["directory"]).exists())
        self.assertEqual(len(list(partial.glob("*.txt"))), 1)

    def test_trigger_required_syntax_style_collision_and_catalog_check(self):
        self.kept()
        for trigger in ("", "smile,blue", "{smile}", "(smile:1.2)", "AND"):
            update_metadata(self.project, name="sample", trigger_word=trigger)
            with self.subTest(trigger=trigger), self.assertRaises(ExchangeError):
                export_images(self.project)
        update_metadata(self.project, name="sample", trigger_word="my_style")
        self.write(sources.create_source, SourceDraft("nai", "style collision", style_prompt="artist my_style painter"))
        with self.assertRaisesRegex(ExchangeError, "style_conflict"):
            export_images(self.project)
        update_metadata(self.project, name="sample", trigger_word="smile")
        catalog = self.catalog()
        with self.assertRaisesRegex(ExchangeError, "existing_tag"):
            export_images(self.project, catalog=catalog)
        report = export_images(self.project, catalog=catalog, options=ExportOptions(allow_existing_trigger=True))
        self.assertEqual(report["trigger_check"], "found")

    def test_blocklist_removal_warn_and_nl_review_do_not_mutate_annotations(self):
        image = self.kept(caption="artist_name, blue_hair\n\nA scene by artist_name.")
        before = self.database_contents()
        options = ExportOptions(blocked_tags=("artist_name",))
        report = export_images(self.project, options=options)
        self.assertEqual(report["images"][0]["removed_tags"], ["artist name"])
        self.assertNotIn("artist name", report["tag_frequencies"])
        report = export_images(self.project, options=replace(options, include_nl=True))
        self.assertEqual(report["reason_counts"], {"caption:nl_requires_review": 1})
        report = export_images(self.project, options=replace(options, include_nl=True, blocklist_mode="warn"))
        self.assertIn("artist name", report["tag_frequencies"])
        self.assertIn("caption:nl_blocklist_match", report["images"][0]["warnings"])
        self.assertEqual(self.database_contents(), before)

    def test_catalog_artist_block_and_alias(self):
        self.kept(caption="artist_alias, smile")
        catalog = self.catalog([{"tag": "artist_name", "category": "artist", "aliases": ["artist_alias"]}])
        report = export_images(self.project, catalog=catalog)
        self.assertEqual(report["images"][0]["removed_tags"], ["artist alias"])

    def test_invalid_catalog_rejected_before_use(self):
        for tags in ([{"tag": "{weighted}"}], [{"tag": "_"}],
                     [{"tag": "a", "aliases": ["same"]}, {"tag": "b", "aliases": ["same"]}]):
            with self.subTest(tags=tags), self.assertRaisesRegex(ExchangeError, "catalog:invalid"):
                self.catalog(tags)

    def test_source_filter_and_style_removal_preserve_database_evidence(self):
        first = self.kept()
        second = self.kept(name="second.png", color="blue")
        # Real generated-source row supplied through the existing F01 completion helper.
        generated = self.image(tag_values=("artist sample", "smile"))
        self.execute("UPDATE sources SET style_prompt='artist_sample' WHERE source_id=?", (generated.source_id,))
        review_images(self.project, (generated.image_id,), "keep")
        before = self.database_contents()
        report = export_images(self.project, options=ExportOptions(source_ids=(generated.source_id,)))
        self.assertEqual(report["counts"]["selected"], 1)
        self.assertEqual(report["images"][0]["removed_tags"], ["artist sample"])
        self.assertEqual(self.database_contents(), before)

    def test_dry_run_writes_nothing_and_unknown_pipeline_or_mode_rejected(self):
        self.kept()
        before = self.database_contents()
        preview = preview_export(self.project)
        self.assertEqual(preview["images"][0]["status"], "ready")
        self.assertFalse(list((self.root / "exports").iterdir()))
        self.assertEqual(self.database_contents(), before)
        for value in (replace(ExportSchemeInput("bad"), pipeline_json=({"name": "resize"},)),):
            scheme = self.write(schemes.create_export_scheme, value)
            with self.assertRaises(UnsupportedScheme):
                export_images(self.project, scheme_id=scheme.scheme_id)

    def test_export_can_be_reimported_into_a_new_project(self):
        image = self.kept()
        edit_manual(self.project, image.image_id, tags=("custom_tag",), natural_language="可往返描述。", write_nl=True)
        report = export_images(self.project, options=ExportOptions(include_nl=True))
        export_dir = self.root / report["directory"]
        close_project(self.project)
        self.root = Path(self.temp.name) / "roundtrip.slproj"
        self.project = create_project(self.root, name="roundtrip", trigger_word="my_style")
        imported = import_images(self.project, export_dir)
        self.assertEqual(imported.imported, 1)
        view = get_annotation_view(self.project, imported.items[0].image_id)
        self.assertEqual((view.tags, view.natural_language), (("my style", "custom tag"), "可往返描述。"))
