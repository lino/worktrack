/**
 * Behavioural tests for `@hey-api/openapi-ts` generated types, normalized `pinia-orm`
 * repository models, and bounded transport telemetry counters.
 */
import { createPinia, setActivePinia } from 'pinia';
import { createORM, useRepo } from 'pinia-orm';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  ActivitySchema,
  AgendaItemSchema,
  AttributionSchema,
  ClueSchema,
  ContextFactSchema,
  EgressRecordSchema,
  EntitySchema,
  IntervalSchema,
  OutboxItemSchema,
  RetroSplitProposalSchema,
  WorkplaceSuggestionSchema,
  WorkplaceTaxExportResponseSchema,
} from '../src/api/generated';
import {
  ActivityModel,
  AgendaItemModel,
  AttributionModel,
  ClueModel,
  ContextFactModel,
  EgressReceiptModel,
  EntityModel,
  ExportReportModel,
  FocusBlockModel,
  IntervalModel,
  OutboxItemModel,
  ReconcileProposalModel,
  SensorSuggestionModel,
} from '../src/models';
import { classifyHttpStatus, WorktrackTransportClient } from '../src/transport/client';

describe('Normalized pinia-orm models and OpenAPI transport client', () => {
  beforeEach(() => {
    const pinia = createPinia().use(createORM());
    setActivePinia(pinia);
  });

  it('normalizes Activity, Interval, Entity, Attribution, ContextFact, AgendaItem, OutboxItem, and EgressReceipt in pinia-orm', () => {
    const activityPayload: ActivitySchema = {
      id: 'act-101',
      name: 'PROJ-101 Core Ledger',
      kind: 'targeted',
      source: 'jira',
      external_ref: 'PROJ-101',
      rollup_target_ref: 'PROJ-101',
      tags: ['backend', 'mvp'],
      archived: false,
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    };
    const intervalPayload: IntervalSchema = {
      id: 'int-201',
      activity_id: 'act-101',
      started_at: '2026-11-07T09:00:00Z',
      ended_at: '2026-11-07T10:30:00Z',
      duration_seconds: 5400,
      capture_mode: 'structured',
      notes: 'Implemented SQLite WAL ledger',
      tags: ['focus'],
      is_open: false,
      is_allocated: true,
      created_at: '2026-11-07T09:00:00Z',
      updated_at: '2026-11-07T10:30:00Z',
    };
    const entityPayload: EntitySchema = {
      id: 'ent-301',
      kind: 'project',
      key: 'PROJ',
      name: 'Worktrack Platform',
      slug: 'project:proj',
      metadata: { cost_center: 'CC-42' },
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    };
    const attributionPayload: AttributionSchema = {
      id: 'attr-401',
      source_activity_id: 'act-101',
      source_entity_id: null,
      target_entity_id: 'ent-301',
      weight: 0.75,
      created_at: '2026-11-07T08:05:00Z',
      updated_at: '2026-11-07T08:05:00Z',
    };
    const factPayload: ContextFactSchema = {
      id: 'fact-501',
      facet_key: 'workplace',
      value: 'home_office',
      scope_date: '2026-11-07',
      interval_id: null,
      source: 'manual',
      updated_at: '2026-11-07T08:00:00Z',
    };
    const agendaPayload: AgendaItemSchema = {
      id: 'ag-601',
      activity_id: 'act-101',
      horizon: 'today',
      priority: 1,
      status: 'in_progress',
      budget_seconds: 7200,
      daily_budget_minutes: 120,
      deadline: '2026-11-08',
      notes: 'Deliver MVP slice',
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    };
    const outboxPayload: OutboxItemSchema = {
      id: 'out-701',
      idempotency_key: 'idem-701',
      sink: 'jira',
      target_ref: 'PROJ-101',
      window_start: '2026-11-07T00:00:00Z',
      window_end: '2026-11-07T23:59:59Z',
      raw_duration_seconds: 5380,
      rounded_duration_seconds: 5400,
      summary: 'PROJ-101 Core Ledger (redacted)',
      payload_preview: { timeSpentSeconds: 5400 },
      payload_sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
      status: 'staged',
      cadence: 'eod',
      attempt_count: 0,
      last_error: null,
      rejection_reason: null,
      source_interval_ids: ['int-201'],
      created_at: '2026-11-07T17:00:00Z',
      updated_at: '2026-11-07T17:00:00Z',
    };
    const receiptPayload: EgressRecordSchema = {
      id: 'egr-801',
      outbox_item_id: 'out-701',
      idempotency_key: 'idem-701',
      sink: 'jira',
      destination_uri: 'https://example.atlassian.net/rest/api/3/issue/PROJ-101/worklog',
      payload_sha256: outboxPayload.payload_sha256,
      outcome: 'ok',
      http_status: 201,
      external_receipt_id: 'wl-9001',
      error_detail: null,
      trace_id: '4bf92f3577b34da6a3ce929d0e0e4736',
      recorded_at: '2026-11-07T17:05:00Z',
    };

    useRepo(ActivityModel).save(activityPayload);
    useRepo(IntervalModel).save(intervalPayload);
    useRepo(EntityModel).save(entityPayload);
    useRepo(AttributionModel).save(attributionPayload);
    useRepo(ContextFactModel).save(factPayload);
    useRepo(AgendaItemModel).save(agendaPayload);
    useRepo(FocusBlockModel).save({
      id: 'fb-650',
      activity_id: 'act-101',
      label: 'Deep Work: Ledger',
      started_at: '2026-11-07T09:00:00Z',
      ended_at: '2026-11-07T11:00:00Z',
      duration_seconds: 7200,
      expose_busy: true,
      notes: null,
      created_at: '2026-11-07T08:00:00Z',
      updated_at: '2026-11-07T08:00:00Z',
    });
    useRepo(OutboxItemModel).save(outboxPayload);
    useRepo(EgressReceiptModel).save(receiptPayload);

    const storedInterval = useRepo(IntervalModel).with('activity').find('int-201');
    expect(storedInterval?.activity?.name).toBe('PROJ-101 Core Ledger');
    expect(useRepo(AttributionModel).find('attr-401')?.weight).toBe(0.75);
    expect(useRepo(ContextFactModel).find('fact-501')?.value).toBe('home_office');
    expect(useRepo(AgendaItemModel).find('ag-601')?.budget_seconds).toBe(7200);
    expect(useRepo(FocusBlockModel).find('fb-650')?.expose_busy).toBe(true);
    expect(useRepo(OutboxItemModel).find('out-701')?.rounded_duration_seconds).toBe(5400);
    expect(useRepo(EgressReceiptModel).find('egr-801')?.http_status).toBe(201);
  });

  it('syncs OpenAPI responses into pinia-orm and increments bounded transport error counters on failure', async () => {
    const fetchMock = vi.fn();
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify([
          {
            id: 'act-999',
            name: 'Incident Triage',
            kind: 'standing',
            source: 'local',
            external_ref: null,
            rollup_target_ref: 'OPS-1',
            tags: ['ops'],
            archived: false,
            created_at: '2026-11-07T08:00:00Z',
            updated_at: '2026-11-07T08:00:00Z',
          },
        ]),
        {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
            'X-Fritz-Trace-Id': '4bf92f3577b34da6a3ce929d0e0e4736',
          },
        },
      ),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Gateway timeout' }), {
        status: 502,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    fetchMock.mockRejectedValueOnce(new Error('Socket hang up'));

    const client = new WorktrackTransportClient({
      baseUrl: 'http://127.0.0.1:8000',
      fetchImpl: fetchMock,
    });

    const activities = await client.fetchActivities();
    expect(activities).toHaveLength(1);
    expect(useRepo(ActivityModel).find('act-999')?.kind).toBe('standing');

    await expect(client.fetchOutboxItems()).rejects.toThrow(/502/);
    await expect(client.fetchEgressLedger()).rejects.toThrow(/Socket hang up/);

    const metrics = client.getTransportMetrics();
    expect(metrics.requestsTotal['/api/v1/activities/|2xx']).toBe(1);
    expect(metrics.errorsTotal['/api/v1/outbox/items/|5xx']).toBe(1);
    expect(metrics.errorsTotal['/api/v1/egress-ledger/|network_error']).toBe(1);
    expect(metrics.totalErrorCount).toBe(2);
    expect(metrics.lastTraceId).toBe('4bf92f3577b34da6a3ce929d0e0e4736');

    expect(classifyHttpStatus(200)).toBe('2xx');
    expect(classifyHttpStatus(302)).toBe('3xx');
    expect(classifyHttpStatus(404)).toBe('4xx');
    expect(classifyHttpStatus(503)).toBe('5xx');
  });

  it('normalizes ClueModel, SensorSuggestionModel, ReconcileProposalModel, and ExportReportModel via Wave 2-4 transport client', async () => {
    const cluePayload: ClueSchema = {
      id: 'clue-301',
      source: 'git',
      external_key: 'commit-deadbeef',
      external_id: 'commit-deadbeef',
      occurred_at: '2026-11-07T10:45:00Z',
      summary: 'feat(outbox): enforce SHA-256 receipt check [PROJ-142]',
      inferred_activity_key: 'PROJ-142',
      activity_id: 'act-proj-142',
      confidence: 0.95,
      metadata: { branch: 'feat/outbox' },
      created_at: '2026-11-07T10:46:00Z',
    };
    const sensorPayload: WorkplaceSuggestionSchema = {
      scope_date: '2026-11-07',
      workplace: 'home_office',
      rule_id: 'rule-home-wifi',
      confidence: 0.95,
      matched_signals: ['wifi_ssid=HomeFiber5G', 'display_count=2'],
      snapshot: {
        wifi_ssid: 'HomeFiber5G',
        display_count: 2,
        vpn_interfaces: ['utun4'],
      },
      clue_id: 'clue-sensor-1',
      context_fact_id: 'fact-workplace-today',
    };
    const proposalPayload: RetroSplitProposalSchema = {
      proposal_id: 'prop-401',
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
          notes: 'RetroSplit slice',
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
          notes: 'RetroSplit slice',
          tags: ['retrosplit'],
        },
      ],
      created_at: '2026-11-07T11:20:00Z',
    };
    const taxExportPayload: WorkplaceTaxExportResponseSchema = {
      format: 'markdown',
      rendered: '| Workplace | Days | Hours |\n| --- | --- | --- |\n| home_office | 1 | 2.25 |',
      report: {
        start_date: '2026-01-01',
        end_date: '2026-12-31',
        total_classified_days: 1,
        total_unclassified_days: 0,
        total_tracked_seconds: 8100,
        total_tracked_hours: 2.25,
        days_by_workplace: { home_office: 1 },
        workplace_days: { home_office: 1 },
        hours_by_workplace: { home_office: 2.25 },
        seconds_by_workplace: { home_office: 8100 },
        summary_rows: [
          {
            workplace: 'home_office',
            day_count: 1,
            tracked_seconds: 8100,
            tracked_hours: 2.25,
            sources: ['sensor'],
          },
        ],
        daily_entries: [
          {
            scope_date: '2026-11-07',
            workplace: 'home_office',
            source: 'sensor',
            tracked_seconds: 8100,
            tracked_hours: 2.25,
            interval_count: 2,
          },
        ],
      },
    };

    const fetchMock = vi.fn();
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ clues: [cluePayload] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ matched: true, suggestion: sensorPayload }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ proposal: proposalPayload }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(taxExportPayload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const client = new WorktrackTransportClient({
      baseUrl: 'http://127.0.0.1:8000',
      fetchImpl: fetchMock,
    });

    const clues = await client.fetchClues();
    expect(clues).toHaveLength(1);
    expect(useRepo(ClueModel).find('clue-301')?.inferred_activity_key).toBe('PROJ-142');

    const evalRes = await client.evaluateSensors({ scope_date: '2026-11-07', apply: true });
    expect(evalRes.matched).toBe(true);
    expect(useRepo(SensorSuggestionModel).find('2026-11-07')?.workplace).toBe('home_office');

    const propRes = await client.proposeRetroSplit({
      interval_id: 'int-chaotic-2',
      activity_ids: ['act-proj-142', 'act-standing-review'],
      strategy: 'proportional',
    });
    expect(propRes.proposal_id).toBe('prop-401');
    expect(useRepo(ReconcileProposalModel).find('prop-401')?.slices).toHaveLength(2);

    const taxRes = await client.fetchWorkplaceTaxExport(2026, 'markdown');
    expect(taxRes.format).toBe('markdown');
    expect(useRepo(ExportReportModel).find('workplace-tax:2026:markdown')?.rendered).toContain(
      'home_office',
    );

    const metrics = client.getTransportMetrics();
    expect(metrics.requestsTotal['/api/v1/clues/|2xx']).toBe(1);
    expect(metrics.requestsTotal['/api/v1/sensors/evaluate/|2xx']).toBe(1);
    expect(metrics.requestsTotal['/api/v1/reconciler/propose/|2xx']).toBe(1);
    expect(metrics.requestsTotal['/api/v1/exports/workplace-tax/|2xx']).toBe(1);
  });
});
