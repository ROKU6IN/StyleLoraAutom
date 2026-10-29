"""F01 project persistence. Public lifecycle functions are imported lazily."""

from .types import *


def __getattr__(name):
    if name in {"create_project", "open_project", "close_project", "update_metadata", "ProjectHandle"}:
        from . import project
        return getattr(project, name)
    if name in {"submit_write", "read_snapshot"}:
        from . import connection
        return getattr(connection, name)
    raise AttributeError(name)
