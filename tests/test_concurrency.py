import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from stylelora.project_db import commands
from stylelora.project_db.connection import read_snapshot, submit_write, transaction, write_transaction
from stylelora.project_db.errors import (
    ActiveReaders, Busy, CommitUnknown, ConstraintViolation, DiskFull, NestedTransaction, ProjectClosed,
    ProjectExists, ProjectLocked, WrongThread,
)
from stylelora.project_db.project import close_project, create_project, open_project, update_metadata
from stylelora.project_db.repositories import annotations, images, sources
from stylelora.project_db.types import PageRequest, SourceDraft, SourceFilter
from .support import DatabaseTest, ROOT


class ConnectionTests(DatabaseTest):
    def test_C01_concurrent_workers_only_one_writer_thread(self):
        seen = set()
        def make(index):
            def work(db):
                seen.add(threading.get_ident())
                return sources.create_source(db, SourceDraft("import", f"source {index}"))
            return submit_write(self.project, work).result(timeout=10)
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = tuple(pool.map(make, range(40)))
        self.assertEqual(len({row.source_id for row in rows}), 40)
        self.assertEqual(len(seen), 1)
        self.assertEqual(next(iter(seen)), self.project.writer._thread.ident)

    def test_C01_concurrent_same_outcome_does_not_duplicate_versions(self):
        image = self.image()
        job, outcome, _ = self.model_result(image.image_id, ("B",))
        before = self.sql("SELECT count(*) FROM annotations")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = tuple(pool.map(
                lambda _: self.write(commands.commit_job_outcome, job.job_id, outcome), range(12)))
        self.assertTrue(all(not result.changed for result in results))
        self.assertEqual(before, self.sql("SELECT count(*) FROM annotations"))

    def test_C02_WAL_reader_sees_old_snapshot_while_writer_commits(self):
        with read_snapshot(self.project) as reader:
            self.assertEqual(reader.execute("SELECT count(*) FROM sources").fetchone()[0], 0)
            self.write(sources.create_source, SourceDraft("import", "new"))
            self.assertEqual(reader.execute("SELECT count(*) FROM sources").fetchone()[0], 0)
            with self.assertRaises(ActiveReaders):
                close_project(self.project)
        self.assertEqual(self.sql("SELECT count(*) FROM sources"), ((1,),))

    def test_C02_second_process_lock_and_release_after_close(self):
        code = (
            "import sys; from pathlib import Path; "
            "from stylelora.project_db.locking import ProjectLock; "
            "from stylelora.project_db.errors import ProjectLocked;\n"
            "try:\n lock=ProjectLock(Path(sys.argv[1])); lock.close()\n"
            "except ProjectLocked:\n sys.exit(23)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        close_project(self.project)
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.project = open_project(self.root)

    def test_C03_begin_busy_finite_retries_do_not_run_callback(self):
        calls = []
        class BusyConnection:
            in_transaction = False
            def execute(self, _):
                calls.append(1)
                exc = sqlite3.OperationalError("database is locked")
                exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise exc
        with patch("stylelora.project_db.connection.time.sleep"), self.assertRaises(Busy):
            with transaction(BusyConnection()):
                self.fail("No callback may run before BEGIN succeeds")
        self.assertEqual(len(calls), 3)

    def test_C03_commit_unknown_never_replays_callback(self):
        calls = []
        def fault(stage):
            if stage == "before_commit":
                raise sqlite3.OperationalError("injected commit uncertainty")
        self.project._fault_hook = fault
        def work(db):
            calls.append(1)
            sources.create_source(db, SourceDraft("import", "once"))
        with self.assertRaises(CommitUnknown):
            self.write(work)
        self.project._fault_hook = None
        self.assertEqual(calls, [1])
        self.assertEqual(self.sql("SELECT count(*) FROM sources"), ((0,),))

    def test_C03_disk_full_mid_operation_rolls_back_real_transaction(self):
        def work(db):
            sources.create_source(db, SourceDraft("import", "rollback"))
            exc = sqlite3.OperationalError("injected disk full")
            exc.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise exc
        with self.assertRaises(DiskFull):
            self.write(work)
        self.assertEqual(self.sql("SELECT count(*) FROM sources"), ((0,),))

    def test_P01_keyset_upper_bound_page_size_and_variable_limit(self):
        for index in range(7):
            self.write(sources.create_source, SourceDraft("import", str(index)))
        first = self.read(sources.list_sources, SourceFilter(), PageRequest(limit=3))
        self.write(sources.create_source, SourceDraft("import", "newer"))
        second = self.read(sources.list_sources, SourceFilter(),
                           PageRequest(first.next_after_id, 3, first.upper_id))
        third = self.read(sources.list_sources, SourceFilter(),
                          PageRequest(second.next_after_id, 3, first.upper_id))
        self.assertEqual([row.source_id for row in (*first.items, *second.items, *third.items)], list(range(1, 8)))
        self.assertIsNone(third.next_after_id)
        from stylelora.project_db.errors import ValidationError
        for page in (PageRequest(limit=1001), PageRequest(limit=0), PageRequest(after_id=-1), PageRequest(limit=True)):
            with self.assertRaises(ValidationError):
                self.read(sources.list_sources, SourceFilter(), page)
        def variable_test(db):
            prior = db._db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 16)
            try:
                return images.clear_recomputable_ratings(db, tuple(range(1, 1200)))
            finally:
                db._db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, prior)
        self.assertEqual(self.write(variable_test), 0)

    def test_sessions_thread_affinity_nested_transactions_and_closed_handles(self):
        with self.assertRaises(WrongThread):
            with write_transaction(self.project):
                pass
        def nested(db):
            with write_transaction(self.project):
                pass
        with self.assertRaises(NestedTransaction):
            self.write(nested)
        with read_snapshot(self.project) as db:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(db.execute, "SELECT 1")
                with self.assertRaises(WrongThread):
                    future.result()
        with self.assertRaises(ProjectClosed):
            db.execute("SELECT 1")
        close_project(self.project)
        close_project(self.project)
        with self.assertRaises(ProjectClosed):
            self.write(lambda db: 1)

    def test_project_creation_open_identity_and_metadata(self):
        self.assertIs(open_project(self.root), self.project)
        with self.assertRaises(ProjectExists):
            create_project(self.root, name="overwrite", trigger_word="")
        with self.assertRaises(ProjectLocked):
            create_project(self.root.with_name("other.slproj"), name="other", trigger_word="")
        updated = update_metadata(self.project, name="新名称", trigger_word="new_trigger")
        self.reopen()
        self.assertEqual(self.project.metadata, updated)

    def test_close_drains_accepted_writes(self):
        futures = [submit_write(self.project, lambda db, i=i: sources.create_source(
            db, SourceDraft("import", str(i)))) for i in range(12)]
        close_project(self.project)
        self.assertTrue(all(future.done() and future.exception() is None for future in futures))
        self.project = open_project(self.root)
        self.assertEqual(self.sql("SELECT count(*) FROM sources"), ((12,),))

    def test_query_indexes_are_usable(self):
        image = self.image()
        queries = (
            ("SELECT image_id FROM images WHERE sha256=?", (image.sha256,), "ix_images_sha256"),
            ("SELECT * FROM annotations WHERE image_id=? AND source=? AND type=? AND is_current=1",
             (image.image_id, "original", "tags"), "ux_annotations_current"),
            ("SELECT * FROM source_items WHERE source_id=? AND status=? ORDER BY seq",
             (image.source_id, "pending"), "ix_source_items_source_status_seq"),
        )
        for sql, params, index in queries:
            plan = " ".join(str(row) for row in self.sql("EXPLAIN QUERY PLAN " + sql, params))
            self.assertIn(index, plan)
