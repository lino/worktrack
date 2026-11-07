/**
 * Normalized `pinia-orm` models for Wave 2–4 ambient `Clue` collectors,
 * `WorkplaceSuggestion` sensors, `RetroSplitProposal` reconciler runs, and
 * `ExportReport` tax/attribution statements.
 */
import { Model, type ModelFields } from 'pinia-orm';
import type { RetroSplitSliceProposalSchema } from '../api/generated';
import { ActivityModel } from './entities';

/**
 * Normalized `Clue` model storing local-only Git, Forge, or Calendar ambient hints.
 */
export class ClueModel extends Model {
  static override entity = 'clues';

  declare id: string;
  declare source: string;
  declare external_key: string;
  declare external_id: string;
  declare occurred_at: string;
  declare summary: string;
  declare inferred_activity_key: string | null;
  declare activity_id: string | null;
  declare confidence: number;
  declare metadata: Record<string, unknown>;
  declare created_at: string;
  declare activity: ActivityModel | null;

  /**
   * Define normalized `pinia-orm` schema fields and relations for `ClueModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      source: this.string('git'),
      external_key: this.string(''),
      external_id: this.string(''),
      occurred_at: this.string(''),
      summary: this.string(''),
      inferred_activity_key: this.attr(null),
      activity_id: this.attr(null),
      confidence: this.number(0.85),
      metadata: this.attr({}),
      created_at: this.string(''),
      activity: this.belongsTo(ActivityModel, 'activity_id'),
    };
  }
}

/**
 * Normalized `SensorSuggestion` (`WorkplaceSuggestion`) model for local `workplace` inference.
 */
export class SensorSuggestionModel extends Model {
  static override entity = 'sensor_suggestions';
  static override primaryKey = 'scope_date';

  declare scope_date: string;
  declare workplace: string;
  declare rule_id: string;
  declare confidence: number;
  declare matched_signals: string[];
  declare snapshot: Record<string, unknown>;
  declare clue_id: string | null;
  declare context_fact_id: string | null;

  /**
   * Define normalized `pinia-orm` schema fields for `SensorSuggestionModel`.
   */
  static override fields(): ModelFields {
    return {
      scope_date: this.string(''),
      workplace: this.string('home_office'),
      rule_id: this.string(''),
      confidence: this.number(0.9),
      matched_signals: this.attr([]),
      snapshot: this.attr({}),
      clue_id: this.attr(null),
      context_fact_id: this.attr(null),
    };
  }
}

/**
 * Normalized `ReconcileProposal` (`RetroSplitProposal`) model for duration-conserving splits.
 */
export class ReconcileProposalModel extends Model {
  static override entity = 'reconcile_proposals';
  static override primaryKey = 'proposal_id';

  declare proposal_id: string;
  declare strategy: 'equal' | 'proportional' | 'weights';
  declare interval_ids: string[];
  declare total_duration_seconds: number;
  declare allocated_duration_seconds: number;
  declare is_conserved: boolean;
  declare normalized_weights: Record<string, number>;
  declare slices: RetroSplitSliceProposalSchema[];
  declare created_at: string;

  /**
   * Define normalized `pinia-orm` schema fields for `ReconcileProposalModel`.
   */
  static override fields(): ModelFields {
    return {
      proposal_id: this.string(''),
      strategy: this.string('proportional'),
      interval_ids: this.attr([]),
      total_duration_seconds: this.number(0),
      allocated_duration_seconds: this.number(0),
      is_conserved: this.boolean(true),
      normalized_weights: this.attr({}),
      slices: this.attr([]),
      created_at: this.string(''),
    };
  }
}

/**
 * Normalized `ExportReport` model caching `workplace` tax and `Entity` attribution exports.
 */
export class ExportReportModel extends Model {
  static override entity = 'export_reports';

  declare id: string;
  declare kind: 'workplace-tax' | 'entity-attribution';
  declare format: 'csv' | 'json' | 'markdown';
  declare rendered: string;
  declare report: Record<string, unknown>;
  declare updated_at: string;

  /**
   * Define normalized `pinia-orm` schema fields for `ExportReportModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      kind: this.string('workplace-tax'),
      format: this.string('markdown'),
      rendered: this.string(''),
      report: this.attr({}),
      updated_at: this.string(''),
    };
  }
}
