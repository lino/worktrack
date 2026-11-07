"""DRF `APIView` endpoints for Wave 2-3 `Clue`, `Sensor`, and `Reconciler`/`RetroSplit`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from worktrack.cli.commands_clues_sensors import (
    _build_sensor_rule_from_mapping,
    _to_str_tuple,
    clues_to_reconcile_inputs,
    parse_sensor_rules_json,
)
from worktrack.cli.context import parse_cli_timestamp, resolve_or_create_activity
from worktrack.collectors import (
    Clue,
    ClueValidationError,
    CollectorSyncSummary,
    GitClueCollector,
    SubprocessGitLogRunner,
)
from worktrack.reconciler import (
    PlausibilityReconciler,
    RetroSplitEngine,
    RetroSplitProposal,
)
from worktrack.sensors import (
    LocalWorkstationProbe,
    WorkplaceSensorEngine,
    WorkplaceSensorRule,
    WorkstationSnapshot,
)
from worktrack.web.serializers_clues_reconciler import (
    ClueGitSyncRequestSerializer,
    ClueGitSyncResponseSerializer,
    ClueListSerializer,
    ReconcilerApplyResponseSerializer,
    ReconcilerInspectResponseSerializer,
    ReconcilerProposeRequestSerializer,
    ReconcilerProposeResponseSerializer,
    SensorEvaluateRequestSerializer,
    SensorEvaluateResponseSerializer,
    serialize_clue_dict,
    serialize_reconciler_inspect,
    serialize_retrosplit_apply,
    serialize_retrosplit_proposal,
    serialize_sensor_eval,
    serialize_sync_summary_dict,
)
from worktrack.web.services import WebServiceContainer, get_active_web_services
from worktrack.web.views_core import parse_dt_param

# region Constants
HOURS_PER_DAY: Final = 24
ENV_SENSOR_RULES_JSON: Final = "WORKTRACK_SENSOR_RULES_JSON"
# endregion


# region Validation
def _resolve_display_count(raw: Mapping[str, Any]) -> int | None:
    """Resolve explicit `display_count` or fall back to `len(displays)`."""
    explicit = raw.get("display_count")
    if explicit is not None:
        return int(explicit)
    displays = _to_str_tuple(raw.get("displays"))
    return len(displays) if displays else None


def _build_snapshot_from_mapping(raw: Mapping[str, Any] | None) -> WorkstationSnapshot | None:
    """Build a `WorkstationSnapshot` from an optional request dictionary."""
    if raw is None:
        return None
    raw_ts = raw.get("captured_at")
    captured_at = (
        parse_cli_timestamp(str(raw_ts), field_name="captured_at") if raw_ts else datetime.now(UTC)
    )
    ips = (*_to_str_tuple(raw.get("ip_addresses")), *_to_str_tuple(raw.get("ipv4_addresses")))
    ifaces = (
        *_to_str_tuple(raw.get("active_interfaces")),
        *_to_str_tuple(raw.get("vpn_interfaces")),
    )
    return WorkstationSnapshot(
        captured_at=captured_at,
        ip_addresses=ips,
        wifi_ssid=raw.get("wifi_ssid"),
        active_interfaces=ifaces,
        display_count=_resolve_display_count(raw),
        hostname=raw.get("hostname"),
    )


def _resolve_sensor_rules(
    raw_rules: Sequence[Mapping[str, Any]] | None,
    services: WebServiceContainer,
) -> tuple[WorkplaceSensorRule, ...]:
    """Build `WorkplaceSensorRule` tuple from request `rules` or environment JSON."""
    if raw_rules:
        return tuple(_build_sensor_rule_from_mapping(item) for item in raw_rules)
    env_json = (services.environ or {}).get(ENV_SENSOR_RULES_JSON, "")
    return parse_sensor_rules_json(env_json)


def _upsert_one_submitted_clue(
    ledger: Any,
    store: Any,
    entry: Mapping[str, Any],
) -> tuple[Clue, bool]:
    """Validate and upsert a single submitted clue dictionary into `ClueStore`."""
    ext_key = str(entry.get("external_key") or entry.get("external_id") or "").strip()
    if not ext_key:
        raise ClueValidationError("Each clue requires external_key or external_id.")
    occ_dt = parse_cli_timestamp(str(entry["occurred_at"]), field_name="occurred_at")
    act_id = entry.get("activity_id")
    inf_key = entry.get("inferred_activity_key")
    if act_id is None and inf_key:
        found = ledger.find_activity_by_external_ref(str(inf_key))
        act_id = found.id if found is not None else None
    clue = store.upsert_clue(
        source=str(entry.get("source", "git")),
        external_key=ext_key,
        occurred_at=occ_dt,
        summary=str(entry["summary"]),
        inferred_activity_key=str(inf_key) if inf_key else None,
        activity_id=str(act_id) if act_id else None,
        confidence=float(entry.get("confidence", 0.85)),
        metadata=dict(entry.get("metadata") or {}),
    )
    return clue, act_id is not None


def _upsert_submitted_clues(
    services: WebServiceContainer,
    items: Sequence[Mapping[str, Any]],
) -> CollectorSyncSummary:
    """Upsert a batch of directly submitted clue dictionaries into `ClueStore`."""
    upserted: list[Clue] = []
    linked = 0
    with services.open_ledger() as ledger, services.open_clue_store() as store:
        for entry in items:
            clue, was_linked = _upsert_one_submitted_clue(ledger, store, entry)
            upserted.append(clue)
            linked += int(was_linked)
    return CollectorSyncSummary(
        commits_scanned=len(upserted),
        forge_records_scanned=0,
        clues_upserted=len(upserted),
        activities_linked=linked,
        clues=tuple(upserted),
    )


def _generate_retrosplit_proposal(
    services: WebServiceContainer,
    data: Mapping[str, Any],
) -> RetroSplitProposal:
    """Resolve activities and generate a `RetroSplitProposal` from validated request data."""
    with (
        services.open_ledger() as ledger,
        services.open_planner(ledger=ledger) as planner,
        services.open_clue_store() as clue_store,
    ):
        interval = ledger.get_interval(str(data["interval_id"]))
        raw_ids: list[str] = list(data["activity_ids"])
        resolved_ids = [
            resolve_or_create_activity(ledger, ident, create_if_missing=False).id
            for ident in raw_ids
        ]
        raw_weights = data.get("weights")
        weights_map: dict[str, float] | None = None
        if isinstance(raw_weights, Mapping):
            weights_map = {
                resolve_or_create_activity(ledger, str(k), create_if_missing=False).id: float(v)
                for k, v in raw_weights.items()
            }
        clues = clue_store.list_clues(since=interval.started_at, until=interval.ended_at)
        engine = RetroSplitEngine(ledger, agenda_planner=planner, telemetry=services.telemetry)
        return engine.propose_split(
            interval,
            strategy=str(data.get("strategy", "proportional")),
            activity_ids=resolved_ids,
            clues=clues_to_reconcile_inputs(clues),
            weights=weights_map,
        )


# endregion


# region Views
class ClueListView(APIView):  # type: ignore[misc]
    """List ambient `Clue` records stored in `ClueStore`."""

    @extend_schema(
        operation_id="v1_clues_list",
        parameters=[
            OpenApiParameter("source", OpenApiTypes.STR, description="Filter by clue source."),
            OpenApiParameter("start", OpenApiTypes.DATETIME, description="Inclusive start UTC."),
            OpenApiParameter("end", OpenApiTypes.DATETIME, description="Exclusive end UTC."),
        ],
        responses={200: ClueListSerializer},
    )
    def get(self, request: Request) -> Response:
        """Return filtered `Clue` records."""
        services = get_active_web_services()
        source = request.query_params.get("source")
        start_dt = parse_dt_param(request, "start")
        end_dt = parse_dt_param(request, "end")
        with services.open_clue_store() as store:
            clues = store.list_clues(
                source=source.strip() if source and source.strip() else None,
                since=start_dt,
                until=end_dt,
            )
        return Response({"clues": [serialize_clue_dict(c) for c in clues]})


class ClueGitSyncView(APIView):  # type: ignore[misc]
    """Trigger local `GitClueCollector` sync or upsert a batch of submitted `Clues`."""

    @extend_schema(
        operation_id="v1_clues_git_sync",
        request=ClueGitSyncRequestSerializer,
        responses={200: ClueGitSyncResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Execute Git clue sync or batch clue upsert."""
        serializer = ClueGitSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()

        submitted_clues = data.get("clues")
        if submitted_clues:
            summary = _upsert_submitted_clues(services, submitted_clues)
            return Response(serialize_sync_summary_dict(summary), status=status.HTTP_200_OK)

        repo_raw = data.get("repo_path")
        repo_paths = (Path(str(repo_raw)).expanduser(),) if repo_raw else ()
        since_dt = (
            parse_cli_timestamp(str(data["since"]), field_name="since")
            if data.get("since")
            else None
        )
        until_dt = (
            parse_cli_timestamp(str(data["until"]), field_name="until")
            if data.get("until")
            else None
        )
        with services.open_ledger() as ledger, services.open_clue_store() as clue_store:
            collector = GitClueCollector(
                clue_store=clue_store,
                git_runner=SubprocessGitLogRunner(),
                ledger=ledger,
                telemetry=services.telemetry,
            )
            summary = collector.sync(
                repo_paths=repo_paths,
                author_pattern=data.get("author"),
                since=since_dt,
                until=until_dt,
                ledger=ledger,
            )
        return Response(serialize_sync_summary_dict(summary), status=status.HTTP_200_OK)


class SensorEvaluateView(APIView):  # type: ignore[misc]
    """Evaluate `WorkplaceSensorEngine` rules against a probed or supplied snapshot."""

    @extend_schema(
        operation_id="v1_sensors_evaluate",
        request=SensorEvaluateRequestSerializer,
        responses={200: SensorEvaluateResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Evaluate workplace sensor rules and optionally assert `workplace` `ContextFact`."""
        serializer = SensorEvaluateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services = get_active_web_services()

        rules = _resolve_sensor_rules(data.get("rules"), services)
        snapshot = _build_snapshot_from_mapping(data.get("snapshot"))
        with services.open_ledger() as ledger, services.open_clue_store() as clue_store:
            engine = WorkplaceSensorEngine(
                rules=rules,
                probe=LocalWorkstationProbe(),
                clue_store=clue_store,
                ledger=ledger,
            )
            suggestion = engine.evaluate(
                snapshot=snapshot,
                scope_date=data.get("scope_date"),
                assert_fact=bool(data.get("apply", False)),
                overwrite_manual=bool(data.get("overwrite_manual", False)),
            )
        return Response(serialize_sensor_eval(suggestion), status=status.HTTP_200_OK)


class ReconcilerInspectView(APIView):  # type: ignore[misc]
    """Inspect `[start, end)` window for plausibility gaps and unallocated intervals."""

    @extend_schema(
        operation_id="v1_reconciler_inspect",
        parameters=[
            OpenApiParameter("start", OpenApiTypes.DATETIME, description="Window start UTC."),
            OpenApiParameter("end", OpenApiTypes.DATETIME, description="Window end UTC."),
        ],
        responses={200: ReconcilerInspectResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        """Inspect plausibility gaps and candidate clues in `[start, end)`."""
        services = get_active_web_services()
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = parse_dt_param(request, "start") or today_start
        end_dt = parse_dt_param(request, "end") or (start_dt + timedelta(hours=HOURS_PER_DAY))
        with (
            services.open_ledger() as ledger,
            services.open_planner(ledger=ledger) as planner,
            services.open_clue_store() as clue_store,
        ):
            clues = clue_store.list_clues(since=start_dt, until=end_dt)
            reconciler = PlausibilityReconciler(ledger, agenda_planner=planner)
            report = reconciler.inspect(
                start_dt,
                end_dt,
                clues=clues_to_reconcile_inputs(clues),
            )
        return Response(serialize_reconciler_inspect(report, clues), status=status.HTTP_200_OK)


class ReconcilerProposeView(APIView):  # type: ignore[misc]
    """Generate a previewable `RetroSplitProposal` for an unallocated/chaotic `Interval`."""

    @extend_schema(
        operation_id="v1_reconciler_propose",
        request=ReconcilerProposeRequestSerializer,
        responses={200: ReconcilerProposeResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Generate and return a `RetroSplitProposal`."""
        serializer = ReconcilerProposeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services = get_active_web_services()
        proposal = _generate_retrosplit_proposal(services, serializer.validated_data)
        return Response(serialize_retrosplit_proposal(proposal), status=status.HTTP_200_OK)


class ReconcilerApplyView(APIView):  # type: ignore[misc]
    """Generate and atomically apply a `RetroSplitProposal` to the `Ledger`."""

    @extend_schema(
        operation_id="v1_reconciler_apply",
        request=ReconcilerProposeRequestSerializer,
        responses={200: ReconcilerApplyResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        """Generate and apply a `RetroSplitProposal`."""
        serializer = ReconcilerProposeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services = get_active_web_services()
        proposal = _generate_retrosplit_proposal(services, serializer.validated_data)
        with services.open_ledger() as ledger, services.open_planner(ledger=ledger) as planner:
            engine = RetroSplitEngine(
                ledger,
                agenda_planner=planner,
                telemetry=services.telemetry,
            )
            result = engine.apply_proposal(proposal)
        return Response(serialize_retrosplit_apply(result), status=status.HTTP_200_OK)


# endregion
