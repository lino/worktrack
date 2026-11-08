/**
 * Deterministic initial seed helpers for the Worktrack cockpit store.
 */
import { useRepo } from 'pinia-orm';
import {
  ActivityModel,
  AttributionModel,
  ContextFactModel,
  EntityModel,
  IntervalModel,
} from './entities';
import {
  AgendaItemModel,
  EgressReceiptModel,
  FocusBlockModel,
  OutboxItemModel,
} from './planning_and_outbox';
import {
  ClueModel,
  ExportReportModel,
  ReconcileProposalModel,
  SensorSuggestionModel,
} from './reconciler_and_exports';
import { seedReportingAndAbsences } from './reporting';

/**
 * Seed default `Activity` records into `pinia-orm`.
 */
function seedActivities(): void {
  useRepo(ActivityModel).save([
    {
      id: 'act-proj-142',
      name: 'PROJ-142 Outbox Rollup Gate',
      kind: 'targeted',
      source: 'jira',
      external_ref: 'PROJ-142',
      rollup_target_ref: 'PROJ-142',
      tags: ['privacy', 'outbox'],
      archived: false,
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
    {
      id: 'act-standing-review',
      name: 'Code Review & Architecture Triage',
      kind: 'standing',
      source: 'local',
      external_ref: null,
      rollup_target_ref: 'PROJ-100',
      tags: ['standing', 'review'],
      archived: false,
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
  ]);
}

/**
 * Seed default structured and chaotic `Interval` records into `pinia-orm`.
 */
function seedIntervals(): void {
  useRepo(IntervalModel).save([
    {
      id: 'int-active-1',
      activity_id: 'act-proj-142',
      started_at: '2026-11-07T09:00:00Z',
      ended_at: '2026-11-07T10:30:00Z',
      duration_seconds: 5400,
      capture_mode: 'structured',
      notes: 'Implemented Outbox preview diff table (redacted before Sink)',
      tags: ['deep-work'],
      is_open: true,
      is_allocated: true,
      created_at: '2026-11-07T09:00:00Z',
      updated_at: '2026-11-07T10:30:00Z',
    },
    {
      id: 'int-chaotic-2',
      activity_id: null,
      started_at: '2026-11-07T10:30:00Z',
      ended_at: '2026-11-07T11:15:00Z',
      duration_seconds: 2700,
      capture_mode: 'chaotic',
      notes: 'Unallocated ad-hoc debugging block awaiting RetroSplit',
      tags: ['chaotic'],
      is_open: false,
      is_allocated: false,
      created_at: '2026-11-07T10:30:00Z',
      updated_at: '2026-11-07T11:15:00Z',
    },
  ]);
}

/**
 * Seed default `Entity` records into `pinia-orm`.
 */
function seedEntities(): void {
  useRepo(EntityModel).save([
    {
      id: 'ent-cust-babelsberg',
      kind: 'customer',
      key: 'BABELSBERG',
      name: 'Studio Babelsberg',
      slug: 'customer:babelsberg',
      metadata: { tier: 'internal' },
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
    {
      id: 'ent-proj-wt',
      kind: 'project',
      key: 'WT',
      name: 'Worktrack Privacy Ledger',
      slug: 'project:wt',
      metadata: {},
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
  ]);
}

/**
 * Seed default `Attribution` and `ContextFact` records into `pinia-orm`.
 */
function seedAttributionsAndFacts(): void {
  useRepo(AttributionModel).save([
    {
      id: 'attr-1',
      source_activity_id: 'act-proj-142',
      source_entity_id: null,
      target_entity_id: 'ent-proj-wt',
      weight: 0.7,
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
    {
      id: 'attr-2',
      source_activity_id: 'act-proj-142',
      source_entity_id: null,
      target_entity_id: 'ent-cust-babelsberg',
      weight: 0.3,
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
  ]);

  useRepo(ContextFactModel).save([
    {
      id: 'fact-workplace-today',
      facet_key: 'workplace',
      value: 'home_office',
      scope_date: '2026-11-07',
      interval_id: null,
      source: 'manual',
      updated_at: '2026-11-07T08:00:00Z',
    },
  ]);
}

/**
 * Seed default `AgendaItem` and `FocusBlock` records into `pinia-orm`.
 */
function seedAgendaAndFocusBlocks(): void {
  useRepo(AgendaItemModel).save([
    {
      id: 'ag-1',
      activity_id: 'act-proj-142',
      horizon: 'today',
      priority: 1,
      status: 'in_progress',
      budget_seconds: 10800,
      daily_budget_minutes: 180,
      deadline: '2026-11-08',
      notes: 'Complete Outbox diff preview & EgressLedger verification',
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
    {
      id: 'ag-2',
      activity_id: 'act-standing-review',
      horizon: 'this_week',
      priority: 2,
      status: 'planned',
      budget_seconds: 7200,
      daily_budget_minutes: 60,
      deadline: '2026-11-12',
      notes: 'Weekly architecture and ADR review queue',
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
  ]);

  useRepo(FocusBlockModel).save([
    {
      id: 'fb-morning',
      activity_id: 'act-proj-142',
      label: 'Deep Focus: Outbox & Cockpit',
      started_at: '2026-11-07T09:00:00Z',
      ended_at: '2026-11-07T11:00:00Z',
      duration_seconds: 7200,
      expose_busy: true,
      notes: 'Publish opaque busy marker only',
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    },
  ]);
}

/**
 * Seed default `OutboxItem` and `EgressReceipt` records into `pinia-orm`.
 */
function seedOutboxAndEgressReceipts(): void {
  useRepo(OutboxItemModel).save([
    {
      id: 'out-1',
      idempotency_key: 'jira:PROJ-142:2026-11-07',
      sink: 'jira',
      target_ref: 'PROJ-142',
      window_start: '2026-11-07T00:00:00Z',
      window_end: '2026-11-07T23:59:59Z',
      raw_duration_seconds: 5340,
      rounded_duration_seconds: 5400,
      summary: 'PROJ-142 Outbox Rollup Gate (notes redacted)',
      payload_preview: { issueKey: 'PROJ-142', timeSpentSeconds: 5400 },
      payload_sha256: '3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b',
      status: 'staged',
      cadence: 'eod',
      attempt_count: 0,
      last_error: null,
      rejection_reason: null,
      source_interval_ids: ['int-active-1'],
      created_at: '2026-11-07T17:00:00Z',
      updated_at: '2026-11-07T17:00:00Z',
    },
  ]);

  useRepo(EgressReceiptModel).save([
    {
      id: 'egr-1',
      outbox_item_id: 'out-1',
      idempotency_key: 'jira:PROJ-142:2026-11-06',
      sink: 'jira',
      destination_uri: 'https://worktrack.atlassian.net/rest/api/3/issue/PROJ-142/worklog',
      payload_sha256: '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08',
      outcome: 'ok',
      http_status: 201,
      external_receipt_id: 'worklog-40192',
      error_detail: null,
      trace_id: '4bf92f3577b34da6a3ce929d0e0e4736',
      recorded_at: '2026-11-06T18:00:00Z',
    },
  ]);
}

/**
 * Seed default Wave 2 `Clue` and `SensorSuggestion` records into `pinia-orm`.
 */
function seedCluesAndSensors(): void {
  useRepo(ClueModel).save([
    {
      id: 'clue-git-1',
      source: 'git',
      external_key: 'commit-a1b2c3d',
      external_id: 'commit-a1b2c3d',
      occurred_at: '2026-11-07T10:35:00Z',
      summary: 'feat(outbox): enforce SHA-256 receipt check [PROJ-142]',
      inferred_activity_key: 'PROJ-142',
      activity_id: 'act-proj-142',
      confidence: 0.95,
      metadata: { branch: 'feat/outbox' },
      created_at: '2026-11-07T10:36:00Z',
    },
    {
      id: 'clue-forge-2',
      source: 'github',
      external_key: 'mr-88',
      external_id: 'mr-88',
      occurred_at: '2026-11-07T10:50:00Z',
      summary: 'Review architecture ADR for Standing Activities [PROJ-100]',
      inferred_activity_key: 'PROJ-100',
      activity_id: 'act-standing-review',
      confidence: 0.85,
      metadata: { repository: 'worktrack' },
      created_at: '2026-11-07T10:51:00Z',
    },
  ]);

  useRepo(SensorSuggestionModel).save([
    {
      scope_date: '2026-11-07',
      workplace: 'home_office',
      rule_id: 'rule-home-wifi',
      confidence: 0.95,
      matched_signals: ['wifi_ssid=HomeFiber5G', 'display_count=2'],
      snapshot: { wifi_ssid: 'HomeFiber5G', display_count: 2 },
      clue_id: 'clue-sensor-1',
      context_fact_id: 'fact-workplace-today',
    },
  ]);
}

/**
 * Seed default Wave 2 `ReconcileProposal` (`RetroSplit`) records into `pinia-orm`.
 */
function seedReconcilerProposals(): void {
  useRepo(ReconcileProposalModel).save([
    {
      proposal_id: 'prop-demo-1',
      strategy: 'proportional',
      interval_ids: ['int-chaotic-2'],
      total_duration_seconds: 2700,
      allocated_duration_seconds: 2700,
      is_conserved: true,
      normalized_weights: { 'act-proj-142': 0.67, 'act-standing-review': 0.33 },
      slices: [
        {
          interval_id: 'int-chaotic-2',
          activity_id: 'act-proj-142',
          activity_name: 'PROJ-142 Outbox Rollup Gate',
          started_at: '2026-11-07T10:30:00Z',
          ended_at: '2026-11-07T11:00:00Z',
          duration_seconds: 1800,
          weight: 2.0,
          share: 0.67,
          notes: 'RetroSplit slice (PROJ-142)',
          tags: ['retrosplit'],
        },
        {
          interval_id: 'int-chaotic-2',
          activity_id: 'act-standing-review',
          activity_name: 'Code Review & Architecture Triage',
          started_at: '2026-11-07T11:00:00Z',
          ended_at: '2026-11-07T11:15:00Z',
          duration_seconds: 900,
          weight: 1.0,
          share: 0.33,
          notes: 'RetroSplit slice (PROJ-100)',
          tags: ['retrosplit'],
        },
      ],
      created_at: '2026-11-07T11:20:00Z',
    },
  ]);
}

/**
 * Seed default Wave 4 `ExportReport` records for annual tax and `Entity` attribution.
 */
function seedExportReports(): void {
  const baseTaxReport = {
    start_date: '2026-01-01',
    end_date: '2026-12-31',
    total_classified_days: 1,
    total_tracked_seconds: 8100,
    total_tracked_hours: 2.25,
    days_by_workplace: { home_office: 1 },
  };
  useRepo(ExportReportModel).save([
    {
      id: 'workplace-tax:2026:markdown',
      kind: 'workplace-tax',
      format: 'markdown',
      rendered: '| Workplace | Days | Hours |\n| --- | --- | --- |\n| home_office | 1 | 2.25 |',
      report: baseTaxReport,
      updated_at: '2026-11-07T18:00:00Z',
    },
    {
      id: 'workplace-tax:2026:csv',
      kind: 'workplace-tax',
      format: 'csv',
      rendered: 'workplace,day_count,tracked_hours\nhome_office,1,2.25\n',
      report: baseTaxReport,
      updated_at: '2026-11-07T18:00:00Z',
    },
    {
      id: 'workplace-tax:2026:json',
      kind: 'workplace-tax',
      format: 'json',
      rendered: JSON.stringify({ workplace: 'home_office', day_count: 1, tracked_hours: 2.25 }),
      report: baseTaxReport,
      updated_at: '2026-11-07T18:00:00Z',
    },
    {
      id: 'entity-attribution:2026:markdown',
      kind: 'entity-attribution',
      format: 'markdown',
      rendered:
        '| Entity | Kind | Share |\n| --- | --- | --- |\n| Worktrack Privacy Ledger | project | 70% |\n| Studio Babelsberg | customer | 30% |',
      report: { total_interval_seconds: 5400, total_interval_hours: 1.5 },
      updated_at: '2026-11-07T18:00:00Z',
    },
  ]);
}

/**
 * Populate normalized `pinia-orm` repositories with representative domain records
 * when the store is empty.
 */
export function seedCockpitDemoState(): void {
  if (useRepo(ActivityModel).all().length > 0) {
    return;
  }
  seedActivities();
  seedIntervals();
  seedEntities();
  seedAttributionsAndFacts();
  seedAgendaAndFocusBlocks();
  seedOutboxAndEgressReceipts();
  seedCluesAndSensors();
  seedReconcilerProposals();
  seedExportReports();
  seedReportingAndAbsences();
}
