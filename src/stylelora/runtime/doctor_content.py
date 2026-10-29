"""Opt-in offline content checks. No recovery, inference, downloads or project writes."""
from contextlib import ExitStack
import hashlib
import os
import re
import sqlite3
import time

from ..llm.secrets import safe_path
from ..services.storage_files import identity
from ..services.verified_file import pinned
from .contracts import strict_json
from .doctor import bounded, canonical_path, result
from .errors import ServiceError

MAX_DATABASE_BYTES = 8 * 1024**3
CHUNK_BYTES = 1024**2


def check_expressions(sql):
    """Extract balanced CHECK expressions from trusted package schema only."""
    tokens = iter(re.finditer(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b\w+\b|[()]", sql))
    for token in tokens:
        if token[0].upper() != 'CHECK':
            continue
        opening = next(tokens)
        if opening[0] != '(':
            raise ValueError('Unsupported package constraint')
        depth = 1
        for closing in tokens:
            depth += (closing[0] == '(') - (closing[0] == ')')
            if depth == 0:
                yield sql[opening.end():closing.start()]
                break
        else:
            raise ValueError('Unbalanced package constraint')


def constraints_valid(db, schema, deadline):
    # SQLite 3.53.1 omits CHECK ASTs when loading a read-only database (build.c,
    # sqlite3AddCheckConstraint). Evaluate package expressions explicitly so a
    # successful read-only integrity_check cannot conceal a CHECK violation.
    for kind, name, sql in schema:
        if kind != 'table':
            continue
        conditions = [f'CAST(({expr}) AS NUMERIC)=0' for expr in check_expressions(sql)]
        if conditions:
            expired(deadline)
            table = '"' + name.replace('"', '""') + '"'
            if db.execute(f'SELECT 1 FROM {table} NOT INDEXED WHERE ' + ' OR '.join(conditions) + ' LIMIT 1').fetchone():
                return False
    return True


def expired(deadline):
    if time.monotonic() >= deadline:
        raise ServiceError(408, 'doctor:content_timeout')


def unchanged(path, fd, before):
    safe_path(path)
    if identity(path.stat()) != before or identity(os.fstat(fd)) != before:
        raise ServiceError(409, 'doctor:file_changed')


def model_content(paths, manifest, *, timeout=120):
    details = {'manifest_id': manifest.digest, 'content_verified': False, 'inference_run': False,
               'verification': 'sha256', 'verified_files': 0, 'verified_bytes': 0}
    deadline = time.monotonic() + timeout
    root = canonical_path(paths.models/manifest.model_id/manifest.revision)
    if not root.exists():
        return result('tagger:not_installed', **details)
    try:
        with ExitStack() as stack:
            directory = stack.enter_context(pinned(root, directory=True))
            directory_id = identity(os.fstat(directory))[:2]
            marker = stack.enter_context(bounded(root/'ready.json', 32768))
            marker_id = identity(os.fstat(marker))
            if strict_json(os.read(marker, 32769)) != {'schema_version': 1, 'manifest': manifest.model_dump(mode='json')}:
                raise ServiceError(503, 'tagger:model_invalid')
            held = []
            for file in manifest.files:
                fd = stack.enter_context(bounded(root/file.name, file.size))
                before = identity(os.fstat(fd))
                if before[2] != file.size:
                    raise ServiceError(503, 'tagger:checksum_mismatch')
                held.append((file, fd, before))
            for file, fd, before in held:
                checksum, count = hashlib.sha256(), 0
                while True:
                    expired(deadline)
                    unchanged(root/file.name, fd, before)
                    chunk = os.read(fd, CHUNK_BYTES)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > file.size:
                        raise ServiceError(503, 'tagger:checksum_mismatch')
                    checksum.update(chunk)
                if count != file.size or checksum.hexdigest() != file.sha256:
                    raise ServiceError(503, 'tagger:checksum_mismatch')
                details['verified_files'] += 1
                details['verified_bytes'] += count
            expired(deadline)
            unchanged(root/'ready.json', marker, marker_id)
            for file, fd, before in held:
                unchanged(root/file.name, fd, before)
            if identity(root.stat())[:2] != directory_id:
                raise ServiceError(409, 'doctor:file_changed')
            details['content_verified'] = True
            return result('tagger:content_verified', **details)
    except ServiceError as error:
        return result(error.code, 7 if error.code == 'doctor:content_timeout' else 4 if error.code == 'image:file_busy' else 5, **details)
    except FileNotFoundError:
        return result('tagger:model_incomplete', 5, **details)


def database_content(root, fd, metadata, details, *, timeout=120):
    """Caller holds the existing project lock, metadata, root and database pins.

    immutable=1 is safe only for a stable main DB with no recovery sidecars.
    It prevents SQLite creating or modifying WAL/SHM, even in read-only folders.
    """
    from ..project_db.migrations import SUPPORTED_VERSION, _baseline_signature, _schema_signature
    details = {**details, 'sqlite_opened': False, 'schema_checked': False, 'foreign_keys_checked': False,
               'check_constraints_checked': False,
               'image_content_checked': False}
    path = root/'project.db'
    before = identity(os.fstat(fd))
    deadline = time.monotonic() + timeout

    def sidecars():
        return [name for name in ('project.db-wal', 'project.db-shm', 'project.db-journal', '.migration-state.json')
                if safe_path(root/name).exists()]

    blockers = sidecars()
    if blockers or details['recovery_indicators']:
        return result('doctor:project_requires_recovery', 7, **details, blocking_files=blockers)
    if before[2] > MAX_DATABASE_BYTES:
        return result('doctor:database_too_large', 7, **details, maximum_bytes=MAX_DATABASE_BYTES)
    # Baseline is package-owned SQL in a separate in-memory database.
    expected_schema = _baseline_signature()
    db = None
    try:
        expired(deadline)
        unchanged(path, fd, before)
        db = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True, timeout=0)
        details.update(sqlite_opened=True, database_check='sqlite_integrity_foreign_keys_schema')
        db.row_factory = sqlite3.Row
        db.enable_load_extension(False)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024**2)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)

        def authorize(action, first, second, *_):
            if action == sqlite3.SQLITE_PRAGMA:
                return sqlite3.SQLITE_OK if first in {'user_version', 'integrity_check', 'foreign_key_check'} and second is None else sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and second in {
                'length', 'trim', 'instr', 'char', 'glob', 'json_valid', 'json_type', 'json_array_length',
            }:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        version = db.execute('PRAGMA user_version').fetchone()[0]
        details['database_version'] = version
        if version != metadata.format_version or version != SUPPORTED_VERSION:
            return result('project:version_mismatch', 7, **details)
        unexpected = db.execute("SELECT 1 FROM sqlite_master WHERE type NOT IN ('table','index') LIMIT 1").fetchone()
        if unexpected or _schema_signature(db) != expected_schema:
            return result('doctor:schema_mismatch', 5, **details)
        details['schema_checked'] = True
        rows = db.execute('PRAGMA integrity_check')
        valid = rows.fetchone()
        if valid is None or valid[0] != 'ok' or rows.fetchone() is not None:
            return result('doctor:database_corrupt', 5, **details)
        if not constraints_valid(db, expected_schema, deadline):
            return result('doctor:database_corrupt', 5, **details)
        details['check_constraints_checked'] = True
        details['integrity_checked'] = True
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            return result('doctor:foreign_key_violation', 5, **details)
        details['foreign_keys_checked'] = True
        expired(deadline)
        unchanged(path, fd, before)
        if sidecars():
            return result('doctor:project_changed', 7, **{**details, 'integrity_checked': False})
        return result('project:integrity_valid', **details)
    except sqlite3.DatabaseError:
        timed_out = time.monotonic() >= deadline
        return result('doctor:content_timeout' if timed_out else 'doctor:database_invalid', 7 if timed_out else 5, **details)
    except ServiceError as error:
        return result(error.code, 7 if error.code == 'doctor:content_timeout' else 5, **details)
    finally:
        if db is not None:
            db.close()
