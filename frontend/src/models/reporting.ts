/**
 * Normalized `pinia-orm` models for statutory and country-neutral reporting packs,
 * leave/absence entries, consolidated vacation summaries, and business travel trips.
 */
import { Model, type ModelFields, useRepo } from 'pinia-orm';

/**
 * Normalized `ReportingPack` model storing country-specific tax and travel allowance parameters.
 */
export class ReportingPackModel extends Model {
  static override entity = 'reporting_packs';
  static override primaryKey = 'slug';

  declare slug: string;
  declare title: string;
  declare currency: string;
  declare country_code: string;
  declare tax_rules_enabled: boolean;
  declare home_office_daily_rate: number;
  declare home_office_max_days: number;
  declare commute_first_tier_km: number;
  declare commute_first_tier_rate: number;
  declare commute_second_tier_rate: number;
  declare per_diem_partial_day_rate: number;
  declare per_diem_full_day_rate: number;
  declare car_mileage_rate_per_km: number;

  /**
   * Define normalized `pinia-orm` schema fields for `ReportingPackModel`.
   */
  static override fields(): ModelFields {
    return {
      slug: this.string(''),
      title: this.string(''),
      currency: this.string('EUR'),
      country_code: this.string(''),
      tax_rules_enabled: this.boolean(false),
      home_office_daily_rate: this.number(0),
      home_office_max_days: this.number(0),
      commute_first_tier_km: this.number(0),
      commute_first_tier_rate: this.number(0),
      commute_second_tier_rate: this.number(0),
      per_diem_partial_day_rate: this.number(0),
      per_diem_full_day_rate: this.number(0),
      car_mileage_rate_per_km: this.number(0),
    };
  }
}

/**
 * Normalized `AbsenceEntry` model tracking vacation, sick leave, and compensatory off time.
 */
export class AbsenceEntryModel extends Model {
  static override entity = 'absence_entries';
  static override primaryKey = 'id';

  declare id: string;
  declare start_date: string;
  declare end_date: string;
  declare kind: string;
  declare day_fraction: number;
  declare effective_days: number;
  declare notes: string;
  declare created_at: string;

  /**
   * Define normalized `pinia-orm` schema fields for `AbsenceEntryModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      start_date: this.string(''),
      end_date: this.string(''),
      kind: this.string('vacation'),
      day_fraction: this.number(1.0),
      effective_days: this.number(1.0),
      notes: this.string(''),
      created_at: this.string(''),
    };
  }
}

/**
 * Normalized `VacationSummary` model storing annual entitlement, taken, planned, and remaining leave.
 */
export class VacationSummaryModel extends Model {
  static override entity = 'vacation_summaries';
  static override primaryKey = 'year';

  declare year: number;
  declare subdivision: string;
  declare entitlement_days: number;
  declare carryover_days: number;
  declare total_quota_days: number;
  declare taken_vacation_days: number;
  declare planned_vacation_days: number;
  declare remaining_vacation_days: number;
  declare sick_leave_days: number;
  declare compensatory_off_days: number;
  declare public_holiday_count: number;
  declare weekday_public_holiday_count: number;

  /**
   * Define normalized `pinia-orm` schema fields for `VacationSummaryModel`.
   */
  static override fields(): ModelFields {
    return {
      year: this.number(2026),
      subdivision: this.string('DE-BY'),
      entitlement_days: this.number(0),
      carryover_days: this.number(0),
      total_quota_days: this.number(0),
      taken_vacation_days: this.number(0),
      planned_vacation_days: this.number(0),
      remaining_vacation_days: this.number(0),
      sick_leave_days: this.number(0),
      compensatory_off_days: this.number(0),
      public_holiday_count: this.number(0),
      weekday_public_holiday_count: this.number(0),
    };
  }
}

/**
 * Normalized `TravelTrip` model storing business trip itinerary and statutory reimbursements.
 */
export class TravelTripModel extends Model {
  static override entity = 'travel_trips';
  static override primaryKey = 'id';

  declare id: string;
  declare title: string;
  declare destination: string;
  declare country_code: string;
  declare started_at: string;
  declare ended_at: string;
  declare transport_mode: string;
  declare distance_km: number;
  declare net_per_diem_total: number;
  declare mileage_allowance: number;
  declare total_reimbursement: number;
  declare currency: string;
  declare notes: string;

  /**
   * Define normalized `pinia-orm` schema fields for `TravelTripModel`.
   */
  static override fields(): ModelFields {
    return {
      id: this.string(''),
      title: this.string(''),
      destination: this.string(''),
      country_code: this.string('DE'),
      started_at: this.string(''),
      ended_at: this.string(''),
      transport_mode: this.string('train'),
      distance_km: this.number(0),
      net_per_diem_total: this.number(0),
      mileage_allowance: this.number(0),
      total_reimbursement: this.number(0),
      currency: this.string('EUR'),
      notes: this.string(''),
    };
  }
}

/**
 * Seed initial jurisdiction reporting packs into `pinia-orm`.
 */
function seedReportingPacks(): void {
  useRepo(ReportingPackModel).save([
    {
      slug: 'generic',
      title: 'Generic Standard Pack',
      currency: 'EUR',
      country_code: 'XX',
      tax_rules_enabled: false,
      home_office_daily_rate: 0.0,
      home_office_max_days: 0,
      commute_first_tier_km: 0.0,
      commute_first_tier_rate: 0.0,
      commute_second_tier_rate: 0.0,
      per_diem_partial_day_rate: 0.0,
      per_diem_full_day_rate: 0.0,
      car_mileage_rate_per_km: 0.0,
    },
    {
      slug: 'de',
      title: 'Germany (EStG / Reisekostenrecht)',
      currency: 'EUR',
      country_code: 'DE',
      tax_rules_enabled: true,
      home_office_daily_rate: 6.0,
      home_office_max_days: 210,
      commute_first_tier_km: 20.0,
      commute_first_tier_rate: 0.3,
      commute_second_tier_rate: 0.38,
      per_diem_partial_day_rate: 14.0,
      per_diem_full_day_rate: 28.0,
      car_mileage_rate_per_km: 0.3,
    },
  ]);
}

/**
 * Seed initial vacation quota balance and demo absence entry into `pinia-orm`.
 */
function seedVacationAndAbsences(): void {
  useRepo(VacationSummaryModel).save({
    year: 2026,
    subdivision: 'DE-BY',
    entitlement_days: 30,
    carryover_days: 2,
    total_quota_days: 32,
    taken_vacation_days: 10,
    planned_vacation_days: 5,
    remaining_vacation_days: 17,
    sick_leave_days: 2,
    compensatory_off_days: 1,
    public_holiday_count: 13,
    weekday_public_holiday_count: 10,
  });

  useRepo(AbsenceEntryModel).save([
    {
      id: 'abs-demo-1',
      start_date: '2026-08-03',
      end_date: '2026-08-14',
      kind: 'vacation',
      day_fraction: 1.0,
      effective_days: 10.0,
      notes: 'Summer Urlaub (Bavaria)',
      created_at: '2026-08-01T08:00:00Z',
    },
  ]);
}

/**
 * Seed initial demo business travel trip into `pinia-orm`.
 */
function seedTravelTrips(): void {
  useRepo(TravelTripModel).save([
    {
      id: 'trip-demo-1',
      title: 'Architecture Summit Berlin',
      destination: 'Berlin',
      country_code: 'DE',
      started_at: '2026-10-12T08:00:00Z',
      ended_at: '2026-10-14T19:00:00Z',
      transport_mode: 'train',
      distance_km: 0,
      net_per_diem_total: 50.4,
      mileage_allowance: 0,
      total_reimbursement: 50.4,
      currency: 'EUR',
      notes: 'Hotel breakfast provided',
    },
  ]);
}

/**
 * Seed initial reporting packs, vacation summary, demo absences, and travel trips into `pinia-orm`.
 */
export function seedReportingAndAbsences(): void {
  seedReportingPacks();
  seedVacationAndAbsences();
  seedTravelTrips();
}
