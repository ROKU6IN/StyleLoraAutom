import contextlib
import io
import json
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from stylelora.cli import main
from stylelora.project_db.project import open_project
from stylelora.project_db.connection import read_snapshot
from stylelora.runtime.instance import ProductLock
from stylelora.runtime.lifecycle import Runtime
from stylelora.runtime.session_cli import dispatch
from stylelora.runtime.standalone import ACTIVE_SESSION
from .api_support import APITest
from .test_nai_direct import DEFAULT_CONFIG


class SessionCLITests(APITest):
    def session(self, commands, *, project=True, expected=0, configure=None):
        self.runtime._close_current()
        output, errors, instances = io.StringIO(), io.StringIO(), []
        def construct(**kw):
            runtime = Runtime(**kw)
            instances.append(runtime)
            if configure:
                configure(runtime)
            return runtime
        commands = iter(commands)
        def readline(limit):
            try:
                command = next(commands)
            except StopIteration:
                return ''
            frames = [json.loads(row) for row in output.getvalue().splitlines()]
            if callable(command):
                command = command(frames)
            return json.dumps(command)+'\n'
        argv = ['session', '--runtime-dir', str(self.base/'runtime')]
        if project:
            argv += ['--project', str(self.root)]
        with patch('stylelora.runtime.lifecycle.Runtime', side_effect=construct), \
                patch('sys.stdin', SimpleNamespace(readline=readline)), \
                patch.object(httpx.HTTPTransport, 'handle_request', side_effect=AssertionError('No socket')), \
                patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('No socket')), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(argv)
        frames = [json.loads(row) for row in output.getvalue().splitlines()]
        self.assertEqual(code, expected, (frames, errors.getvalue()))
        self.assertEqual(frames[-1]['event'], 'closed')
        self.assertTrue(frames[-1]['cli']['service_closed'])
        self.assertIsNone(ACTIVE_SESSION.get())
        for runtime in instances:
            self.assertTrue(runtime.closed)
            self.assertNotIn(runtime.token, output.getvalue()+errors.getvalue())
        owner = ProductLock()
        owner.close()
        with open_project(self.root):
            pass
        return frames

    def test_reviewed_import_export_loop_and_replay_without_daemon(self):
        self.make_input()
        import_plan, export_plan = self.base/'import-plan.json', self.base/'export-plan.json'
        options = self.base/'export-options.json'; options.write_text('{"include_unreviewed":true}', encoding='utf-8')
        def run_import(frames):
            return {'argv': ['import', 'run', '--preview', str(import_plan), '--confirm-digest', frames[-1]['result']['confirmation_digest']]}
        def run_export(frames):
            return {'argv': ['export', 'run', '--preview', str(export_plan), '--confirm-digest', frames[-1]['result']['confirmation_digest']]}
        frames = self.session([
            {'argv': ['import', 'preview', str(self.inputs), '--project-id', '$project_id'], 'save_plan': str(import_plan)},
            run_import,
            {'argv': ['export', 'preview', '--project-id', '$project_id', '--options', str(options)], 'save_plan': str(export_plan)},
            run_export,
            lambda f: {'argv': ['export', 'run', '--preview', str(export_plan), '--confirm-digest', json.loads(export_plan.read_text(encoding='utf-8'))['confirmation_digest']]},
            {'argv': ['export', 'list']},
        ])
        results = [f['result'] for f in frames if f['event'] == 'command']
        self.assertEqual(results[3]['exported'], 1)
        self.assertEqual(results[3]['export_id'], results[4]['export_id'])
        self.assertEqual(len(results[5]['items']), 1)
        self.assertTrue(import_plan.exists())
        with open_project(self.root) as project, read_snapshot(project) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM images').fetchone()[0], 1)
        # A saved plan cannot be rebound to a different owned Runtime.
        self.session([{'argv': ['import', 'run', '--preview', str(import_plan),
            '--confirm-digest', json.loads(import_plan.read_text(encoding='utf-8'))['confirmation_digest']]}], expected=4)

    def test_source_create_and_query_with_session_identity(self):
        spec = self.base/'source.json'
        spec.write_text(json.dumps({'name': 'local draft', 'config': DEFAULT_CONFIG, 'target_count': 1}), encoding='utf-8')
        frames = self.session([
            {'argv': ['source', 'create', '--type', 'nai', '--spec', str(spec), '--project-id', '$project_id']},
            {'argv': ['source', 'list']},
            {'argv': ['job', 'list']},
            {'argv': ['storage', 'scan']},
        ])
        self.assertEqual(frames[2]['result']['items'][0]['name'], 'local draft')

    def test_product_session_needs_no_project_and_eof_closes(self):
        frames = self.session([{'argv': ['prompt', 'list']}], project=False)
        self.assertIsNone(frames[0]['cli']['project_id'])
        self.assertEqual(frames[1]['result']['items'], [])

    def test_product_prompt_preview_run_and_seeds_in_owned_session(self):
        from .test_prompt_production import entry
        dictionary_id = str(uuid4())
        def configure(runtime):
            runtime.prompts.dictionaries.save(dictionary_id, {'kind': 'prompts', 'name': 'session',
                'entries': [entry(['no humans', 'forest'], rating='general')]}, None, str(uuid4()), create=True)
        spec = self.base/'sampling.json'
        spec.write_text(json.dumps({'prompt_dictionary_ids': [dictionary_id], 'coverage_enabled': False,
                                    'plan': {'target_count': 1, 'random_seed': 7}}), encoding='utf-8')
        plan = self.base/'prompt-plan.json'
        frames = self.session([
            {'argv': ['prompt', 'preview', '--spec', str(spec)], 'save_plan': str(plan)},
            lambda f: {'argv': ['prompt', 'run', '--spec', str(plan), '--confirm-digest', f[-1]['result']['confirmation_digest']]},
            lambda f: {'argv': ['prompt', 'seeds', f[-1]['result']['task_id'], '--expected-revision', str(f[-1]['result']['revision'])]},
        ], project=False, configure=configure)
        self.assertEqual(frames[-2]['result']['items'][0]['prompt'], 'no humans, forest')

    def test_cleanup_confirmation_uses_same_owned_project_and_preview(self):
        target = self.root/'cache/thumbnails'/('a'*64+'.webp')
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'fixture')
        frames = self.session([
            {'argv': ['storage', 'cleanup', '--kind', 'thumbnails']},
            lambda f: {'argv': ['storage', 'cleanup', '--project-id', '$project_id',
                '--preview-id', f[-1]['result']['result']['preview_id'], '--confirm-digest', f[-1]['result']['result']['digest']]},
        ])
        self.assertFalse(target.exists())
        self.assertEqual(frames[-2]['result']['result']['deleted'], 1)

    def test_job_preview_submit_outcomes_and_control_in_session(self):
        self.import_inputs()
        plan = self.base/'job-plan.json'
        def configure(runtime):
            from .test_llm import configure as llm_configure, reply
            from stylelora.llm.types import RolesInput
            ep, _ = llm_configure(runtime, models=[{'name': 'example/model', 'supports_images': True}])
            roles = runtime.llm.config.roles()
            runtime.llm.config.save_roles(RolesInput.model_validate({'roles': {
                'vision': {'endpoint_id': ep['endpoint_id'], 'model': 'example/model'}}}), roles['version'])
            runtime.llm.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=reply(
                text=json.dumps({'vision_tags': ['red cup'], 'vision_description': 'A red cup.'}))))
        frames = self.session([
            {'argv': ['job', 'preview', '--executor', 'llm_vision', '--mode', 'all', '--project-id', '$project_id'],
             'save_plan': str(plan)},
            lambda f: {'argv': ['job', 'run', '--preview', str(plan), '--confirm-digest', f[-1]['result']['confirmation_digest']]},
            {'argv': ['job', 'outcomes', '1', '--project-id', '$project_id']},
            {'argv': ['job', 'pause', '1', '--project-id', '$project_id']},
        ], configure=configure, expected=4)
        self.assertEqual(frames[2]['result']['done_count'], 1)
        self.assertEqual(frames[3]['result']['items'][0]['kind'], 'done')
        self.assertEqual(frames[4]['result']['code'], 'job:state_conflict')

    def test_inherited_allow_policy_still_requires_explicit_cli_confirmation(self):
        spec = self.base/'source.json'
        spec.write_text(json.dumps({'name': 'policy', 'config': DEFAULT_CONFIG, 'target_count': 1}), encoding='utf-8')
        def configure(runtime):
            runtime.product_config.update({'cost_policy': 'allow', 'confirm_allow': True}, runtime.product_config.get()['version'])
        frames = self.session([
            {'argv': ['source', 'create', '--type', 'nai', '--spec', str(spec), '--project-id', '$project_id']},
            {'argv': ['source', 'list']},
            {'argv': ['source', 'create', '--type', 'nai', '--spec', str(spec), '--project-id', '$project_id', '--confirm-allow']},
        ], configure=configure, expected=2)
        self.assertEqual(frames[1]['result']['code'], 'source:default_cost_confirmation_required')
        self.assertEqual(frames[2]['result']['items'], [])
        self.assertEqual(frames[3]['result']['cost_policy'], 'allow')

    def test_invalid_commands_detach_and_shell_text_cannot_escape(self):
        frames = self.session([
            {'argv': ['serve', '--no-browser']}, {'argv': ['source', 'list', '--detach']},
            {'argv': ['source', 'list', '--standalone']}, {'argv': ['source', 'list', '--project=elsewhere']},
            {'argv': 'source list; anything'}, {'argv': ['source', 'list'], 'unexpected': True},
            {'argv': ['source', 'list']}, {'exit': True},
            {'argv': ['source', 'create']},
        ], expected=2)
        self.assertEqual(len([f for f in frames if f['event'] == 'command']), 7)
        self.assertEqual(frames[-2]['exit_code'], 0)

    def test_existing_plan_file_is_not_overwritten(self):
        self.make_input()
        target = self.base/'plan.json'; target.write_text('preserve', encoding='utf-8')
        frames = self.session([{'argv': ['import', 'preview', str(self.inputs), '--project-id', '$project_id'],
                               'save_plan': str(target)}], expected=4)
        self.assertFalse(frames[1]['plan_saved'])
        self.assertEqual(target.read_text(encoding='utf-8'), 'preserve')

    def test_ctrl_c_breaks_session_and_closes(self):
        with patch('stylelora.runtime.source_cli.execute', side_effect=KeyboardInterrupt):
            frames = self.session([{'argv': ['source', 'list']}, {'argv': ['job', 'list']}], expected=130)
        self.assertEqual(len([f for f in frames if f['event'] == 'command']), 1)

    def test_unsafe_control_and_lifecycle_routes_are_rejected(self):
        from stylelora.runtime.standalone import InProcessClient, session_control
        from stylelora.runtime.errors import ServiceError
        client = InProcessClient(self.runtime, allow_writes=True)
        for path in ('/api/session/shutdown', '/api/projects', '/api/filesystem/grants',
                     self.p+'/close', 'https://example.com/'):
            with self.assertRaises(ServiceError):
                client.request('POST', path, json={})
        token = ACTIVE_SESSION.set(client)
        try:
            with self.assertRaises(ServiceError):
                session_control({'action': 'grant', 'purpose': 'project_open', 'instance_id': self.runtime.instance_id})
        finally:
            ACTIVE_SESSION.reset(token)
