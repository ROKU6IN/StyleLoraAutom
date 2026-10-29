"""Thread-affine SQLite sessions, WAL snapshots and a single FIFO writer."""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager

from .errors import (
    Busy, CommitUnknown, ConstraintViolation, Corrupt, DiskFull, NestedTransaction,
    ProjectClosed, StorageError, UnsupportedSQLite, WrongThread,
)


def check_sqlite() -> None:
    if sqlite3.sqlite_version_info < (3, 38):
        raise UnsupportedSQLite("SQLite 3.38 or newer is required")
    with sqlite3.connect(":memory:") as db:
        try:
            if db.execute("SELECT json_valid('{}')").fetchone()[0] != 1:
                raise UnsupportedSQLite("SQLite JSON functions are required")
        except sqlite3.Error as exc:
            raise UnsupportedSQLite("SQLite JSON functions are required") from exc


def connect(path, *, readonly=False, wal=True):
    check_sqlite()
    uri = path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        if readonly:
            db.execute("PRAGMA query_only=ON")
        else:
            if wal and db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise StorageError("WAL could not be enabled")
            db.execute("PRAGMA synchronous=FULL")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise UnsupportedSQLite("SQLite foreign keys could not be enabled")
        return db
    except BaseException:
        db.close()
        raise


def translate_sqlite(exc: sqlite3.Error):
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if isinstance(exc, sqlite3.IntegrityError):
        return ConstraintViolation("Database constraint rejected the change")
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return Busy("Project database is busy")
    if code == sqlite3.SQLITE_FULL:
        return DiskFull("Insufficient disk space")
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return Corrupt("Project database is corrupt")
    return StorageError("Database operation failed")


class ReadSession:
    def __init__(self, db, project=None):
        self._db = db
        self.project = project
        self._owner = threading.get_ident()
        self._active = True

    def _check(self):
        if threading.get_ident() != self._owner:
            raise WrongThread("A database session cannot cross threads")
        if not self._active:
            raise ProjectClosed("Database session has ended")

    def execute(self, sql, parameters=()):
        self._check()
        return self._db.execute(sql, parameters)

    def executemany(self, sql, parameters):
        self._check()
        return self._db.executemany(sql, parameters)

    def chunks(self, values, reserved=0):
        self._check()
        size = min(500, self._db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - reserved)
        if size <= 0:
            raise ConstraintViolation("SQL variable budget exhausted")
        values = tuple(values)
        for index in range(0, len(values), size):
            yield values[index:index + size]


class WriteSession(ReadSession):
    def __init__(self, db, project=None):
        super().__init__(db, project)
        self._permissions = set()
        self.after_commit = []

    @contextmanager
    def permit(self, permission):
        had = permission in self._permissions
        self._permissions.add(permission)
        try:
            yield
        finally:
            if not had:
                self._permissions.remove(permission)

    def require(self, permission):
        self._check()
        if permission not in self._permissions:
            raise ConstraintViolation("This write requires its owning atomic command")


@contextmanager
def transaction(db, project=None):
    if db.in_transaction:
        raise NestedTransaction("Nested transactions are not supported")
    for attempt in range(3):
        try:
            db.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 0xFF) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise translate_sqlite(exc) from exc
            if attempt == 2:
                raise Busy("Could not begin a transaction") from exc
            time.sleep(0.01 * (attempt + 1))
    session = WriteSession(db, project)
    try:
        yield session
        try:
            if project is not None:
                project._fault("before_commit")
            db.commit()
        except sqlite3.Error as exc:
            raise CommitUnknown("Commit could not be confirmed; inspect persistent evidence before retrying") from exc
    except BaseException as exc:
        if db.in_transaction:
            db.rollback()
        if isinstance(exc, sqlite3.Error):
            raise translate_sqlite(exc) from exc
        raise
    else:
        for callback in session.after_commit:
            callback()
        if project is not None:
            project._fault("after_commit")
    finally:
        session._active = False


class WriterQueue:
    def __init__(self, project):
        self.project = project
        self._queue = queue.Queue()
        self._gate = threading.Lock()
        self._closed = False
        self._ready = Future()
        self._thread = threading.Thread(target=self._run, name="stylelora-project-writer", daemon=True)
        self._thread.start()
        self._ready.result()

    def _run(self):
        try:
            self._db = connect(self.project.root / "project.db")
        except BaseException as exc:
            self._ready.set_exception(exc)
            return
        self._ready.set_result(None)
        try:
            while True:
                entry = self._queue.get()
                if entry is None:
                    break
                future, work = entry
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    with transaction(self._db, self.project) as session:
                        result = work(session)
                    future.set_result(result)
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._db.close()

    def submit(self, work):
        if threading.get_ident() == self._thread.ident:
            raise WrongThread("A writer callback must not enqueue another writer callback")
        with self._gate:
            if self._closed:
                raise ProjectClosed("Writer is closed")
            future = Future()
            self._queue.put((future, work))
            return future

    def close(self):
        if threading.get_ident() == self._thread.ident:
            raise WrongThread("Cannot close a project from its writer thread")
        with self._gate:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._thread.join()


def submit_write(project, work):
    with project._lifecycle:
        if project._closed or project._closing:
            raise ProjectClosed("Project is closed or closing")
        return project.writer.submit(work)


@contextmanager
def write_transaction(project):
    if threading.get_ident() != project.writer._thread.ident:
        raise WrongThread("Write transactions belong to the writer thread")
    with transaction(project.writer._db, project) as session:
        yield session


@contextmanager
def read_snapshot(project):
    with project._lifecycle:
        if project._closed or project._closing:
            raise ProjectClosed("Project is closed or closing")
        project._readers += 1
    db = None
    session = None
    try:
        db = connect(project.root / "project.db", readonly=True)
        db.execute("BEGIN")
        # Establish the WAL snapshot now, rather than at the caller's first query.
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        session = ReadSession(db, project)
        yield session
    except sqlite3.Error as exc:
        raise translate_sqlite(exc) from exc
    finally:
        if session:
            session._active = False
        if db:
            db.rollback()
            db.close()
        with project._lifecycle:
            project._readers -= 1
