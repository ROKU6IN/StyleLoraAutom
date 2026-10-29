import shutil
from dataclasses import replace
from unittest.mock import patch

from PIL import Image

from stylelora.exchange.errors import ExchangeError
from stylelora.exchange.importer import import_images, scan_import
from stylelora.exchange.types import ImportOptions
from stylelora.project_db.errors import ActiveOperations, StateConflict, UnsupportedScheme
from stylelora.project_db.project import close_project
from stylelora.project_db.repositories import annotations, images, sources, schemes
from stylelora.project_db.types import ImportSchemeInput
from .exchange_support import ExchangeTest


class ImportTests(ExchangeTest):
    def test_sidecar_index_reuses_directory_but_checks_content_and_names(self):
        from pathlib import Path
        from stylelora.exchange.importer import SidecarIndex, _read_inputs
        from stylelora.project_db.validation import file_hash
        path = self.input_image()
        entry = scan_import(self.project, self.inputs).entries[0]
        index = SidecarIndex()
        original = Path.iterdir
        with patch.object(Path, 'iterdir', autospec=True, side_effect=original) as scan:
            for _ in range(20):
                _read_inputs(self.inputs, entry, ImportOptions(), index)
            self.assertEqual(scan.call_count, 1)
        sidecar = path.with_suffix('.txt')
        sidecar.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ExchangeError, 'sidecar_changed'):
            _read_inputs(self.inputs, entry, ImportOptions(), index)
        sidecar.unlink()
        with self.assertRaisesRegex(ExchangeError, 'sidecar_changed'):
            _read_inputs(self.inputs, entry, ImportOptions(), index)
        absent = replace(entry, sidecar_path=None, sidecar_sha256=None)
        _read_inputs(self.inputs, absent, ImportOptions(), index)
        sidecar.write_text('new', encoding='utf-8')
        with self.assertRaisesRegex(ExchangeError, 'sidecar_changed'):
            _read_inputs(self.inputs, absent, ImportOptions(), index)
        changed = replace(entry, sidecar_sha256=file_hash(sidecar))
        self.assertEqual(_read_inputs(self.inputs, changed, ImportOptions(), index)[1], b'new')

    def test_decodes_formats_preserves_bytes_and_png_metadata(self):
        paths = [self.input_image("原图.PNG", metadata={"Description": "画作"})]
        for ext in ("jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff"):
            paths.append(self.input_image("sample." + ext, color=(len(paths) * 20, 3, 9)))
        report = import_images(self.project, self.inputs)
        self.assertEqual((report.imported, report.failed, report.status), (8, 0, "done"))
        for result in report.items:
            image = self.read(images.get_image, result.image_id)
            self.assertEqual((image.width, image.height), (8, 6))
            self.assertEqual((self.root / "images" / image.filename).read_bytes(), (self.inputs / result.path).read_bytes())
        image = self.read(images.get_image, next(item.image_id for item in report.items if item.path == "原图.PNG"))
        self.assertEqual(image.png_meta_json["Description"], "画作")
        original = self.read(annotations.get_current, image.image_id, "original", "tags")
        self.assertEqual(original.content[0].tag, "Blue_Hair")
        self.assertRegex(image.phash, r'^[0-9a-f]{16}$')

    def test_stable_recursive_scan_and_shared_sidecar(self):
        self.input_image("z.png")
        self.input_image("a/B.png")
        self.input_image("A.png", color="blue")
        self.input_image("A.jpg", color="green")
        manifest = scan_import(self.project, self.inputs)
        paths = [entry.path for entry in manifest.entries]
        self.assertEqual(paths, sorted(paths, key=lambda name: (name.casefold(), name)))
        self.assertIn("sidecar:shared_stem", manifest.entries[0].warnings)
        self.assertEqual(len(scan_import(self.project, self.inputs, options=ImportOptions(recursive=False)).entries), 3)

    def test_duplicate_sha_skipped_in_batch_and_reimport(self):
        path = self.input_image("a.png")
        shutil.copyfile(path, self.inputs / "b.png")
        first = import_images(self.project, self.inputs)
        second = import_images(self.project, self.inputs)
        self.assertEqual((first.imported, first.skipped, second.skipped), (1, 1, 2))
        self.assertEqual(self.sql("SELECT count(*) FROM images")[0][0], 1)
        self.assertIsNone(first.items[1].image_id)

    def test_missing_existing_image_is_still_duplicate(self):
        image = self.imported()
        (self.root / "images" / image.filename).unlink()
        self.reopen()
        self.assertEqual(import_images(self.project, self.inputs).skipped, 1)

    def test_missing_sidecar_and_empty_sidecar(self):
        self.input_image("a.png", caption=None)
        self.input_image("b.png", color="blue", caption="")
        report = import_images(self.project, self.inputs)
        self.assertIsNone(self.read(annotations.get_current, report.items[0].image_id, "original", "tags"))
        self.assertEqual(self.read(annotations.get_current, report.items[1].image_id, "original", "tags").content, ())

    def test_bad_inputs_fail_individually_with_no_orphans(self):
        self.input_image("a.png", caption="smile\nBad layout")
        self.input_image("b.png", color="green", caption="蓝发", encoding="gbk")
        self.input_image("c.png", color="blue").write_bytes(b"not a png")
        self.input_image("d.png", color="yellow")
        report = import_images(self.project, self.inputs)
        self.assertEqual((report.imported, report.failed, report.status), (1, 3, "done"))
        self.assertEqual({item.error for item in report.items if item.error},
                         {"sidecar:invalid_layout", "encoding:invalid", "image:decode_failed"})
        self.assertEqual(len(list((self.root / "images").glob("*.png"))), 1)
        self.assertEqual(list((self.root / "images/.pending").rglob("out.png")), [])

    def test_limits_wrong_extension_and_multiframe(self):
        path = self.input_image("a.png")
        path.rename(self.inputs / "a.jpg")
        image = Image.new("RGB", (8, 6), "blue")
        image.save(self.inputs / "b.gif", save_all=True, append_images=[Image.new("RGB", (8, 6), "red")])
        self.input_image("c.png", color="green")
        report = import_images(self.project, self.inputs, options=ImportOptions(max_pixels=47))
        self.assertEqual(report.failed, 3)
        report = import_images(self.project, self.inputs)
        self.assertEqual((report.imported, report.failed), (1, 2))
        self.assertEqual({item.error for item in report.items if item.error}, {"image:format_mismatch", "image:multiframe_unsupported"})

    def test_project_overlap_and_file_count_rejected_before_source_creation(self):
        for path in (self.root, self.root.parent, self.root / "images"):
            with self.subTest(path=path), self.assertRaisesRegex(ExchangeError, "project_overlap"):
                import_images(self.project, path)
        self.input_image("a.png")
        self.input_image("b.png")
        with self.assertRaisesRegex(ExchangeError, "file_limit"):
            import_images(self.project, self.inputs, options=ImportOptions(max_files=1))
        self.assertEqual(self.sql("SELECT count(*) FROM sources")[0][0], 0)

    def test_changed_sidecar_after_snapshot_pauses_before_sent(self):
        path = self.input_image()
        def hook(stage):
            if stage == "import_after_manifest":
                path.with_suffix(".txt").write_text("changed", encoding="utf-8")
        self.project._fault_hook = hook
        report = import_images(self.project, self.inputs)
        self.assertEqual((report.status, report.pending, report.imported), ("paused", 1, 0))
        self.assertIn("input:sidecar_changed", report.warnings)
        self.assertEqual(self.item(report.source_id).attempts, 0)

    def test_resume_frozen_inputs_after_input_directory_moves(self):
        self.input_image()
        first = import_images(self.project, self.inputs, cancelled=lambda: True)
        moved = self.inputs.with_name("移动后")
        self.inputs.rename(moved)
        config = self.read(sources.get_source, first.source_id).config_json
        self.assertNotIn(str(self.inputs), str(config))
        with self.assertRaises(StateConflict):
            import_images(self.project, moved, source_id=first.source_id, options=ImportOptions(encoding="gbk"))
        resumed = import_images(self.project, moved, source_id=first.source_id)
        self.assertEqual((resumed.imported, resumed.status), (1, "done"))

    def test_copy_interruption_requires_explicit_retry(self):
        self.input_image()
        self.fail_at("import_after_mark_sent")
        with self.assertRaises(OSError):
            import_images(self.project, self.inputs)
        self.project._fault_hook = None
        source_id = self.sql("SELECT source_id FROM sources")[0][0]
        first = import_images(self.project, self.inputs, source_id=source_id)
        self.assertEqual((first.failed, first.imported), (1, 0))
        resumed = import_images(self.project, self.inputs, source_id=source_id, retry_failed=True)
        self.assertEqual((resumed.imported, self.item(source_id).attempts), (1, 2))

    def test_receipt_recovery_after_restart_imports_once(self):
        self.input_image()
        self.fail_at("import_after_receipt")
        first = import_images(self.project, self.inputs)
        self.assertEqual((first.unresolved, first.imported), (1, 0))
        self.project._fault_hook = None
        self.reopen()
        report = import_images(self.project, self.inputs, source_id=first.source_id)
        self.assertEqual((report.imported, report.unresolved), (1, 0))
        self.assertEqual(self.sql("SELECT count(*) FROM annotations")[0][0], 2)

    def test_new_sidecar_during_resume_is_detected(self):
        path = self.input_image(caption=None)
        first = import_images(self.project, self.inputs, cancelled=lambda: True)
        path.with_suffix(".txt").write_text("smile", encoding="utf-8")
        report = import_images(self.project, self.inputs, source_id=first.source_id)
        self.assertEqual(report.pending, 1)
        self.assertIn("input:sidecar_changed", report.warnings)

    def test_link_policy_and_live_operation_close(self):
        self.input_image()
        with patch("stylelora.exchange.importer.is_link", side_effect=lambda path: path.name.endswith(".png")):
            manifest = scan_import(self.project, self.inputs)
            self.assertFalse(manifest.entries)
            self.assertIn("input:linked_file_skipped", manifest.warnings)
        def hook(stage):
            if stage == "import_after_manifest":
                with self.assertRaises(ActiveOperations):
                    close_project(self.project)
        self.project._fault_hook = hook
        self.assertEqual(import_images(self.project, self.inputs).imported, 1)

    def test_duplicate_is_rechecked_in_final_registration_transaction(self):
        original = self.input_image()
        competitor = self.inputs.with_name("competing")
        competitor.mkdir()
        shutil.copyfile(original, competitor / "copy.png")
        def hook(stage):
            if stage == "import_after_receipt":
                self.project._fault_hook = None
                nested = import_images(self.project, competitor)
                self.assertEqual(nested.imported, 1)
        self.project._fault_hook = hook
        report = import_images(self.project, self.inputs)
        self.assertEqual((report.imported, report.skipped), (0, 1))
        self.assertEqual(self.sql("SELECT count(*) FROM images")[0][0], 1)
        self.assertEqual(list((self.root / "images/.pending").rglob("receipt.json")), [])

    def test_scheme_reader_options_are_applied_and_frozen(self):
        self.input_image(caption="蓝发", encoding="gbk")
        value = replace(ImportSchemeInput("GBK"), params_json={"schema_version": 1, "encoding": "gbk"})
        scheme = self.write(schemes.create_import_scheme, value)
        first = import_images(self.project, self.inputs, scheme_id=scheme.scheme_id, cancelled=lambda: True)
        changed = replace(value, params_json={"schema_version": 1, "encoding": "utf-8-sig"})
        self.write(schemes.update_import_scheme, scheme.scheme_id, changed, expected_updated_at=scheme.updated_at)
        resumed = import_images(self.project, self.inputs, source_id=first.source_id)
        self.assertEqual(resumed.imported, 1)
        scheme = self.write(schemes.create_import_scheme, replace(value, params_json={
            "schema_version": 1, "encoding": "gbk", "tag_separator": ";"}))
        with self.assertRaises(UnsupportedScheme):
            import_images(self.project, self.inputs, scheme_id=scheme.scheme_id)

    def test_catalog_revision_required_to_resume_and_aliases_applied(self):
        self.input_image(caption="azure_hair, smile")
        catalog = self.catalog()
        first = import_images(self.project, self.inputs, options=ImportOptions(parser="catalog-comma-v1"),
                              catalog=catalog, cancelled=lambda: True)
        changed = self.catalog([{"tag": "other"}])
        with self.assertRaisesRegex(ExchangeError, "catalog:snapshot_mismatch"):
            import_images(self.project, self.inputs, source_id=first.source_id, catalog=changed)
        report = import_images(self.project, self.inputs, source_id=first.source_id, catalog=catalog)
        annotation = self.read(annotations.get_current, report.items[0].image_id, "original", "tags")
        self.assertEqual(annotation.content[0].tag, "blue hair")

    def test_input_byte_limits_and_invalid_tag_are_item_failures(self):
        self.input_image(caption="_")
        self.assertEqual(import_images(self.project, self.inputs).items[0].error, "tag:invalid")
        self.assertEqual(import_images(self.project, self.inputs, options=ImportOptions(max_image_bytes=2)).items[0].error, "image:too_large")
        self.input_image(caption="long tags")
        self.assertEqual(import_images(self.project, self.inputs, options=ImportOptions(max_sidecar_bytes=2)).items[0].error, "sidecar:too_large")

    def test_scan_permission_errors_are_not_silently_omitted(self):
        self.input_image()
        def failed_walk(root, *, followlinks, onerror):
            onerror(PermissionError("denied subtree"))
        with patch("stylelora.exchange.importer.os.walk", side_effect=failed_walk):
            with self.assertRaisesRegex(ExchangeError, "input:scan_failed"):
                import_images(self.project, self.inputs)
        self.assertEqual(self.sql("SELECT count(*) FROM sources")[0][0], 0)
