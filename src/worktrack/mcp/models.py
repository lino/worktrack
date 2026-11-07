"""Canonical models, tool schemas, constants, and exceptions for `worktrack.mcp`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from worktrack.core.exceptions import LedgerError
from worktrack.ipc.framing import JSONRPC_INVALID_PARAMS
from worktrack.outbox.exceptions import EgressBypassBlockedError

# region Constants
DEFAULT_MCP_SERVER_NAME: Final = "worktrack-mcp"
DEFAULT_MCP_SERVER_VERSION: Final = "0.1.0"
DEFAULT_MCP_PROTOCOL_VERSION: Final = "2024-11-05"
MCP_SOURCE_LABEL: Final = "mcp"

MCP_METHOD_INITIALIZE: Final = "initialize"
MCP_METHOD_INITIALIZED: Final = "notifications/initialized"
MCP_METHOD_PING: Final = "ping"
MCP_METHOD_TOOLS_LIST: Final = "tools/list"
MCP_METHOD_TOOLS_CALL: Final = "tools/call"

MCP_TOOL_STATUS: Final = "worktrack_status"
MCP_TOOL_INSIGHTS: Final = "worktrack_insights"
MCP_TOOL_AGENDA: Final = "worktrack_agenda"
MCP_TOOL_CLUES: Final = "worktrack_clues"
MCP_TOOL_STAGE_OUTBOX_ROLLUP: Final = "worktrack_stage_outbox_rollup"
MCP_TOOL_PROPOSE_RETROSPLIT: Final = "worktrack_propose_retrosplit"

FORBIDDEN_DIRECT_SINK_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "worktrack_direct_sink_write",
        "worktrack_flush_outbox",
        "worktrack_push_sink",
        "worktrack_write_sink",
    }
)
FORBIDDEN_BYPASS_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {
        "auto_flush",
        "auto_approve",
        "bypass_outbox",
        "flush_immediately",
        "direct_write",
    }
)

MCP_ERR_EGRESS_BYPASS_BLOCKED: Final = -32003
DEFAULT_CLUES_LIMIT: Final = 50
MIN_CLUES_LIMIT: Final = 1
MAX_CLUES_LIMIT: Final = 500
# endregion


# region Exceptions
class McpError(LedgerError):
    """Base exception for `worktrack.mcp` protocol and tool execution errors."""


class McpRequestError(McpError):
    """Structured JSON-RPC 2.0 MCP request or tool argument error."""

    def __init__(
        self,
        message: str,
        *,
        code: int = JSONRPC_INVALID_PARAMS,
        data: dict[str, object] | None = None,
    ) -> None:
        """Store JSON-RPC error `code`, `message`, and optional structured `data`."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class McpEgressBypassError(McpRequestError, EgressBypassBlockedError):
    """Raised when an MCP tool call attempts to bypass the previewable `Outbox` gate."""

    def __init__(
        self,
        message: str,
        *,
        sink: str,
        source: str = MCP_SOURCE_LABEL,
    ) -> None:
        """Initialize Outbox gate bypass error with `sink` and `source` labels."""
        super().__init__(
            message,
            code=MCP_ERR_EGRESS_BYPASS_BLOCKED,
            data={
                "error_code": "egress_bypass_blocked",
                "sink": sink,
                "source": source,
            },
        )
        self.sink = sink
        self.source = source


# endregion


# region Models
@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """Declarative Model Context Protocol tool definition and JSON Schema."""

    name: str
    description: str
    input_schema: dict[str, object]
    read_only: bool = True

    def to_dict(self) -> dict[str, object]:
        """Serialize tool descriptor into an MCP `tools/list` entry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }


CANONICAL_MCP_TOOLS: Final[tuple[McpToolSpec, ...]] = (
    McpToolSpec(
        name=MCP_TOOL_STATUS,
        description="Read active Interval timer, elapsed duration, workplace, and daily total.",
        input_schema={
            "type": "object",
            "properties": {
                "now": {"type": "string", "description": "Optional UTC ISO-8601 timestamp."}
            },
            "additionalProperties": False,
        },
        read_only=True,
    ),
    McpToolSpec(
        name=MCP_TOOL_INSIGHTS,
        description="Compute local Now, Today, or This Week horizon insights.",
        input_schema={
            "type": "object",
            "properties": {
                "horizon": {
                    "type": "string",
                    "enum": ["now", "today", "week", "this_week"],
                    "default": "today",
                },
                "now": {"type": "string", "description": "Optional UTC ISO-8601 timestamp."},
            },
            "additionalProperties": False,
        },
        read_only=True,
    ),
    McpToolSpec(
        name=MCP_TOOL_AGENDA,
        description="List scheduled AgendaItems and FocusBlocks from the local SQLite Node.",
        input_schema={
            "type": "object",
            "properties": {
                "horizon": {
                    "type": "string",
                    "enum": ["today", "this_week", "backlog"],
                },
                "include_done": {"type": "boolean", "default": False},
                "since": {"type": "string", "description": "Optional UTC ISO-8601 start bound."},
                "until": {"type": "string", "description": "Optional UTC ISO-8601 end bound."},
            },
            "additionalProperties": False,
        },
        read_only=True,
    ),
    McpToolSpec(
        name=MCP_TOOL_CLUES,
        description="Read ambient local Clues stored in SQLite (never transmitted externally).",
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Optional clue collector source."},
                "limit": {
                    "type": "integer",
                    "minimum": MIN_CLUES_LIMIT,
                    "maximum": MAX_CLUES_LIMIT,
                    "default": DEFAULT_CLUES_LIMIT,
                },
            },
            "additionalProperties": False,
        },
        read_only=True,
    ),
    McpToolSpec(
        name=MCP_TOOL_STAGE_OUTBOX_ROLLUP,
        description=(
            "Compute and stage previewable RollupProposals into OutboxStore with "
            "status='staged' awaiting human approval. Never flushes directly to Sinks."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sink": {"type": "string", "description": "Target Sink slug (e.g. 'jira')."},
                "grouping_window": {
                    "type": "string",
                    "enum": ["day", "week", "month", "interval"],
                    "default": "day",
                },
                "rounding_quantum_seconds": {"type": "integer", "default": 60},
                "rounding_mode": {
                    "type": "string",
                    "enum": ["nearest", "ceil", "floor"],
                    "default": "nearest",
                },
                "redact_notes": {"type": "boolean", "default": True},
                "flush_cadence": {
                    "type": "string",
                    "enum": ["on_clock_out", "eod", "eow", "eom", "manual"],
                    "default": "eod",
                },
                "since": {"type": "string"},
                "until": {"type": "string"},
                "skip_unchanged": {"type": "boolean", "default": True},
            },
            "required": ["sink"],
            "additionalProperties": False,
        },
        read_only=False,
    ),
    McpToolSpec(
        name=MCP_TOOL_PROPOSE_RETROSPLIT,
        description="Preview a proportional or equal RetroSplit over a local closed Interval.",
        input_schema={
            "type": "object",
            "properties": {
                "interval_id": {"type": "string"},
                "activity_ids": {"type": "array", "items": {"type": "string"}},
                "weights": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["interval_id", "activity_ids"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
)
# endregion
