import hashlib
import json
from unittest.mock import patch
from uuid import uuid4

from stylelora.api.security import MAX_BODY
from .api_support import APITest


class APISecurityTests(APITest):
    def test_api_media_and_unknown_routes_require_authentication(self):
        image_id = self.import_inputs()["items"][0]["image_id"]
        for path in ("/api/session", "/api/events", "/api/openapi.json", "/api/unknown", self.p+f"/images/{image_id}/content"):
            response = self.client.get(path, headers={"Authorization": "", "If-None-Match": "*"})
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_host_origin_and_duplicate_auth_are_rejected(self):
        for headers in ({"Host": "localhost:8301"}, {"Host": "evil.example"}, {"Origin": "null"},
                        {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.get("/api/session", headers=headers).status_code, 403)
        token = "Bearer " + self.runtime.token
        self.assertEqual(self.client.get("/api/session", headers=[("Authorization", token), ("Authorization", token)]).status_code, 401)
        self.assertEqual(self.client.get("/api/session", headers={"Authorization": b"Bearer \xff"}).status_code, 401)
        self.assertEqual(self.client.get("/api/session", headers={"Origin": self.runtime.origin}).status_code, 200)

    def test_bootstrap_is_origin_bound_one_use_and_expiring(self):
        code = self.runtime.issue_bootstrap().split("#bootstrap=")[1]
        path = "/api/session/bootstrap"
        self.assertEqual(self.client.post(path, json={"code": code}).status_code, 401)
        value = self.client.post(path, json={"code": code}, headers={"Origin": self.runtime.origin})
        self.assertEqual(value.status_code, 200)
        self.assertEqual(value.json()["token"], self.runtime.token)
        self.assertEqual(self.client.post(path, json={"code": code}, headers={"Origin": self.runtime.origin}).status_code, 401)
        code = self.runtime.issue_bootstrap().split("#bootstrap=")[1]
        self.runtime._bootstrap[hashlib.sha256(code.encode()).hexdigest()] = (0, self.runtime.origin)
        self.assertEqual(self.client.post(path, json={"code": code}, headers={"Origin": self.runtime.origin}).status_code, 401)

    def test_invalid_json_and_strict_models_do_not_echo_input(self):
        for body in ('{"grace_seconds":NaN}', '{"grace_seconds":1,"grace_seconds":2}', '{'):
            response = self.client.post("/api/session/shutdown", content=body, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 400)
        for body in ({"grace_seconds": True}, {"grace_seconds": "1"}, {"path": "C:/secret-value"}):
            response = self.client.post("/api/session/shutdown", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("secret-value", response.text)
        self.assertFalse(self.runtime.draining)

    def test_request_size_encoding_and_unknown_fields(self):
        self.assertEqual(self.client.post("/api/projects", content=b"{}", headers={"Content-Length": str(MAX_BODY+1)}).status_code, 413)
        self.assertEqual(self.client.post("/api/projects", content=b"{}", headers={"Content-Length": "-1"}).status_code, 400)
        self.assertEqual(self.client.post("/api/projects", content=b"{}", headers={"Content-Encoding": "gzip"}).status_code, 415)
        self.assertEqual(self.client.post("/api/projects", content=b"{}", headers={"Content-Type": "text/plain"}).status_code, 415)
        response = self.client.post("/api/projects/open", json={"path": str(self.root)})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(str(self.root), response.text)

    def test_preflight_allows_only_exact_configured_development_origin(self):
        self.runtime.dev_origin = "http://127.0.0.1:5173"
        headers = {"Origin": self.runtime.dev_origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Authorization, Idempotency-Key"}
        response = self.client.options("/api/projects", headers=headers)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["access-control-allow-origin"], self.runtime.dev_origin)
        self.assertEqual(self.client.options("/api/projects", headers={**headers, "Origin": "http://localhost:5173"}).status_code, 403)
        self.assertEqual(self.client.options("/api/projects", headers={**headers, "Access-Control-Request-Headers": "x-arbitrary"}).status_code, 403)

    def test_idempotency_key_required_and_grant_scope_revocation(self):
        grant = self.runtime.grant(self.inputs, "import_root", self.pid)
        body = {"root_handle": grant["grant_id"]}
        self.assertEqual(self.client.post(self.p+"/imports/preview", json=body).status_code, 428)
        self.request("POST", "/api/projects/open", {"grant_id": grant["grant_id"]}, status=403)
        self.request("DELETE", "/api/filesystem/grants/"+grant["grant_id"], status=428)
        self.request("DELETE", "/api/filesystem/grants/"+grant["grant_id"], version=grant["version"], status=204)
        self.request("POST", self.p+"/imports/preview", body, status=410)

    def test_errors_do_not_disclose_paths_sql_or_credentials(self):
        with patch.object(self.runtime.dataset, "list_images", side_effect=RuntimeError("SELECT secret FROM " + str(self.root) + self.runtime.token)):
            result = self.client.get(self.p+"/images")
        self.assertEqual(result.status_code, 500)
        self.assertEqual(result.json()["error"]["outcome"], "unknown")
        for secret in ("SELECT", str(self.root), self.runtime.token):
            self.assertNotIn(secret, result.text)
        self.assertEqual(result.json()["error"]["request_id"], result.headers["x-request-id"])
