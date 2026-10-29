from ..project_db.errors import ProjectError


class ExchangeError(ProjectError):
    """A stable, path-free reason suitable for item errors and reports."""

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(code if message is None else f"{code}: {message}")


class ExportInterrupted(ExchangeError):
    def __init__(self, code, relative_directory):
        self.relative_directory = relative_directory
        super().__init__(code, "Export was not published; the interrupted receipt is retained")
