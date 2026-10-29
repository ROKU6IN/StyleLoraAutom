import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from uuid import uuid4

from stylelora.cli import main
from stylelora.generation.settings import SettingsJournal
from stylelora.project_db.project import create_project
from stylelora.project_db.storage import atomic_json
from stylelora.runtime import doctor
from stylelora.runtime.errors import ServiceError
from stylelora.runtime.paths import locations
from .support import TEMP_ROOT
from .tagger_support import fixture


def snapshot(root, ignore=()):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file() and p.name not in ignore}


class DoctorTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.paths = locations(self.base/'runtime')
        for name in ('instance_status', 'dpapi_capability'):
            mock = patch.object(doctor, name, return_value=doctor.result())
            mock.start()
            self.addCleanup(mock.stop)

    def run_doctor(self, **kwargs):
        return doctor.diagnose(runtime_dir=self.paths.runtime, **kwargs)

    def check(self, report, key):
        return next(row for row in report['checks'] if row['id'] == key)

    def project(self):
        root = self.base/'样例.slproj'
        with create_project(root, name='Private project text', trigger_word='secret_style'):
            pass
        return root

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['doctor', '--runtime-dir', str(self.paths.runtime), *map(str, args)])
        return code, json.loads(out.getvalue()), out.getvalue(), err.getvalue()

    def test_fresh_install_defaults_without_creating_any_files_or_network(self):
        with patch('socket.socket', side_effect=AssertionError('network must not run')), \
             patch('stylelora.runtime.lifecycle.Runtime', side_effect=AssertionError('no runtime')), \
             patch('stylelora.project_db.project.open_project', side_effect=AssertionError('no recovery')):
            before = snapshot(self.base)
            code, report, raw, err = self.invoke('--json')
        self.assertEqual(code, 0, report)
        self.assertEqual(before, snapshot(self.base))
        self.assertFalse(self.paths.runtime.exists())
        self.assertFalse(report['network_probed'])
        self.assertFalse(report['repairs_performed'])
        self.assertEqual(self.check(report, 'config.product')['code'], 'config:defaults')
        self.assertEqual(self.check(report, 'model.1')['code'], 'tagger:not_installed')
        self.assertNotIn(str(self.base), raw)
        self.assertEqual(err, '')

    def test_path_selection_matches_default_and_custom_serve_layout(self):
        env = {'APPDATA': str(self.base/'roaming'), 'LOCALAPPDATA': str(self.base/'local')}
        default = locations(environ=env)
        self.assertEqual(default.config, self.base/'roaming'/'StyleLoraAutom')
        self.assertEqual(default.product, default.config/'tagging')
        self.assertEqual(default.private, self.base/'local'/'StyleLoraAutom')
        self.assertEqual(default.models, default.private/'models'/'wd')
        self.assertEqual(self.paths.config, self.paths.runtime/'product'/'llm')
        custom = locations(product_dir=self.base/'custom', environ=env)
        self.assertEqual(custom.config, self.base/'custom'/'llm')
        self.assertEqual(custom.private, self.base/'custom'/'private')
        both = locations(self.base/'state', self.base/'custom', environ={})
        self.assertEqual(both.models, self.base/'models'/'wd')
        self.assertEqual(list(self.base.iterdir()), [])

    def test_missing_environment_reports_failure_without_environment_values(self):
        with patch.dict(os.environ, {}, clear=True):
            report = doctor.diagnose()
        self.assertEqual(report['exit_code'], 5)
        self.assertEqual(self.check(report, 'locations')['code'], 'config:locations_unavailable')

    def test_each_corrupt_configuration_is_independent_and_remains_untouched(self):
        for key, path in (
            ('product', self.paths.config/'product-config.json'),
            ('llm', self.paths.config/'llm.json'),
            ('secrets', self.paths.private/'secrets.json'),
            ('vision', self.paths.config/'vision-policy'/'current.json'),
            ('presets', self.paths.config/'generation_presets.json'),
            ('recent', self.paths.config/'known-projects.json'),
            ('catalog', self.paths.product/'tagging-library.json'),
        ):
            with self.subTest(key=key):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'{"secret":"DO_NOT_ECHO_BEARER", "bad":')
                before = snapshot(self.base)
                report = self.run_doctor()
                self.assertEqual(self.check(report, 'config.'+key)['exit_code'], 5)
                self.assertEqual(before, snapshot(self.base))
                self.assertNotIn('DO_NOT_ECHO_BEARER', json.dumps(report))
                self.assertEqual(self.check(report, 'sqlite')['exit_code'], 0)
                path.unlink()

    def test_duplicate_keys_and_oversized_files_fail_without_reading_unbounded_data(self):
        path = self.paths.config/'product-config.json'
        path.parent.mkdir(parents=True)
        for data in (b'{"content":{},"content":{},"digest":"bad"}', b'x'*16385):
            path.write_bytes(data)
            report = self.run_doctor()
            self.assertEqual(self.check(report, 'config.product')['exit_code'], 5)
            self.assertEqual(path.read_bytes(), data)

    def test_valid_configs_use_existing_validators_without_emitting_contents(self):
        from stylelora.services.product_config import ProductConfig, DEFAULTS
        from stylelora.project_db.validation import digest
        from stylelora.services.recent_projects import MAX_BYTES
        from types import SimpleNamespace
        store = ProductConfig(SimpleNamespace(emit=lambda *a, **k: None), self.paths.config)
        store.update({'storage_thresholds': {'project_bytes': DEFAULTS['project_bytes']+1024**2}}, store.get()['version'])
        state = {'schema_version': 1, 'revision': 0, 'projects': {}}
        recent = self.paths.config/'known-projects.json'
        raw = json.dumps({'content': state, 'digest': digest(state)}).encode()
        # Match the existing reader's exact 2 MiB cap, not a smaller doctor cap.
        recent.write_bytes(raw + b' '*(MAX_BYTES-len(raw)))
        before = snapshot(self.base)
        report = self.run_doctor()
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(self.check(report, 'config.product')['code'], 'config:valid')
        self.assertEqual(self.check(report, 'config.recent')['code'], 'config:valid')
        self.assertNotIn('storage_thresholds', json.dumps(report))
        self.assertEqual(before, snapshot(self.base))
        with recent.open('ab') as stream:
            stream.write(b' ')
        self.assertEqual(self.check(self.run_doctor(), 'config.recent')['exit_code'], 5)

    def test_sqlite_check_only_connects_to_memory(self):
        connect = sqlite3.connect
        root = self.project()
        with patch.object(doctor.sqlite3, 'connect', wraps=connect) as opened:
            report = self.run_doctor(project=root)
        self.assertEqual(opened.call_args_list[0].args, (':memory:',))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(report['exit_code'], 0, report)
        self.assertFalse(self.check(report, 'project')['details']['integrity_checked'])

    def test_old_sqlite_or_failed_capability_does_not_skip_other_checks(self):
        with patch.object(doctor.sqlite3, 'sqlite_version_info', (3, 37, 0)):
            report = self.run_doctor()
        self.assertEqual(self.check(report, 'sqlite')['code'], 'doctor:sqlite_too_old')
        self.assertEqual(self.check(report, 'config.product')['exit_code'], 0)

    def test_existing_project_has_no_recovery_or_database_side_effects(self):
        root = self.project()
        before = snapshot(self.base)
        code, report, raw, _ = self.invoke('--project', root)
        self.assertEqual(code, 0, report)
        self.assertEqual(before, snapshot(self.base))
        self.assertNotIn('Private project text', raw)
        self.assertNotIn('secret_style', raw)
        self.assertFalse(self.check(report, 'project')['details']['opened'])

    def test_missing_project_and_lock_file_are_not_created(self):
        absent = self.base/'absent.slproj'
        self.assertEqual(self.run_doctor(project=absent)['exit_code'], 5)
        self.assertFalse(absent.exists())
        root = self.project()
        (root/'.project.lock').unlink()
        before = snapshot(self.base)
        self.assertEqual(self.run_doctor(project=root)['exit_code'], 5)
        self.assertEqual(before, snapshot(self.base))

    def test_running_project_is_reported_busy_and_remains_open(self):
        with create_project(self.base/'busy.slproj', name='busy', trigger_word='') as project:
            before = snapshot(self.base, ignore=('.project.lock',))
            report = self.run_doctor(project=project.root)
            self.assertEqual(report['exit_code'], 4, report)
            self.assertEqual(self.check(report, 'project')['code'], 'project:locked')
            self.assertEqual(before, snapshot(self.base, ignore=('.project.lock',)))

    def test_invalid_header_and_metadata_are_reported_without_sqlite_repair(self):
        root = self.project()
        db = root/'project.db'
        db.write_bytes(b'not sqlite')
        before = snapshot(self.base)
        report = self.run_doctor(project=root)
        self.assertEqual(self.check(report, 'project')['code'], 'doctor:invalid_database_header')
        self.assertEqual(before, snapshot(self.base))
        (root/'project.json').write_text('{"name":"PRIVATE"}', encoding='utf-8')
        report = self.run_doctor(project=root)
        self.assertEqual(report['exit_code'], 5)
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_newer_project_and_migration_indicators_require_attention_without_upgrade(self):
        root = self.project()
        path = root/'project.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        atomic_json(path, {**original, 'format_version': 2})
        report = self.run_doctor(project=root)
        self.assertEqual(report['exit_code'], 7)
        self.assertEqual(self.check(report, 'project')['code'], 'project:unsupported_version')
        atomic_json(path, original)
        marker = root/'.migration-state.json'
        marker.write_bytes(b'keep unknown migration evidence')
        before = snapshot(self.base)
        report = self.run_doctor(project=root)
        self.assertEqual(report['exit_code'], 7)
        self.assertEqual(before, snapshot(self.base))

    def test_pending_receipts_are_reported_without_reading_or_recovering_them(self):
        root = self.project()
        pending = root/'images'/'.pending'
        pending.mkdir(exist_ok=True)
        (pending/'private-receipt.json').write_bytes(b'secret receipt payload')
        before = snapshot(self.base)
        report = self.run_doctor(project=root)
        self.assertEqual(report['exit_code'], 7)
        self.assertEqual(self.check(report, 'project')['details']['recovery_indicators'], ['images/.pending'])
        self.assertNotIn('secret receipt', json.dumps(report))
        self.assertEqual(before, snapshot(self.base))

    def test_wal_presence_is_reported_without_treating_header_version_as_current(self):
        root = self.project()
        (root/'project.db-wal').write_bytes(b'opaque WAL data')
        before = snapshot(self.base)
        report = self.run_doctor(project=root)
        self.assertEqual(report['exit_code'], 0, report)
        self.assertTrue(self.check(report, 'project')['details']['wal_present'])
        self.assertEqual(before, snapshot(self.base))

    def test_recovery_journal_is_validated_but_not_restored_or_disclosed(self):
        journal = SettingsJournal(self.paths.private/'recovery'/'launcher')
        journal.save(journal.load(), {'lease_id': str(uuid4()), 'project_id': str(uuid4()), 'source_id': 1,
            'cycle_id': str(uuid4()), 'identity': {'instance_id': 'a'*64, 'contract_digest': 'b'*64,
            'writable_keys': ['model']}, 'original': {'model': 'PRIVATE_ORIGINAL'},
            'desired': {'model': 'PRIVATE_DESIRED'}, 'phase': 'applying', 'write_id': str(uuid4()), 'inflight': None})
        before = snapshot(self.base)
        report = self.run_doctor()
        self.assertEqual(report['exit_code'], 7)
        self.assertEqual(self.check(report, 'recovery.launcher')['details']['phase'], 'applying')
        self.assertNotIn('PRIVATE_', json.dumps(report))
        self.assertEqual(before, snapshot(self.base))
        journal.path.write_bytes(b'{')
        report = self.run_doctor()
        self.assertEqual(report['exit_code'], 5)
        self.assertEqual(journal.path.read_bytes(), b'{')

    def test_model_marker_and_size_are_checked_without_claiming_content_verification(self):
        manifest, _ = fixture()
        root = self.paths.models/manifest.model_id/manifest.revision
        root.mkdir(parents=True)
        atomic_json(root/'ready.json', {'schema_version': 1, 'manifest': manifest.model_dump(mode='json')})
        for file in manifest.files:
            (root/file.name).write_bytes(b'x'*file.size)  # Deliberately wrong hash, same size.
        with patch('stylelora.tagging.manifests.REVIEWED_MANIFESTS', (manifest,)), \
             patch('stylelora.tagging.model_runtime.validate_tensors', side_effect=AssertionError('no inference')):
            report = self.run_doctor()
        self.assertEqual(report['exit_code'], 0, report)
        self.assertEqual(self.check(report, 'model.1')['code'], 'tagger:metadata_valid')
        self.assertFalse(self.check(report, 'model.1')['details']['content_verified'])
        (root/manifest.model_file.name).write_bytes(b'bad size')
        with patch('stylelora.tagging.manifests.REVIEWED_MANIFESTS', (manifest,)):
            report = self.run_doctor()
        self.assertEqual(self.check(report, 'model.1')['exit_code'], 5)

    def test_partial_model_without_ready_marker_and_bad_manifest_are_not_installed(self):
        manifest, _ = fixture()
        root = self.paths.models/manifest.model_id/manifest.revision
        root.mkdir(parents=True)
        with patch('stylelora.tagging.manifests.REVIEWED_MANIFESTS', (manifest,)):
            self.assertEqual(self.check(self.run_doctor(), 'model.1')['exit_code'], 5)
            (root/'ready.json').write_bytes(b'{}')
            self.assertEqual(self.check(self.run_doctor(), 'model.1')['code'], 'tagger:model_invalid')

    def test_errors_do_not_disclose_exception_messages_and_continue(self):
        with patch.object(doctor, 'directory_access', side_effect=PermissionError('SECRET_PATH_URL_TOKEN')):
            report = self.run_doctor()
        self.assertEqual(report['exit_code'], 5)
        self.assertNotIn('SECRET_PATH_URL_TOKEN', json.dumps(report))
        self.assertEqual(self.check(report, 'config.product')['exit_code'], 0)

    def test_cli_help_and_unsupported_mutations_do_not_run_checks(self):
        for arguments, expected in ((['--help'], 0), (['--repair'], 2), (['--install'], 2)):
            with self.subTest(arguments=arguments), patch.object(doctor, 'diagnose') as diagnosed:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(['doctor', *arguments])
                self.assertEqual(error.exception.code, expected)
                diagnosed.assert_not_called()

    def test_interrupt_returns_130_without_success_json(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(doctor, 'diagnose', side_effect=KeyboardInterrupt), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(['doctor']), 130)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('interrupted', err.getvalue())

    def test_optional_bounded_reader_does_not_swallow_body_file_not_found(self):
        path = self.base/'present.json'
        path.write_bytes(b'{}')
        with self.assertRaises(FileNotFoundError):
            with doctor.bounded(path, 2, optional=True):
                raise FileNotFoundError('body failure')


@unittest.skipUnless(os.name == 'nt', 'Windows native diagnostic checks')
class NativeDoctorTests(unittest.TestCase):
    def test_real_dpapi_only_round_trips_an_ephemeral_probe(self):
        with patch('stylelora.llm.secrets.SecretStore.read', side_effect=AssertionError('no real secrets')):
            value = doctor.dpapi_capability()
        self.assertEqual(value['exit_code'], 0)
        self.assertFalse(value['details']['stored_secrets_decrypted'])

    def test_product_mutex_busy_is_observed_without_attaching_or_releasing_owner(self):
        from stylelora.runtime.instance import ProductLock
        try:
            owner = ProductLock()
        except ServiceError:
            self.skipTest('User service already running')
        try:
            self.assertEqual(doctor.instance_status()['code'], 'instance:running')
            with self.assertRaises(ServiceError):
                ProductLock()
        finally:
            owner.close()
        self.assertEqual(doctor.instance_status()['code'], 'instance:idle')

    def test_junction_configuration_is_rejected_without_changing_target(self):
        import subprocess
        TEMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            base = Path(temporary)
            target = base/'target'
            target.mkdir()
            (target/'untouched').write_bytes(b'private')
            link = base/'linked'
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True,
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                before = snapshot(target)
                report = doctor.diagnose(runtime_dir=base/'runtime', product_dir=link)
                self.assertEqual(report['exit_code'], 5)
                self.assertEqual(snapshot(target), before)
            finally:
                link.rmdir()
