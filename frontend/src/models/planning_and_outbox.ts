/**
 * Normalized `pinia-orm` models for `AgendaItem`, `FocusBlock`, `OutboxItem`,
 * and `EgressReceipt` (`EgressRecord`).
 */
import { Model, type ModelFields } from 'pinia-orm';
import { ActivityModel } from './entities';

/**
 * Normalized `AgendaItem` model for prioritized *Today* / *This Week* queues.
 */
export class AgendaItemModel extends Model {
  static override entity = 'agenda_items';

  declare id: string;
  declare activity_id: string;
  declare horizon: 'today' | 'this_week' | 'backlog';
  declare priority: number;
  declare status: 'planned' | 'in_progress' | 'done' | 'deferred';
  declare budget_seconds: number | null;
  declare daily_budget_minutes: number | null;
  declare deadline: string | null;
  declare notes: string | null;
  declare created_at: string;
  declare updated_at: string;
  declare activity: ActivityModel | null;

  /**
   * Define normalized `pinia-orm` schema fields and relations for `AgendaItemModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      activity_id: this.string(''),
      horizon: this.string('today'),
      priority: this.number(3),
      status: this.string('planned'),
      budget_seconds: this.attr(null),
      daily_budget_minutes: this.attr(null),
      deadline: this.attr(null),
      notes: this.attr(null),
      created_at: this.string(''),
      updated_at: this.string(''),
      activity: this.belongsTo(ActivityModel, 'activity_id'),
    };
  }
}

/**
 * Normalized `FocusBlock` model representing a scheduled capacity block.
 */
export class FocusBlockModel extends Model {
  static override entity = 'focus_blocks';

  declare id: string;
  declare activity_id: string | null;
  declare label: string;
  declare started_at: string;
  declare ended_at: string;
  declare duration_seconds: number;
  declare expose_busy: boolean;
  declare notes: string | null;
  declare created_at: string;
  declare updated_at: string;
  declare activity: ActivityModel | null;

  /**
   * Define normalized `pinia-orm` schema fields and relations for `FocusBlockModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      activity_id: this.attr(null),
      label: this.string('Focus Block'),
      started_at: this.string(''),
      ended_at: this.string(''),
      duration_seconds: this.number(0),
      expose_busy: this.boolean(false),
      notes: this.attr(null),
      created_at: this.string(''),
      updated_at: this.string(''),
      activity: this.belongsTo(ActivityModel, 'activity_id'),
    };
  }
}

/**
 * Normalized `OutboxItem` model representing a previewable aggregate `Rollup` proposal.
 */
export class OutboxItemModel extends Model {
  static override entity = 'outbox_items';

  declare id: string;
  declare idempotency_key: string;
  declare sink: string;
  declare target_ref: string;
  declare window_start: string;
  declare window_end: string;
  declare raw_duration_seconds: number;
  declare rounded_duration_seconds: number;
  declare summary: string;
  declare payload_preview: Record<string, unknown>;
  declare payload_sha256: string;
  declare status: 'staged' | 'approved' | 'rejected' | 'flushed' | 'failed';
  declare cadence: 'on_clock_out' | 'eod' | 'eow' | 'eom' | 'manual';
  declare attempt_count: number;
  declare last_error: string | null;
  declare rejection_reason: string | null;
  declare source_interval_ids: string[];
  declare created_at: string;
  declare updated_at: string;

  /**
   * Define normalized `pinia-orm` schema fields for `OutboxItemModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      idempotency_key: this.string(''),
      sink: this.string('jira'),
      target_ref: this.string(''),
      window_start: this.string(''),
      window_end: this.string(''),
      raw_duration_seconds: this.number(0),
      rounded_duration_seconds: this.number(0),
      summary: this.string(''),
      payload_preview: this.attr({}),
      payload_sha256: this.string(''),
      status: this.string('staged'),
      cadence: this.string('eod'),
      attempt_count: this.number(0),
      last_error: this.attr(null),
      rejection_reason: this.attr(null),
      source_interval_ids: this.attr([]),
      created_at: this.string(''),
      updated_at: this.string(''),
    };
  }
}

/**
 * Normalized `EgressReceipt` (`EgressRecord`) model storing immutable SHA-256 audit receipts.
 */
export class EgressReceiptModel extends Model {
  static override entity = 'egress_receipts';

  declare id: string;
  declare outbox_item_id: string;
  declare idempotency_key: string;
  declare sink: string;
  declare destination_uri: string;
  declare payload_sha256: string;
  declare outcome: 'ok' | 'error' | 'skipped_duplicate';
  declare http_status: number | null;
  declare external_receipt_id: string | null;
  declare error_detail: string | null;
  declare trace_id: string;
  declare recorded_at: string;
  declare outbox_item: OutboxItemModel | null;

  /**
   * Define normalized `pinia-orm` schema fields and relations for `EgressReceiptModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      outbox_item_id: this.string(''),
      idempotency_key: this.string(''),
      sink: this.string('jira'),
      destination_uri: this.string(''),
      payload_sha256: this.string(''),
      outcome: this.string('ok'),
      http_status: this.attr(null),
      external_receipt_id: this.attr(null),
      error_detail: this.attr(null),
      trace_id: this.string(''),
      recorded_at: this.string(''),
      outbox_item: this.belongsTo(OutboxItemModel, 'outbox_item_id'),
    };
  }
}
