"""Read-only and Outbox-staged MCP tool execution handlers and anti-bypass guards."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from worktrack.agenda import (
    HORIZON_THIS_WEEK,
    HORIZON_TODAY,
    AgendaPlanner,
)
from worktrack.agenda import (
    insights as insight_ops,
)
from worktrack.agenda import (
    persistence as agenda_persistence,
)
from worktrack.core import Ledger
from worktrack.core.models import format_utc_iso, parse_utc_iso
from worktrack.mcp.models import (
    DEFAULT_CLUES_LIMIT,
    FORBIDDEN_BYPASS_ARGUMENTS,
    FORBIDDEN_DIRECT_SINK_TOOLS,
    MAX_CLUES_LIMIT,
    MCP_SOURCE_LABEL,
    MIN_CLUES_LIMIT,
    McpEgressBypassError,
    McpRequestError,
)
from worktrack.outbox import (
    DEFAULT_FLUSH_CADENCE,
    DEFAULT_GROUPING_WINDOW,
    DEFAULT_ROUNDING_MODE,
    DEFAULT_ROUNDING_QUANTUM_SECONDS,
    OUTBOX_STATUS_STAGED,
    OutboxItem,
    OutboxStore,
    RollupEngine,
    RollupPolicy,
)
from worktrack.outbox.grants import sanitize_bypass_label
from worktrack.telemetry.metrics import TelemetryRecorder

# region Constants
HORIZON_NOW: Final = "now"
HORIZON_WEEK_ALIAS: Final = "week"
MIN_RETROSPLIT_TARGETS: Final = 1
ZERO_WEIGHT_SUM: Final = 0.0
# endregion


# region Validation
def enforce_mcp_outbox_gate(
    tool_name: str,
    arguments: Mapping[str, object],
    telemetry: TelemetryRecorder | None,
) -> None:
    """Block any MCP tool or argument attempting unpreviewed direct `Sink` egress."""
    clean_tool = tool_name.strip()
    raw_sink = arguments.get("sink")
    sink_label = sanitize_bypass_label(str(raw_sink) if isinstance(raw_sink, str) else "unknown")

    if clean_tool in FORBIDDEN_DIRECT_SINK_TOOLS or "direct_sink" in clean_tool:
        _record_and_raise_bypass(
            sink=sink_label,
            telemetry=telemetry,
            message=(
                f"MCP tool '{clean_tool}' is forbidden by the Outbox anti-surveillance gate; "
                "all external Sink writes must be staged via worktrack_stage_outbox_rollup."
            ),
        )

    for forbidden_arg in FORBIDDEN_BYPASS_ARGUMENTS:
        if bool(arguments.get(forbidden_arg)):
            _record_and_raise_bypass(
                sink=sink_label,
                telemetry=telemetry,
                message=(
                    f"MCP argument '{forbidden_arg}=True' is forbidden; MCP tools may only "
                    "stage previewable proposals in OutboxStore with status='staged'."
                ),
            )


def _record_and_raise_bypass(
    *,
    sink: str,
    telemetry: TelemetryRecorder | None,
    message: str,
) -> None:
    """Increment `worktrack_egress_bypass_blocked_total` and raise `McpEgressBypassError`."""
    if telemetry is not None:
        telemetry.record_egress_bypass_blocked(sink=sink, source=MCP_SOURCE_LABEL)
    raise McpEgressBypassError(message, sink=sink, source=MCP_SOURCE_LABEL)


def parse_optional_utc(raw_value: object, *, field_name: str) -> datetime | None:
    """Parse an optional UTC ISO-8601 timestamp argument or raise `McpRequestError`."""
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise McpRequestError(f"Argument '{field_name}' must be a non-empty ISO-8601 string.")
    try:
        return parse_utc_iso(raw_value.strip())
    except ValueError as exc:
        raise McpRequestError(f"Invalid ISO-8601 timestamp for '{field_name}': {exc}") from exc


def serialize_outbox_item(item: OutboxItem) -> dict[str, object]:
    """Serialize an `OutboxItem` into a deterministic JSON dictionary."""
    return {
        "id": item.id,
        "idempotency_key": item.idempotency_key,
        "sink": item.sink,
        "target_ref": item.target_ref,
        "window_start": format_utc_iso(item.window_start),
        "window_end": format_utc_iso(item.window_end),
        "raw_duration_seconds": item.raw_duration_seconds,
        "rounded_duration_seconds": item.rounded_duration_seconds,
        "summary": item.summary,
        "cadence": item.cadence,
        "status": item.status,
        "payload_sha256": item.payload_sha256,
        "payload_preview": dict(item.payload_preview),
    }


# endregion


# region MCP Tools
def execute_status_tool(
    *,
    agenda_conn: sqlite3.Connection,
    ledger: Ledger,
    outbox_store: OutboxStore,
    arguments: Mapping[str, object],
    default_now: datetime,
) -> dict[str, object]:
    """Execute `worktrack_status` returning active interval, daily total, and staged count."""
    target_now = parse_optional_utc(arguments.get("now"), field_name="now") or default_now
    now_insight = insight_ops.compute_now_insight(agenda_conn, ledger, now=target_now)
    payload = now_insight.to_dict()
    payload["staged_outbox_count"] = len(outbox_store.list_items(status=OUTBOX_STATUS_STAGED))
    return payload


def execute_insights_tool(
    *,
    agenda_conn: sqlite3.Connection,
    ledger: Ledger,
    arguments: Mapping[str, object],
    default_now: datetime,
) -> dict[str, object]:
    """Execute `worktrack_insights` for `now`, `today`, or `week`/`this_week`."""
    raw_horizon = arguments.get("horizon", HORIZON_TODAY)
    if not isinstance(raw_horizon, str):
        raise McpRequestError("Argument 'horizon' must be a string.")
    horizon = raw_horizon.strip().lower()
    target_now = parse_optional_utc(arguments.get("now"), field_name="now") or default_now

    if horizon == HORIZON_NOW:
        return insight_ops.compute_now_insight(agenda_conn, ledger, now=target_now).to_dict()
    if horizon == HORIZON_TODAY:
        return insight_ops.compute_today_insight(agenda_conn, ledger, now=target_now).to_dict()
    if horizon in (HORIZON_WEEK_ALIAS, HORIZON_THIS_WEEK):
        return insight_ops.compute_week_insight(agenda_conn, ledger, now=target_now).to_dict()
    raise McpRequestError(
        f"Unsupported insight horizon '{raw_horizon}'; expected 'now', 'today', or 'week'."
    )


def execute_agenda_tool(
    *,
    agenda_planner: AgendaPlanner | None,
    agenda_conn: sqlite3.Connection,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    """Execute `worktrack_agenda` returning scheduled `AgendaItems` and `FocusBlocks`."""
    raw_horizon = arguments.get("horizon")
    horizon = None if raw_horizon is None else str(raw_horizon).strip().lower()
    include_done = bool(arguments.get("include_done", False))
    since = parse_optional_utc(arguments.get("since"), field_name="since")
    until = parse_optional_utc(arguments.get("until"), field_name="until")

    try:
        if agenda_planner is not None:
            items = agenda_planner.list_items(horizon=horizon, include_done=include_done)
            blocks = agenda_planner.list_focus_blocks(since=since, until=until)
        else:
            items = agenda_persistence.list_agenda_items(
                agenda_conn, horizon=horizon, include_done=include_done
            )
            blocks = agenda_persistence.list_focus_blocks(agenda_conn, since=since, until=until)
    except Exception as exc:
        raise McpRequestError(str(exc)) from exc

    return {
        "agenda_items": [item.to_dict() for item in items],
        "focus_blocks": [block.to_dict() for block in blocks],
    }


def execute_clues_tool(
    *,
    conn: sqlite3.Connection,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    """Execute `worktrack_clues` reading local ambient `clues` rows if present."""
    raw_limit = arguments.get("limit", DEFAULT_CLUES_LIMIT)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise McpRequestError("Argument 'limit' must be an integer.")
    limit = max(MIN_CLUES_LIMIT, min(MAX_CLUES_LIMIT, raw_limit))

    has_table = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clues'"
        ).fetchone()
        is not None
    )
    if not has_table:
        return {"clues": [], "count": 0}

    raw_source = arguments.get("source")
    if isinstance(raw_source, str) and raw_source.strip():
        rows = conn.execute(
            "SELECT * FROM clues WHERE source = ? ORDER BY rowid DESC LIMIT ?",
            (raw_source.strip(), limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM clues ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    clues = [dict(row) for row in rows]
    return {"clues": clues, "count": len(clues)}


def execute_stage_outbox_rollup_tool(
    *,
    rollup_engine: RollupEngine,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    """Compute and stage `RollupProposals` into `OutboxStore` with `status='staged'`."""
    raw_sink = arguments.get("sink")
    if not isinstance(raw_sink, str) or not raw_sink.strip():
        raise McpRequestError("Argument 'sink' is required and must be a non-empty string.")
    sink = raw_sink.strip().lower()
    since = parse_optional_utc(arguments.get("since"), field_name="since")
    until = parse_optional_utc(arguments.get("until"), field_name="until")
    skip_unchanged = bool(arguments.get("skip_unchanged", True))

    raw_quantum = arguments.get("rounding_quantum_seconds", DEFAULT_ROUNDING_QUANTUM_SECONDS)
    if isinstance(raw_quantum, bool) or not isinstance(raw_quantum, int):
        raise McpRequestError("Argument 'rounding_quantum_seconds' must be an integer.")

    try:
        policy = RollupPolicy(
            grouping_window=str(arguments.get("grouping_window", DEFAULT_GROUPING_WINDOW)),
            rounding_quantum_seconds=raw_quantum,
            rounding_mode=str(arguments.get("rounding_mode", DEFAULT_ROUNDING_MODE)),
            redact_notes=bool(arguments.get("redact_notes", True)),
            flush_cadence=str(arguments.get("flush_cadence", DEFAULT_FLUSH_CADENCE)),
        )
        preview = rollup_engine.preview_rollup(sink=sink, policy=policy, since=since, until=until)
        staged = rollup_engine.stage_rollup(
            sink=sink,
            policy=policy,
            since=since,
            until=until,
            skip_unchanged=skip_unchanged,
        )
    except Exception as exc:
        raise McpRequestError(str(exc)) from exc

    return {
        "sink": sink,
        "status": OUTBOX_STATUS_STAGED,
        "staged_count": len(staged),
        "staged_items": [serialize_outbox_item(item) for item in staged],
        "diffs": [diff.to_dict() for diff in preview.diffs],
    }


def execute_propose_retrosplit_tool(
    *,
    ledger: Ledger,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    """Compute a non-destructive `RetroSplit` preview for a closed `Interval`."""
    raw_interval_id = arguments.get("interval_id")
    raw_acts = arguments.get("activity_ids")
    if not isinstance(raw_interval_id, str) or not raw_interval_id.strip():
        raise McpRequestError("Argument 'interval_id' must be a non-empty string.")
    if not isinstance(raw_acts, Sequence) or isinstance(raw_acts, (str, bytes)):
        raise McpRequestError("Argument 'activity_ids' must be a non-empty list of IDs.")
    activity_ids = [str(act_id).strip() for act_id in raw_acts if str(act_id).strip()]
    if len(activity_ids) < MIN_RETROSPLIT_TARGETS:
        raise McpRequestError("At least one target Activity ID is required for retrosplit.")

    try:
        interval = ledger.get_interval(raw_interval_id.strip())
    except Exception as exc:
        raise McpRequestError(str(exc)) from exc
    if interval.duration_seconds is None:
        raise McpRequestError("Cannot propose retrosplit for an open Interval.")

    weights = _resolve_retrosplit_weights(arguments.get("weights"), len(activity_ids))
    total_secs = interval.duration_seconds
    slices = [
        {
            "activity_id": act_id,
            "weight": weight,
            "duration_seconds": round(total_secs * weight, 3),
        }
        for act_id, weight in zip(activity_ids, weights, strict=True)
    ]
    return {
        "interval_id": interval.id,
        "total_duration_seconds": total_secs,
        "status": "proposed",
        "slices": slices,
    }


def _resolve_retrosplit_weights(raw_weights: object, count: int) -> list[float]:
    """Normalize optional `weights` list or default to equal weights across `count`."""
    if raw_weights is None:
        equal_share = 1.0 / count
        return [equal_share] * count
    if not isinstance(raw_weights, Sequence) or isinstance(raw_weights, (str, bytes)):
        raise McpRequestError("Argument 'weights' must be a list of positive numbers.")
    if len(raw_weights) != count:
        raise McpRequestError("Length of 'weights' must match 'activity_ids'.")
    numeric = [_coerce_positive_weight(val) for val in raw_weights]
    total = sum(numeric)
    return [val / total for val in numeric]


def _coerce_positive_weight(raw_val: object) -> float:
    """Validate and convert a single retrosplit weight into a positive float."""
    if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
        raise McpRequestError("All retrosplit weights must be numeric.")
    numeric = float(raw_val)
    if numeric <= ZERO_WEIGHT_SUM:
        raise McpRequestError("All retrosplit weights must be strictly positive.")
    return numeric


# endregion
