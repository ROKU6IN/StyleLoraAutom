import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from stylelora.cli import main
from stylelora.project_db.project import create_project
from stylelora.project_db.storage import atomic_json
from stylelora.runtime import doctor, doctor_content as content
from stylelora.runtime.paths import locations
from .support import TEMP_ROOT
from .tagger_support import fixture
from .test_doctor import snapshot

CONNECT = sqlite3.connect


class ContentDoctorTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.paths = locations(self.base/'runtime')
        for name in ('instance_status', 'dpapi_capability'):
            patched = patch.object(doctor, name, return_value=doctor.result())
            patched.start()
            self.addCleanup(patched.stop)

    def diagnose(self, **kwargs):
        return doctor.diagnose(runtime_dir=self.paths.runtime, verify_content=True, **kwargs)

    def row(self, report, key):
        return next(value for value in report['checks'] if value['id'] == key)

    def project(self):
        root = self.base/'项目 #100%.slproj'
        with create_project(root, name='PRIVATE_NAME', trigger_word='private_style'):
            pass
        return root

    def install(self, **kwargs):
        manifest, files = fixture(**kwargs)
        root = self.paths.models/manifest.model_id/manifest.revision
        root.mkdir(parents=True)
        atomic_json(root/'ready.json', {'schema_version': 1, 'manifest': manifest.model_dump(mode='json')})
        for name, value in files.items():
            (root/name).write_bytes(value)
        return manifest, root

    def mutate(self, root, *statements):
        db = CONNECT(root/'project.db')
        try:
            for statement in statements:
                db.execute(statement)
            db.commit()
        finally:
            db.close()

    def test_content_verification_is_opt_in(self):
        with patch.object(content, 'model_content', side_effect=AssertionError('not requested')), \
                patch.object(content, 'database_content', side_effect=AssertionError('not requested')):
            report = doctor.diagnose(runtime_dir=self.paths.runtime, project=self.project())
        self.assertEqual(report['scope'], 'offline_metadata')
        self.assertFalse(self.row(report, 'project')['details']['integrity_checked'])

    def test_missing_optional_model_does_not_create_or_download_anything(self):
        before = snapshot(self.base)
        report = self.diagnose()
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(report['scope'], 'offline_content')
        self.assertEqual(self.row(report, 'model.1')['code'], 'tagger:not_installed')
        self.assertFalse(self.row(report, 'model.1')['details']['content_verified'])
        self.assertEqual(before, snapshot(self.base))

    def test_streamed_model_hashes_match_without_loading_onnx_or_mutating_files(self):
        manifest, root = self.install(model=b'x'*(content.CHUNK_BYTES+17))
        before = snapshot(self.base)
        with patch('socket.socket', side_effect=AssertionError('no network')), \
                patch('stylelora.runtime.lifecycle.Runtime', side_effect=AssertionError('no service')), \
                patch('stylelora.tagging.model_runtime.validate_tensors', side_effect=AssertionError('no ONNX')), \
                patch('stylelora.tagging.manifests.REVIEWED_MANIFESTS', (manifest,)):
            report = self.diagnose()
        result = self.row(report, 'model.1')
        self.assertEqual(result['code'], 'tagger:content_verified')
        self.assertTrue(result['details']['content_verified'])
        self.assertEqual(result['details']['verified_files'], 2)
        self.assertEqual(result['details']['verified_bytes'], sum(f.size for f in manifest.files))
        self.assertFalse(result['details']['inference_run'])
        self.assertEqual(before, snapshot(self.base))
        self.assertNotIn(str(root), json.dumps(report))

    def test_same_size_model_tampering_is_detected(self):
        manifest, root = self.install()
        target = root/manifest.model_file.name
        target.write_bytes(b'x'*target.stat().st_size)
        self.assertEqual(doctor.model_metadata(self.paths, manifest)['exit_code'], 0)
        before = snapshot(self.base)
        result = content.model_content(self.paths, manifest)
        self.assertEqual(result['code'], 'tagger:checksum_mismatch')
        self.assertFalse(result['details']['content_verified'])
        self.assertEqual(before, snapshot(self.base))

    def test_missing_file_in_installed_model_is_an_error(self):
        manifest, root = self.install()
        (root/manifest.csv_file.name).unlink()
        result = content.model_content(self.paths, manifest)
        self.assertEqual(result['code'], 'tagger:model_incomplete')
        self.assertEqual(result['exit_code'], 5)

    def test_wrong_or_malformed_marker_does_not_authorize_hash_success(self):
        manifest, root = self.install()
        for value in (b'{', b'{"PRIVATE_MARKER":true}'):
            (root/'ready.json').write_bytes(value)
            result = content.model_content(self.paths, manifest)
            self.assertEqual(result['exit_code'], 5)
            self.assertFalse(result['details']['content_verified'])
            self.assertNotIn('PRIVATE_MARKER', json.dumps(result))

    def test_model_timeout_releases_all_pins_and_never_reports_success(self):
        manifest, root = self.install()
        with patch.object(content.time, 'monotonic', side_effect=[0, 121]):
            result = content.model_content(self.paths, manifest)
        self.assertEqual(result['code'], 'doctor:content_timeout')
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse(result['details']['content_verified'])
        with (root/manifest.model_file.name).open('r+b'):
            pass

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing')
    def test_model_with_existing_writer_is_not_read_as_a_stable_snapshot(self):
        manifest, root = self.install()
        with (root/manifest.model_file.name).open('r+b'):
            result = content.model_content(self.paths, manifest)
        self.assertEqual(result['code'], 'image:file_busy')
        self.assertEqual(result['exit_code'], 4)
        self.assertFalse(result['details']['content_verified'])

    def test_valid_database_uses_encoded_immutable_uri_and_preserves_every_file(self):
        root = self.project()
        before, opened = snapshot(self.base), []
        def connect(path, **kwargs):
            opened.append((str(path), kwargs))
            return CONNECT(path, **kwargs)
        with patch.object(content.sqlite3, 'connect', side_effect=connect), \
                patch('stylelora.project_db.project.open_project', side_effect=AssertionError('no recovery')):
            report = self.diagnose(project=root)
        row = self.row(report, 'project')
        self.assertEqual(row['code'], 'project:integrity_valid', row)
        self.assertTrue(row['details']['integrity_checked'])
        self.assertTrue(row['details']['foreign_keys_checked'])
        self.assertTrue(row['details']['schema_checked'])
        self.assertTrue(row['details']['check_constraints_checked'])
        self.assertFalse(row['details']['opened'])
        self.assertFalse(row['details']['image_content_checked'])
        actual = [(uri, opts) for uri, opts in opened if 'project.db' in uri]
        self.assertEqual(len(actual), 1)
        self.assertTrue(actual[0][0].endswith('project.db?mode=ro&immutable=1'))
        self.assertIn('%23', actual[0][0])
        self.assertIn('%25', actual[0][0])
        self.assertTrue(actual[0][1]['uri'])
        self.assertEqual(before, snapshot(self.base))

    def test_recovery_sidecars_block_sqlite_inspection_without_cleanup(self):
        root = self.project()
        for name in ('project.db-wal', 'project.db-shm', 'project.db-journal', '.migration-state.json'):
            file = root/name
            file.write_bytes(b'PRIVATE_RECOVERY_CONTENT')
            before = snapshot(self.base)
            with self.subTest(name=name):
                row = self.row(self.diagnose(project=root), 'project')
                self.assertEqual(row['code'], 'doctor:project_requires_recovery')
                self.assertEqual(row['exit_code'], 7)
                self.assertFalse(row['details']['sqlite_opened'])
                self.assertIn(name, row['details']['blocking_files'])
                self.assertEqual(before, snapshot(self.base))
                self.assertNotIn('PRIVATE_RECOVERY', json.dumps(row))
            file.unlink()

    def test_pending_evidence_blocks_content_check_without_reading_receipt(self):
        root = self.project()
        (root/'images/.pending').mkdir()
        (root/'images/.pending/private.json').write_text('PRIVATE_RECEIPT', encoding='utf-8')
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'doctor:project_requires_recovery')
        self.assertFalse(row['details']['sqlite_opened'])
        self.assertNotIn('PRIVATE_RECEIPT', json.dumps(row))

    def test_open_project_is_not_verified_or_recovered(self):
        with create_project(self.base/'busy.slproj', name='Busy', trigger_word='busy_style') as project:
            with patch.object(content, 'database_content', side_effect=AssertionError('locked project')):
                row = self.row(self.diagnose(project=project.root), 'project')
        self.assertEqual(row['code'], 'project:locked')
        self.assertEqual(row['exit_code'], 4)

    def test_truncated_database_with_valid_header_is_rejected(self):
        root = self.project()
        target = root/'project.db'
        target.write_bytes(target.read_bytes()[:100])
        self.assertEqual(doctor.project_check(root)['exit_code'], 0)
        before = snapshot(self.base)
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['exit_code'], 5)
        self.assertEqual(row['code'], 'doctor:database_invalid')
        self.assertEqual(before, snapshot(self.base))

    def test_unexpected_schema_is_reported_without_user_sql_or_table_names(self):
        root = self.project()
        self.mutate(root, 'CREATE TABLE PRIVATE_TABLE(value TEXT)')
        before = snapshot(self.base)
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'doctor:schema_mismatch')
        self.assertNotIn('PRIVATE_TABLE', json.dumps(row))
        self.assertEqual(before, snapshot(self.base))

    def test_version_mismatch_does_not_migrate(self):
        root = self.project()
        self.mutate(root, 'PRAGMA user_version=2')
        before = snapshot(self.base)
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'project:version_mismatch')
        self.assertEqual(row['exit_code'], 7)
        self.assertFalse(row['details']['integrity_checked'])
        self.assertEqual(before, snapshot(self.base))

    def test_foreign_key_violation_is_detected_without_disclosing_row_values(self):
        root = self.project()
        self.mutate(root, "INSERT INTO source_items(source_id,seq,prompt,updated_at) VALUES(999,1,'PRIVATE_PROMPT','private_time')")
        before = snapshot(self.base)
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'doctor:foreign_key_violation', row)
        self.assertTrue(row['details']['integrity_checked'])
        self.assertNotIn('PRIVATE_PROMPT', json.dumps(row))
        self.assertEqual(before, snapshot(self.base))

    def test_sqlite_check_constraint_violation_is_detected(self):
        root = self.project()
        self.mutate(root, 'PRAGMA ignore_check_constraints=ON', "INSERT INTO sources(type,name,created_at) VALUES('import','','private_time')")
        before = snapshot(self.base)
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'doctor:database_corrupt', row)
        self.assertEqual(before, snapshot(self.base))

    def test_package_check_expressions_handle_quoted_parentheses_and_nested_calls(self):
        sql = "CREATE TABLE t(x TEXT CHECK (length(trim(x))>0), y TEXT CHECK (y<>')CHECK(''('))"
        self.assertEqual(list(content.check_expressions(sql)), ["length(trim(x))>0", "y<>')CHECK(''('"])

    def test_valid_rows_with_json_and_nullable_fields_pass_constraint_checks(self):
        root = self.project()
        self.mutate(root,
                    "INSERT INTO sources(type,name,created_at,config_json) VALUES('import','sample','time','{\"note\":\")\"}')",
                    "INSERT INTO source_items(source_id,seq,prompt,updated_at) VALUES(1,1,'private_prompt','time')")
        before = snapshot(self.base)
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'project:integrity_valid', row)
        self.assertTrue(row['details']['check_constraints_checked'])
        self.assertEqual(before, snapshot(self.base))

    def test_compound_check_violation_is_detected_without_exposing_values(self):
        root = self.project()
        self.mutate(root, 'PRAGMA ignore_check_constraints=ON',
                    "INSERT INTO sources(type,name,created_at,status,locked) VALUES('import','private_name','time','running',1)")
        before = snapshot(self.base)
        row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'doctor:database_corrupt', row)
        self.assertFalse(row['details']['check_constraints_checked'])
        self.assertNotIn('private_name', json.dumps(row))
        self.assertEqual(before, snapshot(self.base))

    def test_database_byte_limit_blocks_before_sqlite_open(self):
        root = self.project()
        with patch.object(content, 'MAX_DATABASE_BYTES', 100):
            row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'doctor:database_too_large')
        self.assertEqual(row['exit_code'], 7)
        self.assertFalse(row['details']['sqlite_opened'])

    def test_database_timeout_is_attention_and_releases_project_lock(self):
        root = self.project()
        ticks = iter([0, 0])
        with patch.object(content.time, 'monotonic', side_effect=lambda: next(ticks, 121)):
            row = self.row(self.diagnose(project=root), 'project')
        self.assertEqual(row['code'], 'doctor:content_timeout', row)
        self.assertEqual(row['exit_code'], 7)
        self.assertEqual(self.row(self.diagnose(project=root), 'project')['code'], 'project:integrity_valid')

    def test_cli_json_has_no_user_content_or_paths_and_unknown_options_fail_before_checks(self):
        root = self.project()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(['doctor', '--runtime-dir', str(self.paths.runtime), '--project', str(root), '--verify-content', '--json'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['scope'], 'offline_content')
        self.assertNotIn('PRIVATE_NAME', output.getvalue())
        self.assertNotIn(str(self.base), output.getvalue())
        self.assertEqual(errors.getvalue(), '')
        for args in (['--content-timeout', '10'], ['--verify-content', '--content-timeout', '0'],
                     ['--verify-content', '--content-timeout', '601']):
            with patch.object(doctor, 'diagnose') as diagnosed, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['doctor', *args])
            diagnosed.assert_not_called()

    def test_interrupt_during_hashing_releases_files_and_returns_no_success_json(self):
        manifest, root = self.install()
        read = os.read
        def interrupt(fd, length):
            if length == content.CHUNK_BYTES:
                raise KeyboardInterrupt()
            return read(fd, length)
        output, errors = io.StringIO(), io.StringIO()
        with patch('stylelora.tagging.manifests.REVIEWED_MANIFESTS', (manifest,)), \
                patch.object(content.os, 'read', side_effect=interrupt), redirect_stdout(output), redirect_stderr(errors):
            code = main(['doctor', '--runtime-dir', str(self.paths.runtime), '--verify-content'])
        self.assertEqual(code, 130)
        self.assertEqual(output.getvalue(), '')
        with (root/manifest.model_file.name).open('r+b'):
            pass
