/**
 * Normalized `pinia-orm` models for core Ledger entities (`Activity`, `Interval`,
 * `Entity`, `Attribution`, and `ContextFact`).
 */
import { Model, type ModelFields } from 'pinia-orm';

/**
 * Normalized `Activity` model (*Targeted* deliverable work vs. *Standing* recurring stream).
 */
export class ActivityModel extends Model {
  static override entity = 'activities';

  declare id: string;
  declare name: string;
  declare kind: 'targeted' | 'standing';
  declare source: string;
  declare external_ref: string | null;
  declare rollup_target_ref: string | null;
  declare tags: string[];
  declare archived: boolean;
  declare created_at: string;
  declare updated_at: string;

  /**
   * Define normalized `pinia-orm` schema fields for `ActivityModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      name: this.string(''),
      kind: this.string('targeted'),
      source: this.string('local'),
      external_ref: this.attr(null),
      rollup_target_ref: this.attr(null),
      tags: this.attr([]),
      archived: this.boolean(false),
      created_at: this.string(''),
      updated_at: this.string(''),
    };
  }
}

/**
 * Normalized `Interval` model representing structured or chaotic time capture.
 */
export class IntervalModel extends Model {
  static override entity = 'intervals';

  declare id: string;
  declare activity_id: string | null;
  declare started_at: string;
  declare ended_at: string | null;
  declare duration_seconds: number | null;
  declare capture_mode: 'structured' | 'chaotic';
  declare notes: string | null;
  declare tags: string[];
  declare is_open: boolean;
  declare is_allocated: boolean;
  declare created_at: string;
  declare updated_at: string;
  declare activity: ActivityModel | null;

  /**
   * Define normalized `pinia-orm` schema fields and relations for `IntervalModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      activity_id: this.attr(null),
      started_at: this.string(''),
      ended_at: this.attr(null),
      duration_seconds: this.attr(0),
      capture_mode: this.string('structured'),
      notes: this.attr(null),
      tags: this.attr([]),
      is_open: this.boolean(false),
      is_allocated: this.boolean(false),
      created_at: this.string(''),
      updated_at: this.string(''),
      activity: this.belongsTo(ActivityModel, 'activity_id'),
    };
  }
}

/**
 * Normalized `Entity` node (`project`, `component`, `customer`, `epic`, `cost_center`).
 */
export class EntityModel extends Model {
  static override entity = 'entities';

  declare id: string;
  declare kind: string;
  declare key: string;
  declare name: string;
  declare slug: string;
  declare metadata: Record<string, unknown>;
  declare created_at: string;
  declare updated_at: string;

  /**
   * Define normalized `pinia-orm` schema fields for `EntityModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      kind: this.string('project'),
      key: this.string(''),
      name: this.string(''),
      slug: this.string(''),
      metadata: this.attr({}),
      created_at: this.string(''),
      updated_at: this.string(''),
    };
  }
}

/**
 * Normalized weighted `Attribution` edge linking an `Activity` or `Entity` to a target `Entity`.
 */
export class AttributionModel extends Model {
  static override entity = 'attributions';

  declare id: string;
  declare source_activity_id: string | null;
  declare source_entity_id: string | null;
  declare target_entity_id: string;
  declare weight: number;
  declare created_at: string;
  declare updated_at: string;
  declare target_entity: EntityModel | null;

  /**
   * Define normalized `pinia-orm` schema fields and relations for `AttributionModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      source_activity_id: this.attr(null),
      source_entity_id: this.attr(null),
      target_entity_id: this.string(''),
      weight: this.number(1.0),
      created_at: this.string(''),
      updated_at: this.string(''),
      target_entity: this.belongsTo(EntityModel, 'target_entity_id'),
    };
  }
}

/**
 * Normalized `ContextFact` assertion (such as `workplace=home_office`) kept strictly local.
 */
export class ContextFactModel extends Model {
  static override entity = 'context_facts';

  declare id: string;
  declare facet_key: string;
  declare value: string | number | boolean;
  declare scope_date: string | null;
  declare interval_id: string | null;
  declare source: string;
  declare updated_at: string;

  /**
   * Define normalized `pinia-orm` schema fields for `ContextFactModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      facet_key: this.string('workplace'),
      value: this.attr('home_office'),
      scope_date: this.attr(null),
      interval_id: this.attr(null),
      source: this.string('manual'),
      updated_at: this.string(''),
    };
  }
}
