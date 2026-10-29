"""Trusted, package-owned migrations with a recoverable DB/JSON commit protocol.

Version 1 is the first published format. No unpublished draft/version-0 project
is silently treated as a supported predecessor.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from ..connection import ReadSession, connect, transaction
from ..errors import (
    Corrupt, DiskFull, MigrationFailed, MigrationRecoveryRequired, NewerProject, VersionMismatch,
)
from ..locking import ProjectLock
from ..storage import atomic_bytes, atomic_json, read_metadata
from ..types import ExportSchemeInput, ImportSchemeInput, MigrationInfo
from ..validation import decode, digest, file_hash, relative_path

SUPPORTED_VERSION = 1


@dataclass(frozen=True)
class MigrationStep:
    info: MigrationInfo
    sql: str


# Only package code may extend this registry. Project directories are never executed.
REGISTRY: tuple[MigrationStep, ...] = ()


def execute_statements(db, sql):
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise MigrationFailed("Incomplete migration SQL")


def initial_schema(db):
    from ..repositories.schemes import create_export_scheme, create_import_scheme
    execute_statements(db, Path(__file__).with_name("0001_initial.sql").read_text(encoding="utf-8"))
    create_export_scheme(db, ExportSchemeInput("Default image + txt"))
    create_import_scheme(db, ImportSchemeInput("Default txt sidecar", is_default=True))
    db.execute("PRAGMA user_version=1")


@lru_cache(maxsize=1)
def _baseline_signature():
    """Validate names, types, defaults, constraints and index definitions, not just counts."""
    db = sqlite3.connect(":memory:")
    try:
        execute_statements(db, Path(__file__).with_name("0001_initial.sql").read_text(encoding="utf-8"))
        return _schema_signature(db)
    finally:
        db.close()


def _schema_signature(db):
    return tuple((row[0], row[1], " ".join(row[2].split()) if row[2] else None) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY type,name"
    ))


def version_pair(root):
    metadata = read_metadata(root)
    db = connect(relative_path(root, "project.db"), readonly=True)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()
    return metadata, version


def _plan(start, target):
    if start < 1:
        raise VersionMismatch("Unpublished draft databases are not supported")
    if target > SUPPORTED_VERSION or start > SUPPORTED_VERSION:
        raise NewerProject("Project requires a newer application")
    if target < start:
        raise VersionMismatch("Downgrade migrations are not supported")
    steps = []
    current = start
    while current < target:
        candidates = [step for step in REGISTRY if step.info.from_version == current]
        if len(candidates) != 1:
            raise MigrationFailed("Missing or ambiguous trusted migration step")
        step = candidates[0]
        if step.info.to_version != current + 1 or step.info.to_version > target:
            raise MigrationFailed("Migration registry must be a continuous chain")
        if hashlib.sha256(step.sql.encode("utf-8")).hexdigest() != step.info.checksum:
            raise MigrationFailed("Migration checksum mismatch")
        steps.append(step)
        current = step.info.to_version
    return tuple(steps)


def plan_migrations(format_version, user_version):
    if max(format_version, user_version) > SUPPORTED_VERSION:
        raise NewerProject("Project requires a newer application")
    if format_version != user_version:
        raise VersionMismatch("Project JSON and database versions disagree")
    return tuple(step.info for step in _plan(user_version, SUPPORTED_VERSION))


def verify_integrity(db, *, full=False):
    issues = []
    check = "integrity_check" if full else "quick_check"
    if [row[0] for row in db.execute("PRAGMA " + check)] != ["ok"]:
        issues.append("sqlite:integrity")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        issues.append("sqlite:foreign_key")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"sources", "source_items", "images", "annotations", "jobs",
                "operation_log", "export_schemes", "import_schemes"}
    if tables != expected:
        return tuple(issues + ["schema:tables"])
    if db.execute("PRAGMA user_version").fetchone()[0] == 1:
        columns = sum(len(db.execute(f"PRAGMA table_info({table})").fetchall()) for table in sorted(expected))
        indexes = db.execute("SELECT count(*),sum(sql IS NOT NULL) FROM sqlite_master WHERE type='index'").fetchone()
        if columns != 93 or tuple(indexes) != (17, 15):
            issues.append("schema:shape")
        if _schema_signature(db) != _baseline_signature():
            issues.append("schema:definition")
    if db.execute("SELECT count(*) FROM import_schemes WHERE is_default=1").fetchone()[0] != 1:
        issues.append("scheme:default")
    if not db.execute("SELECT 1 FROM export_schemes LIMIT 1").fetchone():
        issues.append("scheme:export")
    if db.execute(
        "SELECT 1 FROM source_items i JOIN images m ON i.image_id=m.image_id "
        "WHERE i.source_id<>m.source_id LIMIT 1"
    ).fetchone():
        issues.append("item:image_owner")
    for row in db.execute("SELECT image_id,filename FROM images"):
        if Path(row["filename"]).stem != str(row["image_id"]):
            issues.append("image:filename")
    for row in db.execute("SELECT job_id,progress_json,total,done_count,failed_count,skipped_count FROM jobs"):
        try:
            progress = decode(row["progress_json"])
            ids, outcomes = progress["image_ids"], progress["outcomes"]
            if progress["schema_version"] != 1 or len(ids) != len(set(ids)) or len(ids) != row["total"]:
                raise ValueError()
            if not set(outcomes).issubset({str(image_id) for image_id in ids}):
                raise ValueError()
            for kind, column in (("done", "done_count"), ("failed", "failed_count"), ("skipped", "skipped_count")):
                if sum(outcome["kind"] == kind for outcome in outcomes.values()) != row[column]:
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            issues.append("job:progress")
    for row in db.execute("SELECT changes_json FROM operation_log"):
        try:
            for change in decode(row[0]):
                for key in ("new_annotation_id", "previous_annotation_id"):
                    annotation_id = change[key]
                    if annotation_id is None and key == "previous_annotation_id":
                        continue
                    ref = db.execute("SELECT image_id,source,type FROM annotations WHERE annotation_id=?",
                                     (annotation_id,)).fetchone()
                    if not ref or tuple(ref) != (change["image_id"], "rule", "tags"):
                        raise ValueError()
        except (KeyError, TypeError, ValueError):
            issues.append("operation:references")
    return tuple(dict.fromkeys(issues))


def _assert_integrity(connection):
    issues = verify_integrity(ReadSession(connection), full=True)
    if issues:
        raise Corrupt("Project integrity validation failed: " + ", ".join(issues))


def _fault(hook, stage):
    if hook:
        hook(stage)


def _migrate_locked(root, target_version, *, fault=None):
    metadata, version = version_pair(root)
    if max(metadata.format_version, version) > SUPPORTED_VERSION:
        raise NewerProject("Project requires a newer application")
    state_path = relative_path(root, ".migration-state.json")
    state = None
    if state_path.exists():
        state = decode(state_path.read_text(encoding="utf-8"))
        try:
            start, target = state["from_version"], state["to_version"]
            steps = _plan(start, target)
            if state["checksums"] != [step.info.checksum for step in steps]:
                raise ValueError()
            old_meta, new_meta = state["old_metadata"], state["new_metadata"]
            if digest(old_meta) != state["old_metadata_hash"] or digest(new_meta) != state["new_metadata_hash"]:
                raise ValueError()
            if old_meta["format_version"] != start or new_meta["format_version"] != target:
                raise ValueError()
            if {**old_meta, "format_version": target} != new_meta:
                raise ValueError()
            if digest(metadata) not in (state["old_metadata_hash"], state["new_metadata_hash"]):
                raise ValueError()
            backup = relative_path(root, state["backup"])
            if file_hash(backup / "project.db") != state["backup_db_hash"]:
                raise ValueError()
            if file_hash(backup / "project.json") != state["backup_metadata_file_hash"]:
                raise ValueError()
            if version not in (start, target) or (version == start and metadata.format_version != start):
                raise ValueError()
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise MigrationRecoveryRequired("Untrusted or inconsistent migration evidence") from exc
        target_version = target
    else:
        if metadata.format_version != version:
            raise VersionMismatch("Mixed versions without trusted migration evidence")
        steps = _plan(version, target_version)
        if not steps:
            return metadata
        required = (root / "project.db").stat().st_size * 3 + 1024 * 1024
        if shutil.disk_usage(root).free < required:
            raise DiskFull("Insufficient space for backup and migration")
        backup_rel = ".migration-backup/" + str(version) + "-" + uuid.uuid4().hex
        backup = relative_path(root, backup_rel)
        backup.mkdir(parents=True)
        source = connect(root / "project.db", readonly=True)
        try:
            target_db = sqlite3.connect(backup / "project.db")
            try:
                source.backup(target_db)
            finally:
                target_db.close()
        finally:
            source.close()
        atomic_bytes(backup / "project.json", (root / "project.json").read_bytes())
        new_metadata = replace(metadata, format_version=target_version)
        from ..validation import primitive
        state = {
            "schema_version": 1, "from_version": version, "to_version": target_version,
            "checksums": [step.info.checksum for step in steps], "backup": backup_rel,
            "old_metadata": primitive(metadata), "new_metadata": primitive(new_metadata),
            "old_metadata_hash": digest(metadata), "new_metadata_hash": digest(new_metadata),
            "backup_db_hash": file_hash(backup / "project.db"),
            "backup_metadata_file_hash": file_hash(backup / "project.json"), "stage": "prepared",
        }
        atomic_json(state_path, state)
    db = connect(root / "project.db")
    try:
        if version != target_version:
            try:
                with transaction(db) as session:
                    for step in steps:
                        execute_statements(session, step.sql)
                    session.execute(f"PRAGMA user_version={int(target_version)}")
                    _assert_integrity(db)
                    _fault(fault, "migration_before_commit")
            except Exception as exc:
                raise MigrationFailed("Migration failed; database transaction rolled back and evidence retained") from exc
            _fault(fault, "migration_db_committed")
            state["stage"] = "db_committed"
            atomic_json(state_path, state)
        _assert_integrity(db)
    finally:
        db.close()
    atomic_json(root / "project.json", state["new_metadata"])
    _fault(fault, "migration_json_published")
    state_path.unlink()
    return read_metadata(root)


def migrate_project(root, *, target_version=None):
    root = Path(root).resolve()
    lock = ProjectLock(root)
    try:
        return _migrate_locked(root, SUPPORTED_VERSION if target_version is None else target_version)
    finally:
        lock.close()


def restore_migration_backup(root):
    """Explicit local rollback restores BOTH files, never only user_version."""
    root = Path(root).resolve()
    lock = ProjectLock(root)
    try:
        state_path = relative_path(root, ".migration-state.json")
        state = decode(state_path.read_text(encoding="utf-8"))
        backup = relative_path(root, state["backup"])
        if file_hash(backup / "project.db") != state["backup_db_hash"] or file_hash(
            backup / "project.json"
        ) != state["backup_metadata_file_hash"]:
            raise MigrationRecoveryRequired("Backup checksum mismatch")
        current_metadata, current_version = version_pair(root)
        if max(current_metadata.format_version, current_version) > SUPPORTED_VERSION:
            raise NewerProject("A newer project cannot be restored by this application")
        source = sqlite3.connect((backup / "project.db").as_uri() + "?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            _assert_integrity(source)
            if source.execute("PRAGMA user_version").fetchone()[0] != state["from_version"]:
                raise MigrationRecoveryRequired("Backup database version mismatch")
            if digest(read_metadata(backup)) != state["old_metadata_hash"]:
                raise MigrationRecoveryRequired("Backup metadata does not match the original project")
        except BaseException:
            source.close()
            raise
        target = connect(root / "project.db")
        try:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            target.close()
            source.close()
        atomic_bytes(root / "project.json", (backup / "project.json").read_bytes())
        state_path.unlink()
        return read_metadata(root)
    finally:
        lock.close()
