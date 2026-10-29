import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from PIL import Image

from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import ProductLock, pipe_request
from .support import TEMP_ROOT

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows current-user process integration")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        # Never attach to or shut down a service that the user already started.
        try:
            probe = ProductLock()
        except ServiceError:
            self.skipTest("A user service is already running; stop it before process integration tests")
        probe.close()
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.base, self.process, self.log, self.client = Path(self.temp.name), None, None, None
        # Windows venv python.exe is a redirector with a different child PID.
        # Launch its base interpreter with these exact dependency paths so kill
        # and PID checks always refer to the actual server process owned here.
        paths = [str(REPO / "src"), *[path for path in sys.path if Path(path).name == "site-packages"]]
        self.python = getattr(sys, "_base_executable", sys.executable)
        self.env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "PYTHONIOENCODING": "utf-8"}
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.process and self.process.poll() is None:
            try:
                self.stop()
            except Exception:
                self.process.kill()  # Only the subprocess created by this test.
                self.process.wait(timeout=10)
        if self.client:
            self.client.close()
        if self.log:
            self.log.close()
        self.temp.cleanup()

    def command(self, *args):
        return subprocess.run([self.python, "-m", "stylelora", *map(str, args)], cwd=REPO, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", timeout=15,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def start(self, project=None):
        self.state = self.base / "runtime"
        if self.log:
            self.log.close()
        self.log = (self.base / "server.log").open("ab")
        command = [self.python, "-m", "stylelora", "serve", "--no-browser", "--runtime-dir", str(self.state)]
        if project:
            command += ["--project", str(project)]
        self.process = subprocess.Popen(command, cwd=REPO, env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic()+12
        locator = self.state / "instance.json"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail((self.base / "server.log").read_text(encoding="utf-8"))
            try:
                info = json.loads(locator.read_text(encoding="utf-8"))
            except (FileNotFoundError, PermissionError):
                # The Windows locator may be briefly unavailable while published.
                # A permanent failure still times out; process exit still fails above.
                pass
            else:
                if info["pid"] == self.process.pid:
                    break
            time.sleep(.02)
        else:
            self.fail("Local server did not become ready: " + (self.base / "server.log").read_text(encoding="utf-8"))
        self.credentials = pipe_request({"action": "hello"})
        self.assertEqual(self.credentials["pid"], self.process.pid)
        if self.client:
            self.client.close()
        self.client = httpx.Client(base_url="http://"+self.credentials["authority"], trust_env=False, timeout=5,
                                   headers={"Authorization": "Bearer "+self.credentials["token"]})
        return info

    def request(self, method, path, body=None, *, status=200, version=None):
        headers = {"Idempotency-Key": str(uuid4())}
        if version:
            headers["If-Match"] = '"'+version+'"'
        result = self.client.request(method, path, json=body, headers=headers)
        self.assertEqual(result.status_code, status, result.text)
        return result.json()

    def wait(self, accepted):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            result = self.request("GET", accepted["status_url"])
            if result["status"] in ("done", "finished", "failed", "canceled", "interrupted"):
                self.assertIn(result["status"], ("done", "finished"), result)
                return result.get("result") or result
            time.sleep(.01)
        self.fail("Task did not finish")

    def stop(self):
        session = self.request("GET", "/api/session")
        self.request("POST", "/api/session/shutdown", {}, status=202, version=session["version"])
        self.assertEqual(self.process.wait(timeout=12), 0)
        self.assertFalse((self.state / "instance.json").exists())

    def create(self):
        root = self.base / "process.slproj"
        grant = self.command("grant", "project_create", root)
        self.assertEqual(grant.returncode, 0, grant.stderr)
        value = json.loads(grant.stdout)
        self.assertNotIn(str(self.base), grant.stdout)
        project = self.wait(self.request("POST", "/api/projects", {"grant_id": value["grant_id"], "name": "Process", "trigger_word": "test_style"}, status=202))
        return root, "/api/projects/"+project["project_id"], project["project_id"]

    def project_cli(self, *args, code=0):
        result = self.command('project', *args)
        self.assertEqual(result.returncode, code, result.stdout+result.stderr)
        if hasattr(self, 'credentials'):
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
        return json.loads(result.stdout)

    def test_project_cli_standalone_and_attached_lifecycle(self):
        root = self.base/'命令行.slproj'
        self.project_cli('init', root, '--name', 'without service', code=3)
        self.assertFalse(root.exists())
        standalone = self.project_cli('init', root, '--name', '独立项目', '--trigger', 'cli_style', '--standalone')
        self.assertTrue(standalone['closed'])
        self.start()
        blocked = self.base/'blocked.slproj'
        self.project_cli('init', blocked, '--name', 'blocked', '--standalone', code=4)
        self.assertFalse(blocked.exists())
        self.assertIsNone(self.project_cli('status')['project'])
        accepted = self.project_cli('open', root, '--detach')
        opened = self.project_cli('status', '--operation-id', accepted['cli']['operation_id'], '--wait')['result']
        self.assertEqual(opened['metadata']['trigger_word'], 'cli_style')
        self.assertEqual(self.request('GET', '/api/projects/current')['project_id'], opened['project_id'])
        self.project_cli('close')
        created = self.project_cli('init', self.base/'attached.slproj', '--name', 'Attached')['result']
        self.assertEqual(self.project_cli('status')['project']['project_id'], created['project_id'])
        self.project_cli('close')
        self.assertIsNone(self.request('GET', '/api/projects/current'))
        self.stop()

    def test_project_cli_replay_is_rejected_after_service_restart(self):
        self.start()
        first = self.project_cli('init', self.base/'replay.slproj', '--name', 'Replay')
        info = first['cli']
        self.stop()
        self.client.close()
        self.start()
        denied = self.project_cli('init', '--grant-id', info['grant_id'], '--name', 'Replay',
                                  '--instance-id', info['instance_id'], '--idempotency-key', info['request']['idempotency_key'], code=4)
        self.assertEqual(denied['code'], 'instance:changed')
        # The control pipe must also reject a stale session before granting a path.
        with self.assertRaises(ServiceError) as error:
            pipe_request({'action': 'grant', 'purpose': 'project_create', 'path': str(self.base/'stale.slproj'),
                          'instance_id': info['instance_id']})
        self.assertEqual(error.exception.code, 'instance:changed')
        self.assertIsNone(self.request('GET', '/api/projects/current'))
        self.assertFalse((self.base/'stale.slproj').exists())
        self.stop()

    def test_job_cli_preview_run_replay_status_and_restart_over_real_http(self):
        self.start()
        root, _, pid = self.create()
        # An empty project proves the real CLI/HTTP lifecycle without external inference.
        # The configured endpoint is deliberately unreachable and never contacted.
        ep = self.request('POST', '/api/llm/endpoints', {'base_url': 'http://127.0.0.1:9/v1', 'protocol': 'openai_chat',
                          'models': [{'name': 'empty-test', 'supports_images': True}]}, status=201)
        self.request('POST', '/api/llm/endpoints/'+ep['endpoint_id']+'/tokens', {'value': 'empty-test-token'}, status=201)
        roles = self.request('GET', '/api/llm/roles')
        self.request('PUT', '/api/llm/roles', {'roles': {'vision': {'endpoint_id': ep['endpoint_id'], 'model': 'empty-test'}}}, version=roles['version'])
        def command(*args, expected=0):
            result = self.command('job', *args)
            self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
            self.assertNotIn('empty-test-token', result.stdout+result.stderr)
            return json.loads(result.stdout)
        preview = command('preview', '--executor', 'llm_vision')
        self.assertEqual(preview['plan']['preview']['image_count'], 0)
        self.assertEqual(self.request('GET', '/api/projects/'+pid+'/jobs')['items'], [])
        plan_path = self.base/'job-preview.json'
        plan_path.write_text(json.dumps(preview), encoding='utf-8')
        arguments = ['run', '--preview', plan_path, '--confirm-digest', preview['confirmation_digest']]
        accepted = command(*arguments, '--detach')
        bid = accepted['batch_id']
        result = command('status', '--batch-id', bid, '--project-id', pid, '--wait')
        self.assertEqual(result['status'], 'done')
        self.assertEqual(command(*arguments)['batch_id'], bid)
        self.assertEqual(len(command('list')['items']), 1)
        self.stop()
        self.start(root)
        denied = command(*arguments, expected=4)
        self.assertEqual(denied['code'], 'instance:changed')
        new_pid = self.request('GET', '/api/projects/current')['project_id']
        self.assertEqual(command('status', '--batch-id', bid, '--project-id', new_pid)['status'], 'done')
        self.stop()

    def test_prompt_cli_product_tasks_and_seed_projection_survive_restart(self):
        self.start()
        identifier = str(uuid4())
        self.request('POST', '/api/prompt-production/dictionaries', {'dictionary_id': identifier, 'kind': 'prompts',
                     'name': 'Local CLI', 'entries': [{'tags': ['no humans', 'forest'], 'rating': 'general', 'aspect': 'landscape'}]}, status=201)
        spec = self.base/'sampling.json'
        spec.write_text(json.dumps({'prompt_dictionary_ids': [identifier], 'coverage_enabled': False,
                                   'plan': {'target_count': 1, 'random_seed': 7}}), encoding='utf-8')
        def command(*args, expected=0):
            result = self.command('prompt', *args)
            self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
            return json.loads(result.stdout)
        preview = command('preview', '--spec', spec)
        self.assertEqual(self.request('GET', '/api/prompt-production/tasks')['items'], [])
        plan = self.base/'prompt-plan.json'
        plan.write_text(json.dumps(preview), encoding='utf-8')
        arguments = ['run', '--spec', plan, '--confirm-digest', preview['confirmation_digest']]
        accepted = command(*arguments, '--detach')
        tid = accepted['task_id']
        done = command('status', tid, '--wait')
        self.assertTrue(done['ready'])
        content = command('seeds', tid, '--expected-revision', done['revision'])
        self.assertEqual(set(content), {'items', 'count', 'digest'})
        self.assertEqual(set(content['items'][0]), {'seq', 'prompt', 'aspect'})
        self.assertEqual(command(*arguments)['task_id'], tid)
        self.assertEqual(len(command('list')['items']), 1)
        self.assertIsNone(self.request('GET', '/api/projects/current'))
        self.stop()
        self.start()
        current = command('status', tid)
        self.assertEqual(command('seeds', tid, '--expected-revision', current['revision']), content)
        self.assertEqual(command(*arguments, expected=4)['code'], 'instance:changed')
        self.assertEqual(len(command('list')['items']), 1)
        self.assertIsNone(self.request('GET', '/api/projects/current'))
        self.stop()

    def test_source_cli_drafts_cost_policy_and_no_adapter_gate_over_real_http(self):
        from .test_generation_handoff import draft
        self.start()
        def command(*args, expected=0):
            result = self.command('source', *args)
            self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
            return json.loads(result.stdout)
        self.assertFalse(command('capabilities')['generation'])
        root, _, pid = self.create()
        spec = self.base/'source.json'
        spec.write_text(json.dumps(draft()), encoding='utf-8')
        created = command('create', '--type', 'nai', '--spec', spec, '--project-id', pid)
        sid = created['source_id']
        self.assertEqual(command('list')['items'][0]['source_id'], sid)
        self.assertEqual(command('status', sid, '--project-id', pid, expected=7)['status'], 'draft')
        self.assertEqual(command('items', sid, '--project-id', pid)['items'], [])
        changed = command('set-cost-policy', sid, '--project-id', pid, '--policy', 'deny')
        self.assertEqual(changed['cost_policy'], 'deny')
        self.assertFalse(command('cost-state', sid, '--project-id', pid)['can_confirm'])
        command('set-cost-policy', sid, '--project-id', pid, '--policy', 'ask', '--if-match', created['version'], expected=4)
        self.stop()
        self.start(root)
        new_pid = self.request('GET', '/api/projects/current')['project_id']
        self.assertEqual(command('status', sid, '--project-id', new_pid, expected=7)['cost_policy'], 'deny')
        command('create', '--type', 'nai', '--spec', spec, '--project-id', new_pid,
                '--instance-id', created['cli']['instance_id'], '--idempotency-key', created['cli']['request']['idempotency_key'], expected=4)
        self.assertEqual(len(command('list')['items']), 1)
        self.stop()

    def test_source_edit_and_failed_item_retry_over_real_http_survive_restart(self):
        from .test_generation_handoff import draft
        from stylelora.project_db import commands
        from stylelora.project_db.connection import submit_write
        from stylelora.project_db.project import open_project, close_project
        from stylelora.project_db.types import ItemSeed
        original_read, denied = Path.read_text, []
        def read_locator(path, *args, **kwargs):
            if path == self.base/'runtime'/'instance.json' and path.is_file() and not denied:
                denied.append(True)
                raise PermissionError('Transient publication failure')
            return original_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read_locator):
            self.start()
        self.assertEqual(denied, [True])
        def command(*args, expected=0):
            result = self.command('source', *args)
            self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
            return json.loads(result.stdout)
        root, _, pid = self.create()
        spec = self.base/'source.json'
        spec.write_text(json.dumps(draft(1)), encoding='utf-8')
        created = command('create', '--type', 'nai', '--spec', spec, '--project-id', pid)
        sid = created['source_id']
        spec.write_text(json.dumps({**draft(1), 'name': 'Edited through HTTP'}), encoding='utf-8')
        updated = command('update', sid, '--spec', spec, '--project-id', pid, '--if-match', created['version'])
        self.assertEqual(updated['name'], 'Edited through HTTP')
        command('update', sid, '--spec', spec, '--project-id', pid, '--if-match', created['version'], expected=4)
        self.stop()
        # Construct a failed attempt in this test's closed project using F01;
        # production never receives a fake adapter or a generation request.
        project = open_project(root)
        try:
            def seed(db):
                commands.start_source(db, sid, [ItemSeed(1, 'fixture content')])
                item_id = db.execute('SELECT item_id FROM source_items WHERE source_id=?', (sid,)).fetchone()[0]
                commands.mark_sent(db, item_id)
                commands.fail_item(db, item_id, attempt=1, error='fixture:failed')
                commands.transition_source(db, sid, expected='running', target='done', reason='fixture')
                return item_id
            item_id = submit_write(project, seed).result()
        finally:
            close_project(project)
        self.start(root)
        pid = self.request('GET', '/api/projects/current')['project_id']
        item = command('item', item_id, '--project-id', pid)
        result = command('retry-item', item_id, '--project-id', pid, '--expected-attempt', 1, '--if-match', item['version'])
        receipt = result['cli']['request']
        replay = command('retry-item', item_id, '--project-id', pid, '--expected-attempt', 1,
                         '--if-match', item['version'], '--instance-id', result['cli']['instance_id'], '--idempotency-key', receipt['idempotency_key'])
        self.assertEqual((replay['status'], replay['attempts']), ('pending', 2))
        self.assertEqual(command('status', sid, '--project-id', pid, expected=7)['status'], 'paused')
        self.stop()
        self.start(root)
        new_pid = self.request('GET', '/api/projects/current')['project_id']
        current = command('item', item_id, '--project-id', new_pid)
        self.assertEqual((current['status'], current['attempts']), ('pending', 2))
        self.assertEqual(command('status', sid, '--project-id', new_pid, expected=7)['name'], updated['name'])
        command('retry-item', item_id, '--project-id', new_pid, '--expected-attempt', 1, '--if-match', item['version'],
                '--instance-id', result['cli']['instance_id'], '--idempotency-key', receipt['idempotency_key'], expected=4)
        # An explicit new request with the observed current version only reconciles the same attempt.
        self.assertEqual(command('retry-item', item_id, '--project-id', new_pid, '--expected-attempt', 1,
                                 '--if-match', current['version'])['attempts'], 2)
        self.stop()

    def test_import_cli_http_loop_and_explicit_root_verification_after_restart(self):
        self.start()
        root, _, pid = self.create()
        inputs = self.base/'input-pictures'
        inputs.mkdir()
        Image.new('RGB', (12, 8), 'blue').save(inputs/'one.png')
        (inputs/'one.txt').write_text('blue_hair, smile\n\n原始描述。', encoding='utf-8')
        before = {p.name: p.read_bytes() for p in inputs.iterdir()}
        def command(group, *args, expected=0):
            result = self.command(group, *args)
            self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
            self.assertNotIn(str(inputs), result.stdout+result.stderr)
            return json.loads(result.stdout)
        preview = command('import', 'preview', inputs, '--project-id', pid, '--name', '本机导入')
        self.assertEqual(self.request('GET', '/api/projects/'+pid+'/sources')['items'], [])
        plan = self.base/'import-plan.json'
        plan.write_text(json.dumps(preview), encoding='utf-8')
        args = ['run', '--preview', plan, '--confirm-digest', preview['confirmation_digest']]
        accepted = command('import', *args, '--detach')
        op = accepted['cli']['operation_id']
        done = command('import', 'status', op, '--project-id', pid, '--wait')
        sid = done['result']['source_id']
        self.assertEqual(done['result']['imported'], 1)
        self.assertEqual(command('import', *args)['operation_id'], op)
        self.assertEqual(command('import', 'summary', sid, '--project-id', pid)['imported'], 1)
        self.stop()
        self.start(root)
        pid = self.request('GET', '/api/projects/current')['project_id']
        self.assertEqual(command('import', *args, expected=4)['code'], 'instance:changed')
        command('import', 'status', op, '--project-id', pid, expected=4)
        self.assertEqual(command('import', 'summary', sid, '--project-id', pid)['imported'], 1)
        self.assertEqual(command('source', 'resume', sid, '--project-id', pid, expected=4)['code'], 'import:root_unavailable')
        verified = command('source', 'import-root', sid, inputs, '--project-id', pid)
        self.assertTrue(verified['result']['verified'])
        resumed = command('source', 'resume', sid, '--project-id', pid)
        self.assertEqual(resumed['result']['imported'], 1)
        self.assertEqual(len(command('source', 'items', sid, '--project-id', pid)['items']), 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in inputs.iterdir()})
        self.stop()

    def test_export_cli_http_import_edit_export_loop_and_report_after_restart(self):
        self.start()
        root, prefix, pid = self.create()
        inputs = self.base/'pictures'
        inputs.mkdir()
        Image.new('RGB', (12, 8), 'purple').save(inputs/'one.png')
        (inputs/'one.txt').write_text('purple_hair, smile', encoding='utf-8')
        original = (inputs/'one.png').read_bytes()
        def command(group, *args, expected=0):
            result = self.command(group, *args)
            self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
            return json.loads(result.stdout)
        def save_plan(value, name):
            path = self.base/name
            path.write_text(json.dumps(value), encoding='utf-8')
            return path
        incoming = command('import', 'preview', inputs, '--project-id', pid)
        imported = command('import', 'run', '--preview', save_plan(incoming, 'import.json'), '--confirm-digest', incoming['confirmation_digest'])
        image_id = imported['result']['items'][0]['image_id']
        annotation = self.request('GET', prefix+f'/annotations/{image_id}')
        self.request('PUT', prefix+f'/annotations/{image_id}/manual', {'tags': [{'tag': 'custom_tag'}],
                     'natural_language': 'HTTP 编辑描述。', 'write_nl': True, 'read_set': annotation['refs']}, version=annotation['version'])
        item = self.request('GET', prefix+f'/images/{image_id}')
        self.request('POST', prefix+'/images/review', {'ids': [image_id], 'target': 'keep', 'expected_versions': {str(image_id): item['version']}})
        options = self.base/'options.json'
        options.write_text('{"include_nl":true}', encoding='utf-8')
        preview = command('export', 'preview', '--project-id', pid, '--options', options)
        self.assertFalse(list((root/'exports').iterdir()))
        plan = save_plan(preview, 'export.json')
        args = ['run', '--preview', plan, '--confirm-digest', preview['confirmation_digest']]
        accepted = command('export', *args, '--detach')
        eid = accepted['export_id']
        done = command('export', 'status', eid, '--project-id', pid, '--wait')
        self.assertEqual((done['status'], done['exported']), ('finished', 1))
        report = command('export', 'report', eid, '--project-id', pid)
        folder = root/report['directory']
        self.assertEqual(next(folder.glob('*.png')).read_bytes(), original)
        self.assertEqual(next(folder.glob('*.txt')).read_text(encoding='utf-8'), 'test style, custom tag\n\nHTTP 编辑描述。\n')
        self.assertEqual(command('export', *args)['export_id'], eid)
        self.assertEqual(len(command('export', 'list')['items']), 1)
        self.stop()
        self.start(root)
        pid = self.request('GET', '/api/projects/current')['project_id']
        self.assertEqual(command('export', *args, expected=4)['code'], 'instance:changed')
        recovered = command('export', 'status', eid, '--project-id', pid)
        self.assertIsNone(recovered['operation_id'])
        self.assertEqual(recovered['status'], 'finished')
        current = command('export', 'report', eid, '--project-id', pid)
        self.assertEqual({k: v for k, v in report.items() if k != 'cli'}, {k: v for k, v in current.items() if k != 'cli'})
        self.assertEqual(command('export', 'list')['items'][0]['export_id'], eid)
        self.assertEqual((inputs/'one.png').read_bytes(), original)
        self.stop()

    def test_doctor_offline_and_live_service_are_read_only_and_do_not_attach(self):
        fresh = self.base/'fresh-runtime'
        result = self.command('doctor', '--runtime-dir', fresh, '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(fresh.exists())
        self.assertEqual(result.stderr, '')
        self.start()
        root, path, pid = self.create()
        original = {name: (root/name).read_bytes() for name in ('project.json', 'project.db', 'project.db-wal')}
        result = self.command('doctor', '--runtime-dir', self.state, '--project', root, '--json')
        self.assertEqual(result.returncode, 4, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        checks = {row['id']: row for row in report['checks']}
        self.assertEqual(checks['instance']['code'], 'instance:running')
        self.assertFalse(checks['instance']['details']['attached'])
        self.assertEqual(checks['project']['code'], 'project:locked')
        self.assertFalse(report['network_probed'])
        self.assertNotIn(self.credentials['token'], result.stdout + result.stderr)
        self.assertNotIn(str(self.base), result.stdout + result.stderr)
        self.assertEqual(self.request('GET', '/api/projects/current')['project_id'], pid)
        self.assertEqual(original, {name: (root/name).read_bytes() for name in original})
        self.stop()
        after = self.command('doctor', '--runtime-dir', self.state, '--project', root)
        self.assertEqual(after.returncode, 0, after.stderr + after.stdout)
        self.assertFalse((self.state/'instance.json').exists())

    def test_real_http_cli_sse_shutdown_restart_and_report_recovery(self):
        info = self.start()
        self.assertGreater(info["port"], 0)
        for _ in range(12):
            self.assertEqual(pipe_request({"action": "hello"})["instance_id"], info["instance_id"])
        empty = self.command("api", "GET", "/api/projects/current")
        self.assertEqual((empty.returncode, json.loads(empty.stdout)), (0, None))
        attached = self.command("serve", "--no-browser", "--runtime-dir", self.base / "unused")
        self.assertEqual(attached.returncode, 0, attached.stderr)
        self.assertTrue(json.loads(attached.stdout)["attached"])
        self.assertFalse((self.base / "unused").exists())
        root, path, pid = self.create()
        inputs = self.base / "inputs"
        inputs.mkdir()
        Image.new("RGB", (8, 9), "blue").save(inputs / "sample.png")
        (inputs / "sample.txt").write_text("blue_hair, smile", encoding="utf-8")
        grant = json.loads(self.command("grant", "import_root", inputs, "--project-id", pid).stdout)
        preview = self.wait(self.request("POST", path+"/imports/preview", {"root_handle": grant["grant_id"]}, status=202))
        report = self.wait(self.request("POST", path+"/imports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202))
        image_id = report["items"][0]["image_id"]
        annotations = self.request("GET", path+f"/annotations/{image_id}")
        with self.client.stream("GET", "/api/events?topics=annotations&project_id="+pid) as response:
            self.assertEqual(response.status_code, 200)
            lines = response.iter_lines()
            while next(lines) != "event: stream.ready":
                pass
            self.request("PUT", path+f"/annotations/{image_id}/manual", {"tags": [{"tag": "green_hair"}], "read_set": annotations["refs"]}, version=annotations["version"])
            while next(lines) != "event: annotations.changed":
                pass
            event = json.loads(next(lines).removeprefix("data: "))
            self.assertEqual(event["payload"]["image_ids"], [image_id])
        image = self.request("GET", path+f"/images/{image_id}")
        self.assertEqual(self.client.get(image["thumbnail_url"]).headers["content-type"], "image/webp")
        self.request("POST", path+"/images/review", {"ids": [image_id], "target": "keep", "expected_versions": {str(image_id): image["version"]}})
        preview = self.wait(self.request("POST", path+"/exports/preview", {}, status=202))
        exported = self.wait(self.request("POST", path+"/exports", {"preview_id": preview["preview_id"], "digest": preview["digest"]}, status=202))
        report = self.request("GET", exported["report_url"])
        self.assertEqual(next((root / report["directory"]).glob("*.txt")).read_text(encoding="utf-8"), "test style, green hair\n")
        offline = self.command("init", self.base / "forbidden.slproj", "--name", "Forbidden", "--trigger-word", "x")
        self.assertNotEqual(offline.returncode, 0)
        self.assertIn("service is running", offline.stderr)
        self.assertFalse((self.base / "forbidden.slproj").exists())
        old = self.credentials.copy()
        locator_text = (self.state / "instance.json").read_text(encoding="utf-8")
        self.assertNotIn(old["token"], locator_text)
        self.assertNotIn("token", locator_text)
        self.stop()
        self.start(root)
        self.assertNotEqual(old["token"], self.credentials["token"])
        self.assertEqual(self.client.get("/api/session", headers={"Authorization": "Bearer "+old["token"]}).status_code, 401)
        self.request("GET", path+"/images", status=410)
        current = self.request("GET", "/api/projects/current")
        path = "/api/projects/"+current["project_id"]
        self.assertEqual(self.request("GET", path+"/images")["total"], 1)
        restored = self.request("GET", path+"/exports")["items"][0]
        self.assertEqual(restored["export_id"], exported["export_id"])
        self.request("GET", restored["report_url"])
        self.stop()
        self.assertNotIn(old["token"], (self.base / "server.log").read_text(encoding="utf-8"))

    def test_startup_rejects_multiple_workers_reload_and_non_loopback(self):
        for args in (("--workers", "2"), ("--reload",), ("--host", "0.0.0.0"), ("--port", "-1")):
            result = self.command("serve", "--no-browser", *args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("process:invalid_options", result.stderr)

    def test_documented_http_demo_uses_real_service(self):
        self.start()
        result = subprocess.run([self.python, str(REPO / "scripts/http_demo.py"), "--output", str(self.base)],
                                cwd=REPO, env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual((report["imported"], report["exported"]), (2, 2))
        self.assertNotIn(self.credentials["token"], result.stdout)
        self.stop()

    def test_crashed_process_releases_singleton_and_rotates_session(self):
        first = self.start()
        root, path, pid = self.create()
        old = self.credentials.copy()
        self.process.kill()
        self.process.wait(timeout=10)
        second = self.start(root)
        self.assertNotEqual(first["instance_id"], second["instance_id"])
        self.assertNotEqual(old["token"], self.credentials["token"])
        self.assertEqual(self.request("GET", "/api/projects/current")["metadata"]["name"], "Process")
        self.stop()

    def test_storage_cli_attaches_scans_previews_confirms_and_reads_receipt(self):
        self.start()
        root, path, pid = self.create()
        target = root/'cache/thumbnails'/('a'*64+'.webp')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'test-cache')
        def storage(*args, expected=0):
            result = self.command('storage', *args)
            self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
            self.assertNotIn(self.credentials['token'], result.stdout+result.stderr)
            self.assertNotIn(str(self.base), result.stdout)
            return json.loads(result.stdout)
        initial = storage('summary', '--json')
        self.assertIsNone(initial['current_project'])
        scan = storage('scan', '--project-id', pid)
        self.assertEqual(scan['result']['categories']['cache'], 10)
        known = storage('scan', '--known', '--detach')
        done = storage('status', known['task']['id'], '--wait')
        page = storage('results', done['operation_id'])
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['project_id'], pid)
        preview = storage('cleanup', '--kind', 'thumbnails')
        self.assertTrue(target.exists())
        self.assertEqual(preview['result']['count'], 1)
        confirmation = ('cleanup', '--project-id', preview['project_id'], '--preview-id', preview['result']['preview_id'],
                        '--confirm-digest', preview['result']['digest'])
        removed = storage(*confirmation)
        self.assertEqual(removed['result']['deleted'], 1)
        self.assertFalse(target.exists())
        self.assertEqual(storage(*confirmation)['operation_id'], removed['operation_id'])
        self.assertEqual(self.request('GET', '/api/projects/current')['project_id'], pid)
        self.stop()

    def test_storage_cli_old_handles_and_previews_cannot_cross_restart(self):
        self.start()
        root, _, pid = self.create()
        target = root/'cache/thumbnails'/('b'*64+'.webp')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'keep')
        result = self.command('storage', 'cleanup', '--kind', 'thumbnails')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        preview = json.loads(result.stdout)['result']
        self.stop()
        missing = self.command('storage', 'scan', '--known')
        self.assertEqual(missing.returncode, 3, missing.stderr)
        self.assertEqual(json.loads(missing.stdout)['code'], 'instance:unavailable')
        self.start(root)
        for project_id in (pid, self.request('GET', '/api/projects/current')['project_id']):
            result = self.command('storage', 'cleanup', '--project-id', project_id, '--preview-id', preview['preview_id'],
                                  '--confirm-digest', preview['digest'])
            self.assertEqual(result.returncode, 4, result.stdout+result.stderr)
        self.assertTrue(target.exists())
        self.stop()
