"""Portable .slproj containers and one open project per process."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from . import migrations
from .connection import ReadSession, WriterQueue, connect, transaction
from .errors import ActiveOperations, ActiveReaders, Corrupt, NewerProject, ProjectClosed, ProjectExists, ProjectLocked, VersionMismatch, WrongThread
from .locking import ProjectLock
from .storage import atomic_json, read_metadata
from .types import ProjectMetadata, RecoveryReport
from .validation import relative_path, text, utc_now

_process_gate = threading.RLock()
_current = None


class ProjectHandle:
    def __init__(self, root, metadata, lock):
        self.root = root
        self.metadata = metadata
        self.schema_version = metadata.format_version
        self._lock = lock
        self._lifecycle = threading.RLock()
        self._closed = False
        self._closing = False
        self._readers = 0
        self._operations = 0
        self._file_operations = threading.RLock()
        self._cycles = {}
        self._job_generations = set()
        self._fault_hook = None
        self.recovery_report = RecoveryReport()
        self.writer = WriterQueue(self)

    def _fault(self, stage):
        if self._fault_hook:
            self._fault_hook(stage)

    def close(self):
        close_project(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        close_project(self)


def create_project(root, *, name, trigger_word):
    global _current
    root = Path(root).absolute()
    text(name, "project name", nonempty=True)
    text(trigger_word, "trigger word")
    if root.suffix != ".slproj":
        raise ValueError("Project directory must end with .slproj")
    with _process_gate:
        if root.exists():
            raise ProjectExists("Project target already exists")
        if _current is not None and not _current._closed:
            raise ProjectLocked("Close the current project before creating another")
        if not root.parent.is_dir():
            raise FileNotFoundError("Project parent directory does not exist")
        temporary = Path(tempfile.mkdtemp(prefix=".stylelora-create-", dir=root.parent))
        try:
            for directory in ("images", "cache", "exports"):
                (temporary / directory).mkdir()
            metadata = ProjectMetadata(1, name, trigger_word, utc_now())
            atomic_json(temporary / "project.json", metadata)
            db = sqlite3.connect(temporary / "project.db", isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                with transaction(db) as session:
                    migrations.initial_schema(session)
                issues = migrations.verify_integrity(ReadSession(db), full=True)
                if issues:
                    raise Corrupt("New project failed integrity checks")
            finally:
                db.close()
            temporary.rename(root)
        finally:
            # Only the newly allocated temporary directory is eligible for cleanup.
            if temporary.exists() and temporary.parent.resolve() == root.parent.resolve() and temporary.name.startswith(".stylelora-create-"):
                shutil.rmtree(temporary)
        return open_project(root)


def open_project(root, *, migrate=True):
    global _current
    root = Path(root).resolve()
    with _process_gate:
        if _current is not None and not _current._closed:
            if _current._closing:
                raise ProjectClosed("Current project is closing")
            if _current.root == root:
                return _current
            raise ProjectLocked("Only one project may be open in this process")
        for name in ("project.json", "project.db", "images", "exports"):
            path = relative_path(root, name)
            if not path.exists():
                raise FileNotFoundError("Incomplete project container")
        lock = ProjectLock(root)
        project = None
        try:
            metadata, version = migrations.version_pair(root)
            if max(metadata.format_version, version) > migrations.SUPPORTED_VERSION:
                raise NewerProject("Project requires a newer application")
            if (root / ".migration-state.json").exists():
                if not migrate:
                    raise VersionMismatch("Migration recovery is required")
                metadata = migrations._migrate_locked(root, migrations.SUPPORTED_VERSION)
            else:
                plan = migrations.plan_migrations(metadata.format_version, version)
                if plan:
                    if not migrate:
                        raise VersionMismatch("Project migration is required")
                    metadata = migrations._migrate_locked(root, migrations.SUPPORTED_VERSION)
            db = connect(root / "project.db", readonly=True)
            try:
                issues = migrations.verify_integrity(ReadSession(db), full=True)
                if issues:
                    raise Corrupt("Project integrity validation failed: " + ", ".join(issues))
            finally:
                db.close()
            relative_path(root, "cache").mkdir(exist_ok=True)
            project = ProjectHandle(root, metadata, lock)
            from .recovery import recover_project
            project.recovery_report = recover_project(project)
            _current = project
            return project
        except BaseException:
            if project:
                project.writer.close()
            lock.close()
            raise


def close_project(project):
    global _current
    if threading.get_ident() == project.writer._thread.ident:
        raise WrongThread("Cannot close a project from a writer callback")
    with _process_gate, project._lifecycle:
        if project._closed:
            return
        if project._readers:
            raise ActiveReaders("Close read snapshots before closing the project")
        if project._operations:
            raise ActiveOperations("Wait for file operations before closing the project")
        project._closing = True
    try:
        project.writer.close()
    finally:
        with _process_gate, project._lifecycle:
            project._cycles.clear()
            project._job_generations.clear()
            project._closed = True
            project._lock.close()
            if _current is project:
                _current = None


def update_metadata(project, *, name, trigger_word):
    from .connection import submit_write
    text(name, "project name", nonempty=True)
    text(trigger_word, "trigger word")

    def update(db):
        value = replace(project.metadata, name=name, trigger_word=trigger_word)
        if value != project.metadata:
            atomic_json(project.root / "project.json", value)
            project.metadata = value
        return value

    return submit_write(project, update).result()
