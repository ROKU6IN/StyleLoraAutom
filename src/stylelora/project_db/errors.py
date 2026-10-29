"""Domain errors; messages never include SQL or upstream responses."""


class ProjectError(Exception):
    """Base for errors safe to translate at the API boundary."""


class ValidationError(ProjectError): pass
class UnsafePath(ValidationError): pass
class UnsupportedSQLite(ProjectError): pass
class ProjectExists(ProjectError): pass
class ProjectLocked(ProjectError): pass
class ProjectClosed(ProjectError): pass
class ActiveReaders(ProjectError): pass
class ActiveOperations(ProjectError): pass
class WrongThread(ProjectError): pass
class NestedTransaction(ProjectError): pass
class VersionMismatch(ProjectError): pass
class NewerProject(VersionMismatch): pass
class NotFound(ProjectError): pass
class StateConflict(ProjectError): pass
class LockedSource(StateConflict): pass
class SeedConflict(StateConflict): pass
class ArtifactConflict(StateConflict): pass
class CurrentConflict(StateConflict): pass
class OutcomeConflict(StateConflict): pass
class SnapshotMismatch(StateConflict): pass
class PreviewStale(StateConflict): pass
class UndoConflict(StateConflict):
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}
class InvalidChangeReference(StateConflict): pass
class FileMissing(StateConflict): pass
class ConstraintViolation(ProjectError): pass
class Busy(ProjectError): pass
class DiskFull(ProjectError): pass
class Corrupt(ProjectError): pass
class StorageError(ProjectError): pass
class CommitUnknown(StorageError): pass
class BatchTooLarge(ValidationError): pass
class UnsupportedScheme(ValidationError): pass
class UnsupportedSorter(ValidationError): pass
class SorterContractViolation(UnsupportedSorter): pass
class MigrationFailed(ProjectError): pass
class MigrationRecoveryRequired(ProjectError): pass
