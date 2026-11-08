/**
 * Typed OpenAPI transport client synchronizing `/api/v1/...` payloads into normalized
 * `pinia-orm` repositories while tracking bounded frontend API transport error counters.
 */
import { useRepo } from 'pinia-orm';
import type {
  ActivitySchema, AgendaItemSchema, AttributionSchema, ClueListSchema,
  ClueSchema, ClueSyncResultSchema, ContextFactSchema, EgressRecordSchema,
  EntityAttributionExportResponseSchema, EntitySchema, FocusBlockSchema, IntervalSchema,
  OutboxItemSchema, ReconcilerProposeRequestSchema, ReconcilerProposeResponseSchema,
  ReportingPackListResponseSchema, ReportingPackSchema, RetroSplitApplyResultSchema,
  RetroSplitProposalSchema, SensorEvaluateRequestSchema, SensorEvaluationResultSchema,
  TravelTripListResponseSchema, VacationSummarySchema, WorkplaceTaxExportResponseSchema,
} from '../api/generated';
import {
  API_ROUTES,
  DEFAULT_API_BASE_URL,
  HEADER_FRITZ_TRACE_ID,
  HTTP_STATUS_BOUNDS,
} from '../config/constants';
import {
  ActivityModel, AgendaItemModel, AttributionModel, ClueModel,
  ContextFactModel, EgressReceiptModel, EntityModel, ExportReportModel,
  FocusBlockModel, IntervalModel, OutboxItemModel, ReconcileProposalModel,
  ReportingPackModel, SensorSuggestionModel, TravelTripModel, VacationSummaryModel,
} from '../models';

/** Bounded frontend API transport telemetry snapshot. */
export interface TransportMetricsSnapshot {
  requestsTotal: Record<string, number>;
  errorsTotal: Record<string, number>;
  totalRequestCount: number;
  totalErrorCount: number;
  lastTraceId: string | null;
}

/** Configuration options for `WorktrackTransportClient`. */
export interface TransportClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

/**
 * Classify an HTTP status code into a bounded Prometheus-style status class (`2xx`, `4xx`, `5xx`).
 * @param status Numeric HTTP status code.
 * @returns Bounded status class label.
 */
export function classifyHttpStatus(status: number): string {
  if (status >= HTTP_STATUS_BOUNDS.serverErrorMin) {
    return '5xx';
  }
  if (status >= HTTP_STATUS_BOUNDS.clientErrorMin) {
    return '4xx';
  }
  if (status >= HTTP_STATUS_BOUNDS.redirectMin) {
    return '3xx';
  }
  if (status >= HTTP_STATUS_BOUNDS.okMin) {
    return '2xx';
  }
  return 'network_error';
}

/**
 * Typed transport client connecting `@hey-api/openapi-ts` schemas to `pinia-orm` repositories.
 */
export class WorktrackTransportClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly requestsTotal: Record<string, number> = {};
  private readonly errorsTotal: Record<string, number> = {};
  private totalRequestCount = 0;
  private totalErrorCount = 0;
  private lastTraceId: string | null = null;

  /**
   * Initialize the transport client with configurable base URL and `fetch` implementation.
   * @param options Optional client overrides.
   */
  constructor(options: TransportClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_API_BASE_URL).replace(/\/+$/, '');
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  /**
   * Return a snapshot of bounded frontend API transport request and error counters.
   * @returns Immutable transport telemetry snapshot.
   */
  getTransportMetrics(): TransportMetricsSnapshot {
    return {
      requestsTotal: { ...this.requestsTotal },
      errorsTotal: { ...this.errorsTotal },
      totalRequestCount: this.totalRequestCount,
      totalErrorCount: this.totalErrorCount,
      lastTraceId: this.lastTraceId,
    };
  }

  /**
   * Perform a JSON HTTP request against a canonical route and record bounded telemetry.
   * @param route Canonical route path (used as bounded metric key).
   * @param init Optional `RequestInit` overrides.
   * @param queryString Optional query string appended to `route` without expanding metric keys.
   * @returns Parsed JSON response payload.
   */
  async requestJson<T>(route: string, init: RequestInit = {}, queryString = ''): Promise<T> {
    const url = `${this.baseUrl}${route}${queryString}`;
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        ...init,
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          ...(init.headers ?? {}),
        },
      });
    } catch (error) {
      this.recordOutcome(route, 'network_error', true);
      throw error;
    }

    const traceHeader = response.headers.get(HEADER_FRITZ_TRACE_ID);
    if (traceHeader) {
      this.lastTraceId = traceHeader;
    }
    const statusClass = classifyHttpStatus(response.status);
    const isError = !response.ok;
    this.recordOutcome(route, statusClass, isError);
    if (isError) {
      throw new Error(`HTTP ${response.status} on ${route}`);
    }
    return (await response.json()) as T;
  }

  /**
   * Record bounded request and error counters by `(route, statusClass)`.
   * @param route Canonical route template.
   * @param statusClass Bounded HTTP status class.
   * @param isError True when the request failed.
   */
  private recordOutcome(route: string, statusClass: string, isError: boolean): void {
    const key = `${route}|${statusClass}`;
    this.requestsTotal[key] = (this.requestsTotal[key] ?? 0) + 1;
    this.totalRequestCount += 1;
    if (isError) {
      this.errorsTotal[key] = (this.errorsTotal[key] ?? 0) + 1;
      this.totalErrorCount += 1;
    }
  }

  /**
   * Fetch `/api/v1/activities/` and normalize into `ActivityModel`.
   */
  async fetchActivities(): Promise<ActivitySchema[]> {
    const items = await this.requestJson<ActivitySchema[]>(API_ROUTES.activities);
    useRepo(ActivityModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/intervals/` and normalize into `IntervalModel`.
   */
  async fetchIntervals(): Promise<IntervalSchema[]> {
    const items = await this.requestJson<IntervalSchema[]>(API_ROUTES.intervals);
    useRepo(IntervalModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/entities/` and normalize into `EntityModel`.
   */
  async fetchEntities(): Promise<EntitySchema[]> {
    const items = await this.requestJson<EntitySchema[]>(API_ROUTES.entities);
    useRepo(EntityModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/attributions/` and normalize into `AttributionModel`.
   */
  async fetchAttributions(): Promise<AttributionSchema[]> {
    const items = await this.requestJson<AttributionSchema[]>(API_ROUTES.attributions);
    useRepo(AttributionModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/context-facts/` and normalize into `ContextFactModel`.
   */
  async fetchContextFacts(): Promise<ContextFactSchema[]> {
    const items = await this.requestJson<ContextFactSchema[]>(API_ROUTES.contextFacts);
    useRepo(ContextFactModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/agenda/items/` and normalize into `AgendaItemModel`.
   */
  async fetchAgendaItems(): Promise<AgendaItemSchema[]> {
    const items = await this.requestJson<AgendaItemSchema[]>(API_ROUTES.agendaItems);
    useRepo(AgendaItemModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/agenda/focus-blocks/` and normalize into `FocusBlockModel`.
   */
  async fetchFocusBlocks(): Promise<FocusBlockSchema[]> {
    const items = await this.requestJson<FocusBlockSchema[]>(API_ROUTES.focusBlocks);
    useRepo(FocusBlockModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/outbox/items/` and normalize into `OutboxItemModel`.
   */
  async fetchOutboxItems(): Promise<OutboxItemSchema[]> {
    const items = await this.requestJson<OutboxItemSchema[]>(API_ROUTES.outboxItems);
    useRepo(OutboxItemModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/egress-ledger/` and normalize into `EgressReceiptModel`.
   */
  async fetchEgressLedger(): Promise<EgressRecordSchema[]> {
    const items = await this.requestJson<EgressRecordSchema[]>(API_ROUTES.egressLedger);
    useRepo(EgressReceiptModel).save(items);
    return items;
  }

  /**
   * Fetch `/api/v1/clues/` and normalize into `ClueModel`.
   */
  async fetchClues(): Promise<ClueSchema[]> {
    const envelope = await this.requestJson<ClueListSchema | ClueSchema[]>(API_ROUTES.clues);
    const clues = Array.isArray(envelope) ? envelope : envelope.clues;
    useRepo(ClueModel).save(clues);
    return clues;
  }

  /**
   * Synchronize Git clues via `POST /api/v1/clues/git-sync/` and normalize into `ClueModel`.
   * @param payload Optional repository path, time window, or direct clue items.
   */
  async syncGitClues(payload: Record<string, unknown> = {}): Promise<ClueSyncResultSchema> {
    const result = await this.requestJson<ClueSyncResultSchema>(API_ROUTES.cluesGitSync, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    useRepo(ClueModel).save(result.clues);
    return result;
  }

  /**
   * Evaluate local `workplace` sensor rules via `POST /api/v1/sensors/evaluate/`.
   * @param payload Optional scope date, apply flag, or inline rules/snapshot.
   */
  async evaluateSensors(
    payload: SensorEvaluateRequestSchema = {},
  ): Promise<SensorEvaluationResultSchema> {
    const result = await this.requestJson<SensorEvaluationResultSchema>(
      API_ROUTES.sensorsEvaluate,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    );
    if (result.suggestion) {
      useRepo(SensorSuggestionModel).save(result.suggestion);
    }
    return result;
  }

  /**
   * Propose a duration-conserving `RetroSplit` via `POST /api/v1/reconciler/propose/`.
   * @param payload Target interval, activity list, and split strategy.
   */
  async proposeRetroSplit(
    payload: ReconcilerProposeRequestSchema,
  ): Promise<RetroSplitProposalSchema> {
    const response = await this.requestJson<ReconcilerProposeResponseSchema>(
      API_ROUTES.reconcilerPropose,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    );
    useRepo(ReconcileProposalModel).save(response.proposal);
    return response.proposal;
  }

  /**
   * Apply a `RetroSplit` via `POST /api/v1/reconciler/apply/` and normalize split `Intervals`.
   * @param payload Target interval, activity list, and split strategy.
   */
  async applyRetroSplit(
    payload: ReconcilerProposeRequestSchema,
  ): Promise<RetroSplitApplyResultSchema> {
    const result = await this.requestJson<RetroSplitApplyResultSchema>(API_ROUTES.reconcilerApply, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    useRepo(IntervalModel).save(result.intervals);
    return result;
  }

  /**
   * Fetch `/api/v1/exports/workplace-tax/` and normalize into `ExportReportModel`.
   * @param year Four-digit calendar year.
   * @param format Target export format (`csv`, `json`, or `markdown`).
   */
  async fetchWorkplaceTaxExport(
    year: number,
    format: 'csv' | 'json' | 'markdown' = 'markdown',
  ): Promise<WorkplaceTaxExportResponseSchema> {
    const query = `?year=${encodeURIComponent(String(year))}&format=${encodeURIComponent(format)}`;
    const result = await this.requestJson<WorkplaceTaxExportResponseSchema>(
      API_ROUTES.exportsWorkplaceTax,
      {},
      query,
    );
    useRepo(ExportReportModel).save({
      id: `workplace-tax:${year}:${format}`,
      kind: 'workplace-tax',
      format,
      rendered: result.rendered,
      report: result.report as unknown as Record<string, unknown>,
      updated_at: new Date().toISOString(),
    });
    return result;
  }

  /**
   * Fetch `/api/v1/exports/entity-attribution/` and normalize into `ExportReportModel`.
   * @param start Window start ISO-8601 timestamp.
   * @param end Window end ISO-8601 timestamp.
   * @param format Target export format (`csv`, `json`, or `markdown`).
   */
  async fetchEntityAttributionExport(
    start: string,
    end: string,
    format: 'csv' | 'json' | 'markdown' = 'markdown',
  ): Promise<EntityAttributionExportResponseSchema> {
    const query = `?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&format=${encodeURIComponent(format)}`;
    const result = await this.requestJson<EntityAttributionExportResponseSchema>(
      API_ROUTES.exportsEntityAttribution,
      {},
      query,
    );
    useRepo(ExportReportModel).save({
      id: `entity-attribution:${start}:${end}:${format}`,
      kind: 'entity-attribution',
      format,
      rendered: result.rendered,
      report: result.report as unknown as Record<string, unknown>,
      updated_at: new Date().toISOString(),
    });
    return result;
  }

  /**
   * Fetch available reporting packs via `GET /api/v1/reporting/packs/` and normalize into `ReportingPackModel`.
   */
  async fetchReportingPacks(): Promise<ReportingPackSchema[]> {
    const response = await this.requestJson<ReportingPackListResponseSchema>(
      API_ROUTES.reportingPacks,
    );
    useRepo(ReportingPackModel).save(response.packs);
    return response.packs;
  }

  /**
   * Fetch consolidated vacation summary via `GET /api/v1/reporting/absences/summary/`.
   * @param year Four-digit calendar year.
   * @param subdivision Jurisdiction subdivision code (default `DE-BY`).
   */
  async fetchVacationSummary(
    year: number,
    subdivision = 'DE-BY',
  ): Promise<VacationSummarySchema> {
    const query = `?year=${encodeURIComponent(String(year))}&subdivision=${encodeURIComponent(subdivision)}`;
    const result = await this.requestJson<VacationSummarySchema>(
      API_ROUTES.reportingAbsenceSummary,
      {},
      query,
    );
    useRepo(VacationSummaryModel).save(result);
    return result;
  }

  /**
   * Fetch business travel trips and statutory allowances via `GET /api/v1/reporting/travel-trips/`.
   * @param pack Reporting pack identifier (default `de`).
   */
  async fetchTravelTrips(pack = 'de'): Promise<TravelTripListResponseSchema> {
    const query = `?pack=${encodeURIComponent(pack)}`;
    const result = await this.requestJson<TravelTripListResponseSchema>(
      API_ROUTES.reportingTravelTrips,
      {},
      query,
    );
    const trips = result.trips.map((calc) => ({
      id: calc.trip.id,
      title: calc.trip.title,
      destination: calc.trip.destination,
      country_code: calc.trip.country_code,
      started_at: calc.trip.started_at,
      ended_at: calc.trip.ended_at,
      transport_mode: calc.trip.transport_mode,
      distance_km: calc.trip.distance_km,
      net_per_diem_total: calc.net_per_diem_total,
      mileage_allowance: calc.mileage_allowance,
      total_reimbursement: calc.total_reimbursement,
      currency: calc.currency,
      notes: calc.trip.notes,
    }));
    useRepo(TravelTripModel).save(trips);
    return result;
  }
}
