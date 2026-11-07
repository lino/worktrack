"""JSON-RPC 2.0 Model Context Protocol server (`WorktrackMcpServer`) with Outbox gate."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

from worktrack.agenda import AgendaPlanner, apply_agenda_migrations
from worktrack.core import Ledger
from worktrack.ipc.framing import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    JSONRPC_VERSION,
    build_error_response,
    build_result_response,
)
from worktrack.mcp import tools as tool_ops
from worktrack.mcp.models import (
    CANONICAL_MCP_TOOLS,
    DEFAULT_MCP_PROTOCOL_VERSION,
    DEFAULT_MCP_SERVER_NAME,
    DEFAULT_MCP_SERVER_VERSION,
    MCP_METHOD_INITIALIZE,
    MCP_METHOD_INITIALIZED,
    MCP_METHOD_PING,
    MCP_METHOD_TOOLS_CALL,
    MCP_METHOD_TOOLS_LIST,
    MCP_TOOL_AGENDA,
    MCP_TOOL_CLUES,
    MCP_TOOL_INSIGHTS,
    MCP_TOOL_PROPOSE_RETROSPLIT,
    MCP_TOOL_STAGE_OUTBOX_ROLLUP,
    MCP_TOOL_STATUS,
    McpEgressBypassError,
    McpRequestError,
    McpToolSpec,
)
from worktrack.outbox import OutboxStore, RollupEngine
from worktrack.telemetry.metrics import TelemetryRecorder


# region MCP Server
class WorktrackMcpServer:
    """Outbox-guarded JSON-RPC 2.0 Model Context Protocol server for a Worktrack Node."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        outbox_store: OutboxStore | None = None,
        agenda_planner: AgendaPlanner | None = None,
        rollup_engine: RollupEngine | None = None,
        telemetry: TelemetryRecorder | None = None,
        server_name: str = DEFAULT_MCP_SERVER_NAME,
        server_version: str = DEFAULT_MCP_SERVER_VERSION,
        protocol_version: str = DEFAULT_MCP_PROTOCOL_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind `WorktrackMcpServer` to `ledger`, `outbox_store`, and `agenda_planner`."""
        resolved_telemetry = telemetry if telemetry is not None else ledger._telemetry
        conn = ledger._require_conn()
        apply_agenda_migrations(conn)

        resolved_outbox = (
            OutboxStore(connection=conn, telemetry=resolved_telemetry)
            if outbox_store is None
            else outbox_store
        )
        resolved_rollup = (
            RollupEngine(
                ledger=ledger,
                outbox_store=resolved_outbox,
                telemetry=resolved_telemetry,
            )
            if rollup_engine is None
            else rollup_engine
        )

        self._ledger = ledger
        self._outbox_store = resolved_outbox
        self._agenda_planner = agenda_planner
        self._rollup_engine = resolved_rollup
        self._telemetry = resolved_telemetry
        self._server_name = server_name
        self._server_version = server_version
        self._protocol_version = protocol_version
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_tools(self) -> tuple[McpToolSpec, ...]:
        """Return the canonical tuple of registered `McpToolSpec` definitions."""
        return CANONICAL_MCP_TOOLS

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a named MCP tool after enforcing the Outbox anti-surveillance gate."""
        if not isinstance(name, str) or not name.strip():
            raise McpRequestError("Tool 'name' must be a non-empty string.")
        clean_name = name.strip()
        args: Mapping[str, Any] = {} if arguments is None else arguments

        tool_ops.enforce_mcp_outbox_gate(clean_name, args, self._telemetry)
        return self._dispatch_tool(clean_name, args)

    def handle_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Process a decoded JSON-RPC 2.0 request dictionary and return a response envelope."""
        request_id = request.get("id")
        if request.get("jsonrpc") != JSONRPC_VERSION:
            return build_error_response(
                request_id,
                code=JSONRPC_INVALID_REQUEST,
                message="jsonrpc version must equal '2.0'.",
            )
        method = request.get("method")
        if not isinstance(method, str) or not method.strip():
            return build_error_response(
                request_id, code=JSONRPC_INVALID_REQUEST, message="Method must be a string."
            )
        raw_params = request.get("params", {})
        if raw_params is not None and not isinstance(raw_params, Mapping):
            return build_error_response(
                request_id, code=JSONRPC_INVALID_PARAMS, message="Params must be an object."
            )
        params: Mapping[str, Any] = {} if raw_params is None else raw_params
        return self._dispatch_rpc_method(request_id, method.strip(), params)

    def handle_json_line(self, line: str) -> str | None:
        """Process a single newline-delimited JSON-RPC frame from stdio."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            err = build_error_response(
                None, code=JSONRPC_PARSE_ERROR, message="Invalid JSON payload."
            )
            return json.dumps(err, separators=(",", ":"))

        if not isinstance(decoded, Mapping):
            err = build_error_response(
                None, code=JSONRPC_INVALID_REQUEST, message="JSON-RPC request must be an object."
            )
            return json.dumps(err, separators=(",", ":"))

        response = self.handle_request(decoded)
        if "id" not in decoded and "error" not in response:
            return None
        return json.dumps(response, separators=(",", ":"))

    def serve_stdio(self, input_stream: TextIO, output_stream: TextIO) -> int:
        """Consume newline-delimited JSON-RPC requests from `input_stream` to `output_stream`."""
        processed_count = 0
        for raw_line in input_stream:
            if not raw_line.strip():
                continue
            processed_count += 1
            response_line = self.handle_json_line(raw_line)
            if response_line is not None:
                output_stream.write(response_line + "\n")
                output_stream.flush()
        return processed_count

    def _dispatch_rpc_method(
        self,
        request_id: object,
        method: str,
        params: Mapping[str, object],
    ) -> dict[str, object]:
        """Dispatch a validated JSON-RPC 2.0 method onto MCP protocol handlers."""
        try:
            if method == MCP_METHOD_INITIALIZE:
                return build_result_response(request_id, self._build_initialize_result())
            if method == MCP_METHOD_INITIALIZED:
                return build_result_response(request_id, {"acknowledged": True})
            if method == MCP_METHOD_PING:
                return build_result_response(request_id, {})
            if method == MCP_METHOD_TOOLS_LIST:
                return build_result_response(
                    request_id,
                    {"tools": [spec.to_dict() for spec in self.list_tools()]},
                )
            if method == MCP_METHOD_TOOLS_CALL:
                return build_result_response(request_id, self._handle_tools_call(params))
            return build_error_response(
                request_id,
                code=JSONRPC_METHOD_NOT_FOUND,
                message=f"Unknown MCP method: '{method}'.",
            )
        except (McpEgressBypassError, McpRequestError) as req_err:
            return build_error_response(
                request_id,
                code=req_err.code,
                message=req_err.message,
                data=req_err.data,
            )
        except Exception as exc:
            return build_error_response(
                request_id,
                code=JSONRPC_INTERNAL_ERROR,
                message=f"Internal MCP server error: {exc}",
            )

    def _build_initialize_result(self) -> dict[str, object]:
        """Build the MCP `initialize` capabilities and serverInfo response dictionary."""
        return {
            "protocolVersion": self._protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self._server_name,
                "version": self._server_version,
            },
        }

    def _handle_tools_call(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Execute an MCP `tools/call` invocation and wrap structured + text content."""
        raw_name = params.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise McpRequestError("tools/call requires a non-empty string 'name'.")
        raw_args = params.get("arguments", {})
        if raw_args is not None and not isinstance(raw_args, Mapping):
            raise McpRequestError("tools/call 'arguments' must be an object.")
        args: Mapping[str, Any] = {} if raw_args is None else raw_args
        payload = self.call_tool(raw_name, args)
        return {
            "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
            "structuredContent": payload,
            "isError": False,
        }

    def _dispatch_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Route a verified tool call to its domain handler."""
        agenda_conn = (
            self._agenda_planner._require_conn()
            if self._agenda_planner is not None
            else self._ledger._require_conn()
        )
        if tool_name == MCP_TOOL_STATUS:
            return tool_ops.execute_status_tool(
                agenda_conn=agenda_conn,
                ledger=self._ledger,
                outbox_store=self._outbox_store,
                arguments=arguments,
                default_now=self._clock(),
            )
        if tool_name == MCP_TOOL_INSIGHTS:
            return tool_ops.execute_insights_tool(
                agenda_conn=agenda_conn,
                ledger=self._ledger,
                arguments=arguments,
                default_now=self._clock(),
            )
        if tool_name == MCP_TOOL_AGENDA:
            return tool_ops.execute_agenda_tool(
                agenda_planner=self._agenda_planner,
                agenda_conn=agenda_conn,
                arguments=arguments,
            )
        if tool_name == MCP_TOOL_CLUES:
            return tool_ops.execute_clues_tool(
                conn=self._ledger._require_conn(),
                arguments=arguments,
            )
        if tool_name == MCP_TOOL_STAGE_OUTBOX_ROLLUP:
            return tool_ops.execute_stage_outbox_rollup_tool(
                rollup_engine=self._rollup_engine,
                arguments=arguments,
            )
        if tool_name == MCP_TOOL_PROPOSE_RETROSPLIT:
            return tool_ops.execute_propose_retrosplit_tool(
                ledger=self._ledger,
                arguments=arguments,
            )
        raise McpRequestError(f"Unknown MCP tool: '{tool_name}'.")


# endregion
