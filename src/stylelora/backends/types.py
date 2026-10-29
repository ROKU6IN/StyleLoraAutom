from dataclasses import dataclass, field


class BackendError(Exception):
    """Only constant reason codes cross the HTTP/log boundary."""

    def __init__(self, code, *, phase='transport', outcome='not_sent', retry='manual'):
        self.code, self.phase, self.outcome, self.retry = code, phase, outcome, retry
        super().__init__(code)


@dataclass(frozen=True)
class DiscoveryInfo:
    endpoint: str = field(repr=False)
    port: int
    token: str = field(repr=False)
    pid: int
    protocol_versions: tuple[str, ...]
    app_version: str


@dataclass(frozen=True)
class ToolSchema:
    name: str
    input_schema: dict = field(repr=False)
    output_schema: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ToolReply:
    payload: dict = field(repr=False)
    is_error: bool = False


@dataclass(frozen=True)
class CompatibilityReport:
    accepted: bool
    schema_hash: str
    full_schema_hash: str
    reason: str | None


@dataclass(frozen=True)
class ReviewedContract:
    app_version: str
    server_version: str
    protocol_version: str
    schema_hash: str
    full_schema_hash: str
    # An adapter release supplies reviewed capabilities, never inferred defaults.
    capabilities: dict = field(default_factory=dict, repr=False)
