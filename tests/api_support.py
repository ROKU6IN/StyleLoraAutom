import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from stylelora.api.app import create_app
from stylelora.runtime.lifecycle import Runtime
from .support import TEMP_ROOT


class APITest(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.base = Path(self.temp.name)
        self.root = self.base / "http.slproj"
        self.inputs = self.base / "inputs"
        self.inputs.mkdir()
        self.runtime = Runtime(authority="127.0.0.1:8301")
        self.client = TestClient(create_app(self.runtime), base_url="http://127.0.0.1:8301",
                                 headers={"Authorization": "Bearer " + self.runtime.token})
        self.client.__enter__()
        self.addCleanup(self.cleanup)
        grant = self.runtime.grant(self.root, "project_create")
        created = self.request("POST", "/api/projects", {"grant_id": grant["grant_id"], "name": "HTTP test", "trigger_word": "http_style"}, status=202)
        project = self.wait_operation(created)["result"]
        self.pid = project["project_id"]
        self.p = "/api/projects/" + self.pid

    def cleanup(self):
        if self.runtime.context:
            self.runtime.context.handle._fault_hook = None
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def request(self, method, path, body=None, *, status=200, version=None, key=None, headers=None):
        options = dict(headers or {})
        if method == "POST":
            options["Idempotency-Key"] = key or str(uuid4())
        if version:
            options["If-Match"] = '"' + version + '"'
        result = self.client.request(method, path, json=body, headers=options)
        self.assertEqual(result.status_code, status, result.text)
        return result.json() if result.content else None

    def wait_operation(self, accepted, *, expected="done"):
        url = accepted.get("status_url") or "/api/operations/" + accepted["operation_id"]
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            result = self.request("GET", url)
            if result["status"] in ("done", "failed", "canceled", "interrupted"):
                self.assertEqual(result["status"], expected, result)
                return result
            time.sleep(0.01)
        self.fail("Operation did not finish")

    def make_input(self, name="sample.png", *, color="red", caption="blue_hair, smile\n\n原始描述。"):
        path = self.inputs / name
        Image.new("RGBA", (12, 8), color).save(path)
        path.with_suffix(".txt").write_text(caption, encoding="utf-8")
        return path

    def import_preview(self):
        grant = self.runtime.grant(self.inputs, "import_root", self.pid)
        accepted = self.request("POST", self.p+"/imports/preview", {"root_handle": grant["grant_id"]}, status=202)
        return self.wait_operation(accepted)["result"]

    def import_inputs(self, count=1):
        for index in range(count):
            self.make_input(f"{index}.png", color=(index*40, 25, 80, 200))
        preview = self.import_preview()
        accepted = self.request("POST", self.p+"/imports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202)
        return self.wait_operation(accepted)["result"]

    def keep(self, ids):
        values = [self.request("GET", self.p+f"/images/{i}") for i in ids]
        return self.request("POST", self.p+"/images/review", {"ids": ids, "target": "keep", "expected_versions": {str(value["image_id"]): value["version"] for value in values}})

    def export_preview(self, **options):
        accepted = self.request("POST", self.p+"/exports/preview", {"options": options}, status=202)
        return self.wait_operation(accepted)["result"]

    def run_export(self, preview):
        value = self.request("POST", self.p+"/exports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202)
        self.wait_operation({"operation_id": value["operation_id"]})
        return self.request("GET", value["status_url"])
