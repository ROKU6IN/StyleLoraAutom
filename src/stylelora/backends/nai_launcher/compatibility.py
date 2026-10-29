"""A seen schema hash is evidence, not permission to invoke its tools."""
import hashlib
import json

from ..types import CompatibilityReport

READ_TOOLS = frozenset({'get_generation_settings', 'get_generation_status', 'get_recent_images'})
WRITE_TOOLS = frozenset({'update_generation_settings', 'prepare_generation', 'submit_generation',
                         'cancel_generation_preparation', 'save_generated_image'})
CORE_TOOLS = READ_TOOLS | WRITE_TOOLS
# No full generation contract has been reviewed. Read-only contracts are separate.
# Synthetic test contracts are injected in tests and never shipped as production trust.
REVIEWED_CONTRACTS = ()


def schema_digest(tools):
    rows = [{'name': tool.name, 'inputSchema': tool.input_schema, 'outputSchema': tool.output_schema}
            for tool in sorted(tools, key=lambda t: t.name)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def verify_compatibility(info, server_version, protocol, tools, *, reviewed=REVIEWED_CONTRACTS):
    names = {tool.name for tool in tools}
    critical = schema_digest(t for t in tools if t.name in CORE_TOOLS)
    full = schema_digest(tools)
    reason = 'backend:missing_core_tools' if not CORE_TOOLS <= names else 'backend:unreviewed_schema'
    for contract in reviewed:
        if (contract.app_version == info.app_version and contract.server_version == server_version
                and contract.protocol_version == protocol and contract.schema_hash == critical
                and contract.full_schema_hash == full and CORE_TOOLS <= names):
            return CompatibilityReport(True, critical, full, None), contract
    return CompatibilityReport(False, critical, full, reason), None
