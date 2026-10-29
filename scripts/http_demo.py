"""Run the import/edit/review/export HTTP loop against an idle local service.

Creates only a new demo directory with synthetic images. Credentials stay in
memory and local directory grants are obtained via the current-user pipe.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from PIL import Image

from stylelora.runtime.instance import pipe_request


def run(parent):
    credentials = pipe_request({"action": "hello"})
    with httpx.Client(base_url="http://"+credentials["authority"], headers={"Authorization": "Bearer "+credentials["token"]},
                      trust_env=False, follow_redirects=False, timeout=30) as client:
        def request(method, path, body=None, version=None):
            headers = {"Idempotency-Key": str(uuid4())}
            if version:
                headers["If-Match"] = '"'+version+'"'
            response = client.request(method, path, json=body, headers=headers)
            response.raise_for_status()
            return response.json()

        def wait(accepted):
            deadline = time.monotonic()+120
            while time.monotonic() < deadline:
                value = request("GET", accepted["status_url"])
                if value["status"] in ("done", "finished"):
                    return value.get("result") or value
                if value["status"] in ("failed", "canceled", "interrupted"):
                    raise RuntimeError("Demo task failed: " + json.dumps(value, ensure_ascii=False))
                time.sleep(.05)
            raise TimeoutError("The accepted task still exists; inspect its status before retrying")

        if request("GET", "/api/projects/current") is not None:
            raise RuntimeError("A project is already open. Close it explicitly before running the demo.")
        parent = parent.resolve(strict=True)
        base = parent / ("http-demo-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid4().hex[:8])
        inputs, project_path = base / "inputs", base / "demo.slproj"
        inputs.mkdir(parents=True)
        for number, color in enumerate(("#3078b5", "#ce9141")):
            Image.new("RGB", (64, 48), color).save(inputs / f"{number}.png")
            (inputs / f"{number}.txt").write_text("blue_hair, smile\n\nSynthetic demo image.\n", encoding="utf-8")
        grant = pipe_request({"action": "grant", "purpose": "project_create", "path": str(project_path)})
        project = wait(request("POST", "/api/projects", {"grant_id": grant["grant_id"], "name": "HTTP 演示", "trigger_word": "demo_style"}))
        pid, p = project["project_id"], "/api/projects/"+project["project_id"]
        grant = pipe_request({"action": "grant", "purpose": "import_root", "path": str(inputs), "project_id": pid})
        preview = wait(request("POST", p+"/imports/preview", {"root_handle": grant["grant_id"]}))
        imported = wait(request("POST", p+"/imports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}))
        ids = [item["image_id"] for item in imported["items"] if item["status"] == "done"]
        for image_id in ids:
            annotation = request("GET", p+f"/annotations/{image_id}")
            request("PUT", p+f"/annotations/{image_id}/manual", {"tags": [{"tag": "green_hair"}, {"tag": "outdoors"}],
                    "natural_language": "通过 HTTP 保存的描述。", "write_nl": True, "read_set": annotation["refs"]}, annotation["version"])
        versions = {str(i): request("GET", p+f"/images/{i}")["version"] for i in ids}
        request("POST", p+"/images/review", {"ids": ids, "target": "keep", "expected_versions": versions})
        preview = wait(request("POST", p+"/exports/preview", {"options": {"include_nl": True}}))
        exported = wait(request("POST", p+"/exports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}))
        report = request("GET", exported["report_url"])
        output = project_path / report["directory"]
        assert report["counts"]["exported"] == 2 and report["counts"]["failed"] == 0
        for caption in output.glob("*.txt"):
            assert caption.read_text(encoding="utf-8") == "demo style, green hair, outdoors\n\n通过 HTTP 保存的描述。\n"
        return {"ok": True, "project_id": pid, "project": str(project_path), "export_directory": str(output),
                "imported": imported["imported"], "exported": report["counts"]["exported"], "export_id": exported["export_id"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Existing parent directory for the new demo")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.output), ensure_ascii=False, indent=2))
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
