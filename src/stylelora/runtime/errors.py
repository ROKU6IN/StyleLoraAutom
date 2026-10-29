from ..project_db import errors as db_errors
from ..exchange.errors import ExchangeError


class ServiceError(Exception):
    def __init__(self, status, code, message=None, *, details=None, outcome="rejected", issues=None):
        self.status, self.code = status, code
        self.message = message or code.replace(":", ": ").replace("_", " ")
        self.details, self.outcome = details or {}, outcome
        self.issues = issues or []
        super().__init__(self.message)

    def body(self, request_id):
        action = "reconcile" if self.outcome == "unknown" else "read" if self.status in (409, 410, 428) else "user" if self.status in (401, 422, 423, 501, 503) else "none"
        return {"code": self.code, "message": self.message, "retryable": False, "retry_action": action,
                "outcome": self.outcome, "details": self.details, "issues": self.issues, "request_id": request_id}


def safe_error(exc):
    if isinstance(exc, ServiceError):
        return exc
    mappings = (
        (db_errors.CommitUnknown, 503, "storage:commit_unknown"),
        (db_errors.ProjectLocked, 423, "project:locked"),
        (db_errors.ProjectExists, 409, "project:exists"),
        (db_errors.VersionMismatch, 409, "project:version_mismatch"),
        (db_errors.ProjectClosed, 410, "project:handle_expired"),
        (db_errors.NotFound, 404, "resource:not_found"),
        (db_errors.BatchTooLarge, 413, "batch:too_large"),
        (db_errors.UnsupportedScheme, 501, "feature:unsupported"),
        (db_errors.SorterContractViolation, 422, "sorter:contract_violation"),
        (db_errors.UnsupportedSorter, 501, "sorter:unsupported"),
        (db_errors.UnsafePath, 422, "path:unsafe"),
        (db_errors.PreviewStale, 409, "concurrency:version_conflict"),
        (db_errors.UndoConflict, 409, "undo:conflict"),
        (db_errors.StateConflict, 409, "state:conflict"),
        (db_errors.ValidationError, 422, "input:invalid"),
        (db_errors.Busy, 503, "storage:busy"),
        (db_errors.DiskFull, 503, "storage:disk_full"),
        (db_errors.Corrupt, 500, "storage:corrupt"),
        (db_errors.StorageError, 503, "storage:unavailable"),
        (FileNotFoundError, 422, "path:unavailable"),
        (PermissionError, 403, "path:access_denied"),
        (OSError, 503, "storage:unavailable"),
        (ValueError, 422, "input:invalid"),
    )
    if isinstance(exc, ExchangeError):
        return ServiceError(422, exc.code)
    for kind, status, code in mappings:
        if isinstance(exc, kind):
            return ServiceError(status, code, details=getattr(exc, 'details', None), outcome="unknown" if isinstance(exc, db_errors.CommitUnknown) else "rejected")
    return ServiceError(500, "internal:error", "The operation failed; inspect local state before retrying", outcome="unknown")
