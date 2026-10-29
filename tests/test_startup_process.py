"""Real Windows startup/pipe/HTTP tests; all product data stays in a temporary root."""
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from stylelora.project_db.project import create_project
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.instance import ProductLock, _windows, pipe_request
from .support import TEMP_ROOT

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows service startup integration')
class StartupProcessTests(unittest.TestCase):
    def setUp(self):
        try:
            probe = ProductLock()
        except ServiceError:
            self.skipTest('An existing user service owns the product lock')
        probe.close()
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.base = Path(self.temp.name)
        self.python = getattr(sys, '_base_executable', sys.executable)
        paths = [str(REPO/'src'), *[path for path in sys.path if Path(path).name == 'site-packages']]
        self.env = {**os.environ, 'PYTHONPATH': os.pathsep.join(paths), 'PYTHONIOENCODING': 'utf-8',
                    'APPDATA': str(self.base/'roaming'), 'LOCALAPPDATA': str(self.base/'local')}
        self.spawned, self.handle, self.client = set(), None, None
        self.kernel, _ = _windows()
        self.kernel.TerminateProcess.argtypes, self.kernel.TerminateProcess.restype = [W.HANDLE, W.UINT], W.BOOL
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.handle:
            try:
                if self.client and self.kernel.WaitForSingleObject(self.handle, 0) == 258:
                    self.client.post('/api/session/shutdown', json={}, headers={
                        'If-Match': '"'+self.credentials['instance_id']+'"', 'Idempotency-Key': str(uuid4())})
            finally:
                if self.kernel.WaitForSingleObject(self.handle, 15000) == 258:
                    # Held process handle belongs to a verified child of this test,
                    # not a PID lookup that could target a later unrelated process.
                    self.kernel.TerminateProcess(self.handle, 1)
                    self.kernel.WaitForSingleObject(self.handle, 5000)
                self.kernel.CloseHandle(self.handle)
        if self.client:
            self.client.close()
        self.temp.cleanup()

    def launch_cli(self, *args):
        return subprocess.Popen([self.python, '-m', 'stylelora', *map(str, args)], cwd=REPO, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                creationflags=subprocess.CREATE_NO_WINDOW)

    def result(self, process):
        output, error = process.communicate(timeout=45)
        self.spawned.update(map(int, re.findall(r'Started background service process ([0-9]+);', error)))
        if self.spawned and self.handle is None:
            self.claim()
        self.assertEqual(process.returncode, 0, error)
        if hasattr(self, 'credentials'):
            self.assertNotIn(self.credentials['token'], output + error)
        return output

    def command(self, *args):
        return self.result(self.launch_cli(*args))

    def claim(self):
        current = pipe_request({'action': 'hello'})
        self.assertIn(current['pid'], self.spawned, 'Refuse to control an unrelated service')
        self.handle = self.kernel.OpenProcess(0x100000 | 1, False, current['pid'])
        self.assertTrue(self.handle)
        self.credentials = current
        self.client = httpx.Client(base_url='http://'+current['authority'], trust_env=False, follow_redirects=False,
                                  timeout=10, headers={'Authorization': 'Bearer '+current['token']})
        self.assertEqual(self.client.get('/api/session').json()['instance_id'], current['instance_id'])

    def test_init_stays_open_after_cli_exit_and_later_command_reuses_server(self):
        root = self.base/'持久项目.slproj'
        first = json.loads(self.command('project', 'init', root, '--name', 'Startup', '--start-service'))
        self.assertEqual(first['cli']['service_start']['status'], 'ready')
        current = json.loads(self.command('project', 'status'))
        self.assertEqual(current['project']['project_id'], first['result']['project_id'])
        with create_project(self.base/'second.slproj', name='Second', trigger_word='second_style'):
            pass
        second = json.loads(self.command('project', 'open', self.base/'second.slproj', '--start-service'))
        self.assertNotIn('spawned_pid', second['cli']['service_start'])
        self.assertEqual(second['cli']['instance_id'], first['cli']['instance_id'])
        self.assertNotEqual(second['result']['project_id'], first['result']['project_id'])
        self.assertEqual(len(self.spawned), 1)

    def test_open_launches_service_and_waits_for_detached_project_operation(self):
        root = self.base/'open.slproj'
        with create_project(root, name='Before startup', trigger_word='open_style'):
            pass
        opened = json.loads(self.command('project', 'open', root, '--start-service', '--detach'))
        self.assertEqual(opened['status'], 'accepted')
        finished = json.loads(self.command('project', 'status', '--operation-id', opened['cli']['operation_id'], '--wait'))
        self.assertEqual(finished['status'], 'done')
        self.assertEqual(self.client.get('/api/projects/current').json()['metadata']['name'], 'Before startup')
        self.assertEqual(self.client.get('/api/projects/'+finished['result']['project_id']+'/sources').json()['items'], [])

    def test_concurrent_ui_clients_share_one_server_and_distinct_bootstrap_links(self):
        # Both clients first discover absence, then race the normal product mutex.
        clients = [self.launch_cli('ui', '--start-service', '--print-link') for _ in range(2)]
        results = [process.communicate(timeout=45) for process in clients]
        for _, error in results:
            self.spawned.update(map(int, re.findall(r'Started background service process ([0-9]+);', error)))
        self.claim()
        links = []
        for process, (output, error) in zip(clients, results):
            self.assertEqual(process.returncode, 0, error)
            self.assertNotIn(self.credentials['token'], output+error)
            links.append(urlsplit(output.strip()))
        self.assertEqual({link.netloc for link in links}, {self.credentials['authority']})
        self.assertEqual(len({link.fragment for link in links}), 2)
        self.assertTrue(all(link.fragment.startswith('bootstrap=') for link in links))
        self.assertIsNone(self.client.get('/api/projects/current').json())
        self.assertEqual(json.loads((self.base/'local/StyleLoraAutom/runtime/instance.json').read_text())['pid'], self.credentials['pid'])
