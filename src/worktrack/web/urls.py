"""Canonical URL routing table for `worktrack.web` (`/api/v1/...`, `/healthz`, `/metrics`)."""

from __future__ import annotations

from django.urls import URLPattern, path
from drf_spectacular.views import SpectacularAPIView

from worktrack.web.apps import neutralize_lazy_admin_site
from worktrack.web.views_calendar_hub_exports import (
    CalendarStageFocusView,
    CalendarSyncView,
    EntityAttributionExportView,
    HubCatalogView,
    HubRollupsView,
    WorkplaceTaxExportView,
)
from worktrack.web.views_clues_reconciler import (
    ClueGitSyncView,
    ClueListView,
    ReconcilerApplyView,
    ReconcilerInspectView,
    ReconcilerProposeView,
    SensorEvaluateView,
)
from worktrack.web.views_core import (
    ActivityDetailView,
    ActivityListCreateView,
    IntervalBindView,
    IntervalClockInView,
    IntervalClockOutView,
    IntervalDetailView,
    IntervalListCreateView,
    IntervalSplitView,
)
from worktrack.web.views_graph import (
    AttributionDetailView,
    AttributionListCreateView,
    AttributionSharesView,
    ContextFactListCreateView,
    EntityDetailView,
    EntityListCreateView,
    FacetDetailView,
    FacetListCreateView,
)
from worktrack.web.views_oauth import OAuthExchangeView, OAuthRefreshView
from worktrack.web.views_outbox import (
    EgressLedgerListView,
    OutboxApproveView,
    OutboxFlushView,
    OutboxItemDetailView,
    OutboxItemListView,
    OutboxPreviewView,
    OutboxRejectView,
)
from worktrack.web.views_planning import (
    AgendaItemDetailView,
    AgendaItemListCreateView,
    FocusBlockDetailView,
    FocusBlockListCreateView,
    NowInsightView,
    TodayInsightView,
    WeekInsightView,
)
from worktrack.web.views_telemetry import HealthzView, MetricsExpositionView

neutralize_lazy_admin_site()

urlpatterns: list[URLPattern] = [
    path("healthz", HealthzView.as_view(), name="healthz"),
    path("metrics", MetricsExpositionView.as_view(), name="metrics"),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="api_v1_schema"),
    path("api/v1/activities/", ActivityListCreateView.as_view(), name="api_v1_activities_list"),
    path(
        "api/v1/activities/<str:activity_id>/",
        ActivityDetailView.as_view(),
        name="api_v1_activities_detail",
    ),
    path("api/v1/intervals/", IntervalListCreateView.as_view(), name="api_v1_intervals_list"),
    path(
        "api/v1/intervals/clock-in/",
        IntervalClockInView.as_view(),
        name="api_v1_intervals_clock_in",
    ),
    path(
        "api/v1/intervals/clock-out/",
        IntervalClockOutView.as_view(),
        name="api_v1_intervals_clock_out",
    ),
    path(
        "api/v1/intervals/<str:interval_id>/",
        IntervalDetailView.as_view(),
        name="api_v1_intervals_detail",
    ),
    path(
        "api/v1/intervals/<str:interval_id>/bind/",
        IntervalBindView.as_view(),
        name="api_v1_intervals_bind",
    ),
    path(
        "api/v1/intervals/<str:interval_id>/split/",
        IntervalSplitView.as_view(),
        name="api_v1_intervals_split",
    ),
    path("api/v1/entities/", EntityListCreateView.as_view(), name="api_v1_entities_list"),
    path(
        "api/v1/entities/<str:entity_id>/",
        EntityDetailView.as_view(),
        name="api_v1_entities_detail",
    ),
    path(
        "api/v1/attributions/",
        AttributionListCreateView.as_view(),
        name="api_v1_attributions_list",
    ),
    path(
        "api/v1/attributions/shares/",
        AttributionSharesView.as_view(),
        name="api_v1_attributions_shares",
    ),
    path(
        "api/v1/attributions/<str:attribution_id>/",
        AttributionDetailView.as_view(),
        name="api_v1_attributions_detail",
    ),
    path("api/v1/facets/", FacetListCreateView.as_view(), name="api_v1_facets_list"),
    path("api/v1/facets/<str:key>/", FacetDetailView.as_view(), name="api_v1_facets_detail"),
    path(
        "api/v1/context-facts/",
        ContextFactListCreateView.as_view(),
        name="api_v1_context_facts_list",
    ),
    path(
        "api/v1/agenda/items/",
        AgendaItemListCreateView.as_view(),
        name="api_v1_agenda_items_list",
    ),
    path(
        "api/v1/agenda/items/<str:item_id>/",
        AgendaItemDetailView.as_view(),
        name="api_v1_agenda_items_detail",
    ),
    path(
        "api/v1/agenda/focus-blocks/",
        FocusBlockListCreateView.as_view(),
        name="api_v1_focus_blocks_list",
    ),
    path(
        "api/v1/agenda/focus-blocks/<str:block_id>/",
        FocusBlockDetailView.as_view(),
        name="api_v1_focus_blocks_detail",
    ),
    path("api/v1/insights/now/", NowInsightView.as_view(), name="api_v1_insights_now"),
    path("api/v1/insights/today/", TodayInsightView.as_view(), name="api_v1_insights_today"),
    path("api/v1/insights/week/", WeekInsightView.as_view(), name="api_v1_insights_week"),
    path(
        "api/v1/outbox/items/",
        OutboxItemListView.as_view(),
        name="api_v1_outbox_items_list",
    ),
    path(
        "api/v1/outbox/items/<str:item_id>/",
        OutboxItemDetailView.as_view(),
        name="api_v1_outbox_items_detail",
    ),
    path("api/v1/outbox/preview/", OutboxPreviewView.as_view(), name="api_v1_outbox_preview"),
    path(
        "api/v1/outbox/items/<str:item_id>/approve/",
        OutboxApproveView.as_view(),
        name="api_v1_outbox_approve",
    ),
    path(
        "api/v1/outbox/items/<str:item_id>/reject/",
        OutboxRejectView.as_view(),
        name="api_v1_outbox_reject",
    ),
    path("api/v1/outbox/flush/", OutboxFlushView.as_view(), name="api_v1_outbox_flush"),
    path(
        "api/v1/egress-ledger/",
        EgressLedgerListView.as_view(),
        name="api_v1_egress_ledger_list",
    ),
    path(
        "api/v1/oauth/<str:provider>/exchange/",
        OAuthExchangeView.as_view(),
        name="api_v1_oauth_exchange",
    ),
    path(
        "api/v1/oauth/<str:provider>/refresh/",
        OAuthRefreshView.as_view(),
        name="api_v1_oauth_refresh",
    ),
    path("api/v1/clues/", ClueListView.as_view(), name="api_v1_clues_list"),
    path(
        "api/v1/clues/git-sync/",
        ClueGitSyncView.as_view(),
        name="api_v1_clues_git_sync",
    ),
    path(
        "api/v1/sensors/evaluate/",
        SensorEvaluateView.as_view(),
        name="api_v1_sensors_evaluate",
    ),
    path(
        "api/v1/reconciler/inspect/",
        ReconcilerInspectView.as_view(),
        name="api_v1_reconciler_inspect",
    ),
    path(
        "api/v1/reconciler/propose/",
        ReconcilerProposeView.as_view(),
        name="api_v1_reconciler_propose",
    ),
    path(
        "api/v1/reconciler/apply/",
        ReconcilerApplyView.as_view(),
        name="api_v1_reconciler_apply",
    ),
    path(
        "api/v1/calendar/sync/",
        CalendarSyncView.as_view(),
        name="api_v1_calendar_sync",
    ),
    path(
        "api/v1/calendar/stage-focus/",
        CalendarStageFocusView.as_view(),
        name="api_v1_calendar_stage_focus",
    ),
    path("api/v1/hub/rollups/", HubRollupsView.as_view(), name="api_v1_hub_rollups"),
    path("api/v1/hub/catalog/", HubCatalogView.as_view(), name="api_v1_hub_catalog"),
    path(
        "api/v1/exports/workplace-tax/",
        WorkplaceTaxExportView.as_view(),
        name="api_v1_exports_workplace_tax",
    ),
    path(
        "api/v1/exports/entity-attribution/",
        EntityAttributionExportView.as_view(),
        name="api_v1_exports_entity_attribution",
    ),
]
