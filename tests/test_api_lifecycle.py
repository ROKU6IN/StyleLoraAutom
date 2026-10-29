from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from .api_support import APITest


class APILifecycleTests(APITest):
    def test_import_root_rebinding_checks_version_after_file_verification(self):
        from stylelora.services.exchange import _read_inputs
        imported = self.import_inputs()
        source_id = imported["source_id"]
        source = self.request("GET", self.p+f"/sources/{source_id}")
        grant = self.runtime.grant(self.inputs, "import_root", self.pid)
        gate = threading.Barrier(2)
        def verify(*args, **kwargs):
            gate.wait(5)
            return _read_inputs(*args, **kwargs)
        with patch("stylelora.services.exchange._read_inputs", side_effect=verify):
            operations = [self.request("POST", self.p+f"/sources/{source_id}/import-root", {"root_handle": grant["grant_id"]},
                                       version=source["version"], status=202) for _ in range(2)]
            deadline = time.monotonic()+8
            while time.monotonic() < deadline:
                values = [self.request("GET", operation["status_url"]) for operation in operations]
                if all(value["status"] in ("done", "failed") for value in values):
                    break
                time.sleep(.01)
        self.assertEqual(sorted(value["status"] for value in values), ["done", "failed"])
        failure = next(value for value in values if value["status"] == "failed")
        self.assertEqual(failure["error"]["code"], "concurrency:version_conflict")

    def test_disconnected_request_keeps_its_admitted_work_and_lease(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def work():
            with self.runtime.lease(self.pid):
                entered.set()
                release.wait(5)
            finished.set()
        async def disconnected():
            call = asyncio.create_task(self.runtime.call(work))
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
            self.assertEqual(self.runtime.context.users, 1)
            self.assertFalse(finished.is_set())
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 3))
        asyncio.run(disconnected())
        self.assertEqual(self.runtime.context.users, 0)

    def test_active_grant_cannot_be_revoked_until_scan_finishes(self):
        from stylelora.services.exchange import scan_import
        self.make_input()
        grant = self.runtime.grant(self.inputs, "import_root", self.pid)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def scan(*args, **kwargs):
            entered.set()
            release.wait(5)
            return scan_import(*args, **kwargs)
        with patch("stylelora.services.exchange.scan_import", side_effect=scan):
            operation = self.request("POST", self.p+"/imports/preview", {"root_handle": grant["grant_id"]}, status=202)
            self.assertTrue(entered.wait(3))
            self.request("DELETE", "/api/filesystem/grants/"+grant["grant_id"], version=grant["version"], status=409)
            release.set()
            self.wait_operation(operation)
        self.request("DELETE", "/api/filesystem/grants/"+grant["grant_id"], version=grant["version"], status=204)

    def test_project_metadata_validation_and_version_check(self):
        project = self.request("GET", self.p)
        for name in ("   ", "bad\u0000name"):
            self.request("PATCH", self.p, {"name": name}, version=project["version"], status=422)
        updated = self.request("PATCH", self.p, {"name": "Updated"}, version=project["version"])
        self.assertEqual(updated["metadata"]["name"], "Updated")
        self.request("PATCH", self.p, {"name": "Stale"}, version=project["version"], status=409)
        self.close_reopen()
        self.assertEqual(self.request("GET", self.p)["metadata"]["name"], "Updated")

    def test_failed_switch_leaves_no_project_and_old_id_expired(self):
        invalid = self.base / "invalid.slproj"
        invalid.mkdir()
        for name in ("images", "exports"):
            (invalid / name).mkdir()
        (invalid / "project.json").write_text("{}", encoding="utf-8")
        (invalid / "project.db").write_bytes(b"not a database")
        grant = self.runtime.grant(invalid, "project_open")
        value = self.request("POST", "/api/projects/open", {"grant_id": grant["grant_id"]}, status=202)
        self.wait_operation(value, expected="failed")
        self.assertIsNone(self.request("GET", "/api/projects/current"))
        self.request("GET", self.p, status=410)

    def test_two_clients_cannot_overwrite_the_same_annotation_version(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        view = self.request("GET", self.p+f"/annotations/{image_id}")
        gate = threading.Barrier(2)
        def edit(tag):
            gate.wait(5)
            return self.client.put(self.p+f"/annotations/{image_id}/manual", json={"tags": [{"tag": tag}], "read_set": view["refs"]},
                                   headers={"If-Match": '"'+view["version"]+'"'})
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(edit, "first"), pool.submit(edit, "second")
            replies = [first.result(10), second.result(10)]
        self.assertEqual(sorted(response.status_code for response in replies), [200, 409])
        winner = next(response.json()["annotation"] for response in replies if response.status_code == 200)
        self.assertEqual(self.request("GET", self.p+f"/annotations/{image_id}")["tags"], winner["tags"])

    def test_close_waits_for_file_task_then_invalidates_old_handles(self):
        context = self.runtime.context
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def copy(task):
            entered.set()
            if not release.wait(8):
                raise TimeoutError()
            return {"completed": True}
        operation = self.runtime.supervisor.submit("test.file", copy, context=context)
        self.assertTrue(entered.wait(5))
        project = self.request("GET", self.p)
        closed = self.request("POST", self.p+"/close", {}, version=project["version"], status=202)
        deadline = time.monotonic()+3
        while not context.closing and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(context.closing)
        self.assertFalse(context.handle._closed)
        self.assertEqual(self.request("GET", closed["status_url"])["status"], "running")
        self.request("GET", self.p+"/images", status=410)
        release.set()
        self.wait_operation(closed)
        self.assertTrue(context.handle._closed)
        self.request("GET", "/api/operations/"+operation["operation_id"], status=410)
        self.assertIsNone(self.request("GET", "/api/projects/current"))

    def test_reopen_replaces_project_id_and_expires_grants_previews_cursors(self):
        self.import_inputs(2)
        grant = self.runtime.grant(self.inputs, "import_root", self.pid)
        preview = self.export_preview()
        cursor = self.request("GET", self.p+"/images?limit=1")["next_cursor"]
        old_id, old_path = self.pid, self.p
        self.close_reopen()
        self.assertNotEqual(self.pid, old_id)
        self.request("GET", old_path+"/images", status=410)
        self.request("GET", self.p+"/images?cursor="+cursor, status=422)
        self.request("POST", self.p+"/imports/preview", {"root_handle": grant["grant_id"]}, status=410)
        self.request("POST", self.p+"/exports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=410)

    def close_reopen(self):
        project = self.request("GET", self.p)
        self.wait_operation(self.request("POST", self.p+"/close", {}, version=project["version"], status=202))
        grant = self.runtime.grant(self.root, "project_open")
        result = self.wait_operation(self.request("POST", "/api/projects/open", {"grant_id": grant["grant_id"]}, status=202))["result"]
        self.pid, self.p = result["project_id"], "/api/projects/"+result["project_id"]

    def test_notification_failure_does_not_undo_committed_edit(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        view = self.request("GET", self.p+f"/annotations/{image_id}")
        with patch.object(self.runtime.events, "publish", side_effect=RuntimeError("notification failed")):
            self.request("PUT", self.p+f"/annotations/{image_id}/manual", {"tags": [{"tag": "committed"}], "read_set": view["refs"]}, version=view["version"])
        self.assertEqual(self.request("GET", self.p+f"/annotations/{image_id}")["tags"], [{"tag": "committed", "conf": None}])

    def test_rollback_publishes_no_annotation_event(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        view = self.request("GET", self.p+f"/annotations/{image_id}")
        sub, _ = self.runtime.events.subscribe(topics=("annotations",))
        def fail(stage):
            if stage == "edit_after_tags":
                raise OSError("rollback")
        self.runtime.context.handle._fault_hook = fail
        self.request("PUT", self.p+f"/annotations/{image_id}/manual", {"tags": [{"tag": "rollback"}], "read_set": view["refs"]}, version=view["version"], status=503)
        self.runtime.context.handle._fault_hook = None
        self.assertTrue(sub.queue.empty())
        self.assertEqual(self.request("GET", self.p+f"/annotations/{image_id}")["version"], view["version"])
        self.runtime.events.unsubscribe(sub)

    def test_queued_task_is_cancelable_and_reads_survive_draining(self):
        release = threading.Event()
        self.addCleanup(release.set)
        blockers = [self.runtime.supervisor.submit("test.block", lambda task: release.wait(8), context=self.runtime.context) for _ in range(2)]
        operation = self.runtime.supervisor.submit("test.never", lambda task: self.fail("Canceled work ran"), context=self.runtime.context)
        self.request("POST", "/api/operations/"+operation["operation_id"]+"/cancel", {}, version=operation["version"])
        release.set()
        self.wait_operation(operation, expected="canceled")
        self.runtime.draining = True
        try:
            self.request("GET", self.p+"/images")
            self.request("POST", self.p+"/exports/preview", {}, status=503)
        finally:
            self.runtime.draining = False

    def test_pause_import_finishes_accepted_file_and_explicit_resume_completes(self):
        for i in range(3):
            self.make_input(f"{i}.png", color=(i*30, 10, 55, 255))
        preview = self.import_preview()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def pause_copy(stage):
            if stage == "import_after_copy" and not entered.is_set():
                entered.set()
                if not release.wait(8):
                    raise TimeoutError()
        self.runtime.context.handle._fault_hook = pause_copy
        sub, _ = self.runtime.events.subscribe(topics=("source",), project_id=self.pid)
        accepted = self.request("POST", self.p+"/imports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202)
        self.assertTrue(entered.wait(5))
        source = self.request("GET", self.p+"/sources")["items"][0]
        self.request("POST", self.p+f"/sources/{source['source_id']}/control", {"action": "pause"}, version=source["version"])
        release.set()
        self.wait_operation(accepted, expected="canceled")
        self.runtime.context.handle._fault_hook = None
        source = self.request("GET", self.p+f"/sources/{source['source_id']}")
        self.assertEqual((source["status"], source["counts"]["done"], source["counts"]["pending"]), ("paused", 1, 2))
        resumed = self.request("POST", self.p+f"/sources/{source['source_id']}/control", {"action": "resume"}, version=source["version"], status=202)
        self.assertEqual(self.wait_operation(resumed)["result"]["imported"], 3)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get())
        completed = [event["data"]["payload"]["item_id"] for event in events if event["type"] == "source.item_completed"]
        self.assertEqual(len(completed), 3)
        self.assertEqual(len(set(completed)), 3)
        self.runtime.events.unsubscribe(sub)

    def test_export_cancel_preserves_partial_and_reopen_recovers_reports(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        self.keep([image_id])
        finished = self.run_export(self.export_preview())
        preview = self.export_preview()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def block(stage):
            if stage == "export_after_image_copy":
                entered.set()
                if not release.wait(8):
                    raise TimeoutError()
        self.runtime.context.handle._fault_hook = block
        value = self.request("POST", self.p+"/exports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202)
        self.assertTrue(entered.wait(5))
        view = self.request("GET", value["status_url"])
        self.request("POST", value["status_url"]+"/cancel", {}, version=view["version"])
        release.set()
        self.wait_operation({"operation_id": value["operation_id"]}, expected="canceled")
        self.runtime.context.handle._fault_hook = None
        self.assertEqual(self.request("GET", value["status_url"])["status"], "canceled")
        self.close_reopen()
        records = {item["export_id"]: item for item in self.request("GET", self.p+"/exports")["items"]}
        self.assertEqual(records[finished["export_id"]]["status"], "finished")
        self.assertEqual(records[value["export_id"]]["status"], "interrupted")
        self.request("GET", records[finished["export_id"]]["report_url"])
        self.assertTrue((self.root / records[value["export_id"]]["relative_dir"] / ".export-state.json").is_file())
