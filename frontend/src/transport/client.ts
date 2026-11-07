/**
 * Typed OpenAPI transport client synchronizing `/api/v1/...` payloads into normalized
 * `pinia-orm` repositories while tracking bounded frontend API transport error counters.
 */
import { useRepo } from 'pinia-orm';
import type {
  ActivitySchema,
  AgendaItemSchema,
  AttributionSchema,
  ContextFactSchema,
  EgressRecordSchema,
  EntitySchema,
  FocusBlockSchema,
  IntervalSchema,
  OutboxItemSchema,
} from '../api/generated';
import {
  API_ROUTES,
  DEFAULT_API_BASE_URL,
  HEADER_FRITZ_TRACE_ID,
  HTTP_STATUS_BOUNDS,
} from '../config/constants';
import {
  ActivityModel,
  AgendaItemModel,
  AttributionModel,
  ContextFactModel,
  EgressReceiptModel,
  EntityModel,
  FocusBlockModel,
  IntervalModel,
  OutboxItemModel,
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
   * @returns Parsed JSON response payload.
   */
  async requestJson<T>(route: string, init: RequestInit = {}): Promise<T> {
    const url = `${this.baseUrl}${route}`;
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
}
