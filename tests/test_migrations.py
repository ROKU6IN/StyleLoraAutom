import hashlib
import sqlite3
from dataclasses import replace
from unittest.mock import patch

from stylelora.project_db import migrations
from stylelora.project_db.errors import (
    DiskFull, MigrationFailed, MigrationRecoveryRequired, NewerProject, VersionMismatch,
)
from stylelora.project_db.locking import ProjectLock
from stylelora.project_db.project import close_project, open_project
from stylelora.project_db.storage import atomic_json, read_metadata
from stylelora.project_db.types import MigrationInfo
from stylelora.project_db.validation import decode, file_hash
from .support import DatabaseTest


def migration_step(sql="UPDATE export_schemes SET name='migrated-once';\n"):
    info = MigrationInfo(1, 2, "test_fixture_upgrade", hashlib.sha256(sql.encode("utf-8")).hexdigest())
    return migrations.MigrationStep(info, sql)


class MigrationTests(DatabaseTest):
    def setup_upgrade(self, step=None):
        self.image()
        close_project(self.project)
        self.stack = []
        for context in (patch.object(migrations, "SUPPORTED_VERSION", 2),
                        patch.object(migrations, "REGISTRY", (step or migration_step(),))):
            context.start()
            self.addCleanup(context.stop)

    def run_fault(self, stage):
        def fault(current):
            if current == stage:
                raise OSError("injected migration interruption")
        lock = ProjectLock(self.root)
        try:
            return migrations._migrate_locked(self.root, 2, fault=fault)
        finally:
            lock.close()

    def test_M01_trusted_continuous_upgrade_preserves_records(self):
        self.setup_upgrade()
        result = migrations.migrate_project(self.root)
        self.assertEqual(result.format_version, 2)
        self.project = open_project(self.root)
        self.assertEqual(self.sql("SELECT count(*) FROM images"), ((1,),))
        self.assertEqual(self.sql("SELECT count(*) FROM annotations"), ((1,),))
        self.assertEqual(self.sql("SELECT name FROM export_schemes"), (("migrated-once",),))
        self.assertEqual(self.read(migrations.verify_integrity, full=True), ())
        self.assertEqual(len(tuple((self.root / ".migration-backup").iterdir())), 1)

    def test_M02_partial_sql_failure_rolls_back_and_retains_evidence(self):
        self.setup_upgrade(migration_step("UPDATE export_schemes SET name='should roll back';\n"
                                          "INSERT INTO nonexistent_table VALUES(1);\n"))
        with self.assertRaises(MigrationFailed):
            migrations.migrate_project(self.root)
        metadata, version = migrations.version_pair(self.root)
        self.assertEqual((metadata.format_version, version), (1, 1))
        db = sqlite3.connect(self.root / "project.db")
        try:
            self.assertEqual(db.execute("SELECT name FROM export_schemes").fetchone()[0], "Default image + txt")
        finally:
            db.close()
        self.assertTrue((self.root / ".migration-state.json").is_file())

    def test_M02_DB_commit_before_JSON_publish_recovers_without_rerunning_SQL(self):
        self.setup_upgrade(migration_step("UPDATE export_schemes SET name=name || '-once';\n"))
        with self.assertRaises(OSError):
            self.run_fault("migration_db_committed")
        metadata, version = migrations.version_pair(self.root)
        self.assertEqual((metadata.format_version, version), (1, 2))
        self.project = open_project(self.root)
        self.assertEqual(self.project.metadata.format_version, 2)
        self.assertEqual(self.sql("SELECT name FROM export_schemes"), (("Default image + txt-once",),))
        self.assertFalse((self.root / ".migration-state.json").exists())

    def test_M02_JSON_publish_before_marker_cleanup_recovers(self):
        self.setup_upgrade()
        with self.assertRaises(OSError):
            self.run_fault("migration_json_published")
        self.assertTrue((self.root / ".migration-state.json").is_file())
        self.project = open_project(self.root)
        self.assertEqual(self.project.metadata.format_version, 2)
        self.assertFalse((self.root / ".migration-state.json").exists())

    def test_M02_before_commit_recovers_by_executing_uncommitted_step(self):
        self.setup_upgrade()
        with self.assertRaises(MigrationFailed):
            self.run_fault("migration_before_commit")
        self.assertEqual(migrations.version_pair(self.root)[1], 1)
        self.project = open_project(self.root)
        self.assertEqual(self.sql("SELECT name FROM export_schemes"), (("migrated-once",),))

    def test_M03_newer_JSON_or_database_refused_without_mutation(self):
        close_project(self.project)
        original_metadata = read_metadata(self.root)
        for json_version, db_version in ((2, 1), (1, 2), (2, 2)):
            with self.subTest(json_version=json_version, db_version=db_version):
                atomic_json(self.root / "project.json", replace(original_metadata, format_version=json_version))
                db = sqlite3.connect(self.root / "project.db")
                db.execute(f"PRAGMA user_version={db_version}")
                db.close()
                before = (file_hash(self.root / "project.db"), file_hash(self.root / "project.json"))
                with self.assertRaises(NewerProject):
                    open_project(self.root)
                self.assertEqual(before, (file_hash(self.root / "project.db"), file_hash(self.root / "project.json")))

    def test_M03_mixed_versions_without_marker_not_repaired(self):
        self.setup_upgrade()
        atomic_json(self.root / "project.json", replace(read_metadata(self.root), format_version=2))
        with self.assertRaises(VersionMismatch):
            open_project(self.root)
        self.assertEqual(migrations.version_pair(self.root)[1], 1)

    def test_M04_restore_both_backup_files(self):
        self.setup_upgrade()
        with self.assertRaises(OSError):
            self.run_fault("migration_db_committed")
        metadata = migrations.restore_migration_backup(self.root)
        self.assertEqual(metadata.format_version, 1)
        self.assertEqual(migrations.version_pair(self.root)[1], 1)
        db = sqlite3.connect(self.root / "project.db")
        try:
            self.assertEqual(db.execute("SELECT name FROM export_schemes").fetchone()[0], "Default image + txt")
            self.assertEqual(db.execute("SELECT count(*) FROM images").fetchone()[0], 1)
        finally:
            db.close()

    def test_M04_checksum_and_missing_chain_fail_before_backup(self):
        self.setup_upgrade()
        wrong = replace(migration_step(), info=replace(migration_step().info, checksum="0" * 64))
        for registry in ((), (wrong,)):
            with patch.object(migrations, "REGISTRY", registry), self.assertRaises(MigrationFailed):
                migrations.migrate_project(self.root)
        self.assertFalse((self.root / ".migration-backup").exists())

    def test_M04_insufficient_space_and_tampered_metadata(self):
        self.setup_upgrade()
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        with patch("stylelora.project_db.migrations.shutil.disk_usage", return_value=Usage(100, 100, 0)):
            with self.assertRaises(DiskFull):
                migrations.migrate_project(self.root)
        self.assertFalse((self.root / ".migration-state.json").exists())
        with self.assertRaises(OSError):
            self.run_fault("migration_db_committed")
        atomic_json(self.root / "project.json", replace(read_metadata(self.root), name="externally edited"))
        with self.assertRaises(MigrationRecoveryRequired):
            open_project(self.root)
        self.assertEqual(read_metadata(self.root).name, "externally edited")

    def test_M04_tampered_backup_refuses_restore(self):
        self.setup_upgrade()
        with self.assertRaises(OSError):
            self.run_fault("migration_db_committed")
        state = decode((self.root / ".migration-state.json").read_text(encoding="utf-8"))
        (self.root / state["backup"] / "project.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(MigrationRecoveryRequired):
            migrations.restore_migration_backup(self.root)

    def test_version_zero_is_not_a_supported_published_project(self):
        close_project(self.project)
        db = sqlite3.connect(self.root / "project.db")
        db.execute("PRAGMA user_version=0")
        db.close()
        with self.assertRaises(VersionMismatch):
            open_project(self.root)
