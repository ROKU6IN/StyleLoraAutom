import hashlib
import json
from pathlib import Path
from uuid import uuid4

from stylelora.project_db.connection import read_snapshot
from stylelora.project_db.migrations import verify_integrity
from .api_support import APITest


class APILoopTests(APITest):
    def test_http_import_edit_review_export_and_integrity(self):
        report = self.import_inputs(2)
        self.assertEqual(report["imported"], 2)
        image_id = report["items"][0]["image_id"]
        page = self.request("GET", self.p+"/images")
        self.assertEqual((page["total"], page["limit"]), (2, 50))
        annotation = self.request("GET", self.p+f"/annotations/{image_id}")
        self.request("PUT", self.p+f"/annotations/{image_id}/manual", {
            "tags": [{"tag": "green_hair"}, {"tag": "outdoors"}], "natural_language": "HTTP 编辑描述。",
            "write_nl": True, "read_set": annotation["refs"]}, version=annotation["version"])
        self.keep([image_id])
        preview = self.export_preview(include_nl=True)
        exported = self.run_export(preview)
        self.assertEqual((exported["status"], exported["exported"], exported["skipped"]), ("finished", 1, 1))
        report = self.request("GET", exported["report_url"])
        output = self.root / report["directory"]
        caption = next(output.glob("*.txt")).read_bytes()
        self.assertEqual(caption, "http style, green hair, outdoors\n\nHTTP 编辑描述。\n".encode())
        self.assertEqual(next(output.glob("*.png")).read_bytes(), (self.inputs / "0.png").read_bytes())
        with read_snapshot(self.runtime.context.handle) as db:
            self.assertFalse(verify_integrity(db, full=True))
        self.assertEqual(self.request("GET", self.p+"/exports")["items"][0]["export_id"], exported["export_id"])

    def test_import_idempotency_and_body_conflict(self):
        self.make_input()
        preview = self.import_preview()
        body = {"preview_id": preview["preview_id"], "digest": preview["digest"], "name": "once"}
        key = str(uuid4())
        first = self.request("POST", self.p+"/imports", body, key=key, status=202)
        self.wait_operation(first)
        second = self.request("POST", self.p+"/imports", body, key=key, status=202)
        self.assertEqual(first["task"], second["task"])
        self.request("POST", self.p+"/imports", {**body, "name": "different"}, key=key, status=409)
        self.assertEqual(len(self.request("GET", self.p+"/sources")["items"]), 1)

    def test_changed_import_preview_fails_without_creating_source(self):
        path = self.make_input()
        preview = self.import_preview()
        path.with_suffix(".txt").write_text("changed", encoding="utf-8")
        accepted = self.request("POST", self.p+"/imports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202)
        result = self.wait_operation(accepted, expected="failed")
        self.assertEqual(result["error"]["code"], "concurrency:version_conflict")
        self.assertEqual(self.request("GET", self.p+"/sources")["items"], [])

    def test_stale_annotation_read_set_and_etag(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        view = self.request("GET", self.p+f"/annotations/{image_id}")
        body = {"tags": [{"tag": "changed"}], "read_set": view["refs"]}
        self.request("PUT", self.p+f"/annotations/{image_id}/manual", body, status=428)
        self.request("PUT", self.p+f"/annotations/{image_id}/manual", body, version=view["version"])
        self.request("PUT", self.p+f"/annotations/{image_id}/manual", body, version=view["version"], status=409)
        current = self.request("GET", self.p+f"/annotations/{image_id}")
        self.request("PUT", self.p+f"/annotations/{image_id}/manual", body, version=current["version"], status=409)
        self.assertEqual(current["layers"]["original/tags"]["content"][0]["tag"], "blue_hair")

    def test_review_aba_invalidates_version_and_replay_precedes_cas(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        before = self.request("GET", self.p+f"/images/{image_id}")
        body = {"ids": [image_id], "target": "keep", "expected_versions": {str(image_id): before["version"]}}
        key = str(uuid4())
        kept = self.request("POST", self.p+"/images/review", body, key=key)
        self.assertEqual(self.request("POST", self.p+"/images/review", body, key=key), kept)
        back = self.request("POST", self.p+"/images/review", {"ids": [image_id], "targets": {str(image_id): "unreviewed"},
                       "expected_versions": {str(image_id): kept["items"][0]["image"]["version"]}})
        self.assertNotEqual(back["items"][0]["image"]["version"], before["version"])
        self.request("POST", self.p+"/images/review", body, status=409)

    def test_batch_review_and_edit_are_atomic(self):
        ids = [item["image_id"] for item in self.import_inputs(2)["items"]]
        views = [self.request("GET", self.p+f"/annotations/{i}") for i in ids]
        body = {"ids": ids, "add": ["new_tag"], "remove": ["smile"], "read_set": [ref for view in views for ref in view["refs"]],
                "expected_versions": {str(view["image_id"]): view["version"] for view in views}}
        count = 0
        def fault(stage):
            nonlocal count
            if stage == "edit_after_tags":
                count += 1
                if count == 2:
                    raise OSError("injected")
        self.runtime.context.handle._fault_hook = fault
        self.request("POST", self.p+"/annotations/manual/bulk", body, status=503)
        self.runtime.context.handle._fault_hook = None
        for view in views:
            self.assertEqual(self.request("GET", self.p+f"/annotations/{view['image_id']}")["refs"], view["refs"])
        result = self.request("POST", self.p+"/annotations/manual/bulk", body)
        self.assertEqual([tag["tag"] for tag in result["items"][0]["tags"]], ["blue hair", "new tag"])

    def test_pagination_filters_and_cursor_binding(self):
        self.import_inputs(3)
        first = self.request("GET", self.p+"/images?limit=1")
        second = self.request("GET", self.p+"/images?limit=1&cursor="+first["next_cursor"])
        self.assertEqual(second["start_index"], 1)
        self.assertNotEqual(first["items"][0]["image_id"], second["items"][0]["image_id"])
        self.request("GET", self.p+"/images?sort=-id&cursor="+first["next_cursor"], status=422)
        self.request("GET", self.p+"/images?cursor="+first["next_cursor"]+"x", status=422)
        self.request("GET", self.p+"/images?limit=1001", status=422)
        self.request("GET", self.p+"/images?offset=1", status=422)
        around = self.request("GET", self.p+"/images?limit=1&around_id="+str(second["items"][0]["image_id"]))
        self.assertEqual(around["anchor_index"], 1)
        self.assertEqual(self.request("GET", self.p+"/images?q=smile")["total"], 3)

    def test_original_thumbnail_etag_and_missing_file(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        image = self.request("GET", self.p+f"/images/{image_id}")
        original = self.client.get(image["original_url"])
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.headers["etag"], '"'+hashlib.sha256(original.content).hexdigest()+'"')
        thumbnail = self.client.get(image["thumbnail_url"])
        self.assertEqual((thumbnail.status_code, thumbnail.headers["content-type"]), (200, "image/webp"))
        cached = self.client.get(image["thumbnail_url"], headers={"If-None-Match": "W/"+thumbnail.headers["etag"]})
        self.assertEqual((cached.status_code, cached.content), (304, b""))
        (self.root / "images" / image["filename"]).unlink()
        self.assertEqual(self.client.get(image["original_url"], headers={"If-None-Match": "*"}).status_code, 410)
        self.assertTrue(self.request("GET", self.p+f"/images/{image_id}")["file_missing"])

    def test_stale_export_preview_fails_before_publication(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        self.keep([image_id])
        preview = self.export_preview()
        image = self.request("GET", self.p+f"/images/{image_id}")
        self.request("POST", self.p+"/images/review", {"ids": [image_id], "target": "reject", "expected_versions": {str(image_id): image["version"]}})
        value = self.request("POST", self.p+"/exports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202)
        self.wait_operation({"operation_id": value["operation_id"]}, expected="failed")
        self.assertFalse(list((self.root / "exports").iterdir()))

    def test_export_pagination_excludes_later_publications(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        self.keep([image_id])
        first_export = self.run_export(self.export_preview())
        second_export = self.run_export(self.export_preview())
        first = self.request("GET", self.p+"/exports?limit=1")
        self.run_export(self.export_preview())
        second = self.request("GET", self.p+"/exports?limit=1&cursor="+first["next_cursor"])
        self.assertFalse(second["has_more"])
        self.assertEqual(first["items"][0]["export_id"], first_export["export_id"])
        self.assertEqual(second["items"][0]["export_id"], second_export["export_id"])

    def test_schemes_capabilities_and_openapi_expose_only_implemented_routes(self):
        self.assertEqual(len(self.request("GET", self.p+"/import-schemes")["items"]), 1)
        self.assertEqual(len(self.request("GET", self.p+"/export-schemes")["items"]), 1)
        self.assertFalse(self.request("GET", "/api/exchange/capabilities")["pipeline"])
        schema = self.request("GET", "/api/openapi.json")
        self.assertIn("/api/projects/{project_id}/imports", schema["paths"])
        self.assertNotIn("/api/jobs", schema["paths"])
        self.request("POST", "/api/filesystem/grants", {"purpose": "project_open"}, status=501)
