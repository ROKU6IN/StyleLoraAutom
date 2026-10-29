import json
import os
import subprocess
import sys
from pathlib import Path

from stylelora.project_db.project import close_project
from .exchange_support import ExchangeTest
from .support import ROOT


class DatasetCLITests(ExchangeTest):
    def cli(self, *args, code=0):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-m", "stylelora", *map(str, args)], cwd=ROOT,
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.stdout else result.stderr

    def test_command_line_image_edit_export_loop_and_integrity(self):
        self.input_image()
        close_project(self.project)
        imported = self.cli("import", self.root, self.inputs)
        self.assertEqual(imported["imported"], 1)
        image_id = imported["items"][0]["image_id"]
        listed = self.cli("images", self.root)
        self.assertEqual(listed["items"][0]["image_id"], image_id)
        view = self.cli("show", self.root, image_id)
        self.cli("edit", self.root, image_id, "--tags", "custom_tag, smile", "--nl", "命令行描述", "--expected-revision", view["revision"])
        self.cli("edit", self.root, image_id, "--tags", "stale", "--expected-revision", view["revision"], code=1)
        self.cli("edit-many", self.root, image_id, "--add", "new_tag", "--remove", "smile")
        self.cli("review", self.root, image_id, "--status", "keep")
        preview = self.cli("export", self.root, "--include-nl", "--dry-run")
        self.assertTrue(preview["dry_run"])
        report = self.cli("export", self.root, "--include-nl")
        self.assertEqual(report["counts"]["exported"], 1)
        txt = next((self.root / report["directory"]).glob("*.txt")).read_text(encoding="utf-8")
        self.assertEqual(txt, "my style, custom tag, new tag\n\n命令行描述\n")
        self.assertTrue(self.cli("check", self.root)["ok"])
        self.assertEqual(self.cli("info", self.root)["counts"]["images"], 1)

    def test_partial_failures_exit_two_with_machine_readable_report(self):
        self.input_image("bad.png", caption="bad\nlayout")
        self.input_image("good.png", color="blue")
        close_project(self.project)
        report = self.cli("import", self.root, self.inputs, code=2)
        self.assertEqual((report["failed"], report["imported"]), (1, 1))
        self.cli("config", self.root, "--trigger-word", "unique_trigger")
        image_id = report["items"][1]["image_id"]
        self.cli("review", self.root, image_id, "--status", "keep")
        self.cli("edit", self.root, image_id, "--nl", "Artist_name painted it.")
        report = self.cli("export", self.root, "--include-nl", "--block", "artist_name", code=2)
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["trigger_word"], "unique trigger")

    def test_clear_annotations_and_explicit_gbk_and_no_recursive(self):
        self.input_image("good.png", caption="蓝发, 微笑", encoding="gbk")
        self.input_image("nested/unused.png", color="blue")
        close_project(self.project)
        report = self.cli("import", self.root, self.inputs, "--encoding", "gbk", "--no-recursive")
        self.assertEqual(report["selected"], 1)
        image_id = report["items"][0]["image_id"]
        self.cli("edit", self.root, image_id, "--clear-tags", "--clear-nl")
        self.assertEqual(self.cli("show", self.root, image_id)["tags"], [])
