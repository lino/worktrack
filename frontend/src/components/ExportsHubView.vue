<script setup lang="ts">
/**
 * Tax, Reporting Packs, Travel & Multi-Entity Attribution Exports Cockpit View.
 * Renders annual `workplace` tax-return tallies, statutory reporting pack selection,
 * vacation/absence balance, business travel expense calculations, and multi-entity statements.
 */
import { useRepo } from 'pinia-orm';
import { computed, ref } from 'vue';
import { formatHumanDuration, formatIsoDuration } from '../composables/useDurationFormat';
import {
  AttributionModel,
  ContextFactModel,
  ExportReportModel,
  IntervalModel,
  ReportingPackModel,
  TravelTripModel,
  VacationSummaryModel,
} from '../models';

const selectedFormat = ref<'markdown' | 'csv' | 'json'>('markdown');
const selectedPack = ref<'de' | 'generic'>('de');

const activePack = computed(() =>
  useRepo(ReportingPackModel).find(selectedPack.value),
);

const vacationSummary = computed(() =>
  useRepo(VacationSummaryModel).where('year', 2026).first(),
);

const travelTrips = computed(() =>
  useRepo(TravelTripModel).all(),
);

const workplaceFacts = computed(() =>
  useRepo(ContextFactModel)
    .where('facet_key', 'workplace')
    .get(),
);
const attributions = computed(() =>
  useRepo(AttributionModel).with('target_entity').get(),
);
const totalIntervalSeconds = computed(() =>
  useRepo(IntervalModel)
    .all()
    .reduce((acc, item) => acc + (item.duration_seconds ?? 0), 0),
);
const activePreviewText = computed(() => {
  const reportId = `workplace-tax:2026:${selectedFormat.value}`;
  const stored = useRepo(ExportReportModel).find(reportId);
  return stored?.rendered ?? '';
});
</script>

<template>
  <section aria-labelledby="exports-hub-heading" class="cockpit-panel">
    <h2 id="exports-hub-heading">Tax &amp; Multi-Entity Attribution Exports</h2>

    <div class="cockpit-grid">
      <article class="cockpit-card" aria-labelledby="reporting-pack-title">
        <h3 id="reporting-pack-title">Jurisdiction Reporting Pack</h3>
        <form class="cockpit-form" @submit.prevent>
          <fieldset>
            <legend>Select Jurisdiction Reporting Pack</legend>
            <div class="radio-group">
              <label for="reporting-pack-de">
                <input
                  id="reporting-pack-de"
                  v-model="selectedPack"
                  type="radio"
                  name="reporting_pack"
                  value="de"
                />
                Germany (EStG / Reisekostenrecht)
              </label>
              <label for="reporting-pack-generic">
                <input
                  id="reporting-pack-generic"
                  v-model="selectedPack"
                  type="radio"
                  name="reporting_pack"
                  value="generic"
                />
                Generic Standard (Country-Neutral)
              </label>
            </div>
          </fieldset>
        </form>
        <div v-if="activePack" class="pack-status-banner">
          <p v-if="activePack.tax_rules_enabled">
            Statutory allowances active:
            <strong>{{ activePack.home_office_daily_rate.toFixed(2) }} {{ activePack.currency }}/day</strong>
            Homeoffice-Pauschale (max {{ activePack.home_office_max_days }} days),
            <strong>{{ activePack.commute_first_tier_rate.toFixed(2) }}/{{ activePack.commute_second_tier_rate.toFixed(2) }} {{ activePack.currency }}/km</strong>
            Entfernungspauschale.
          </p>
          <p v-else>
            Country-neutral generic pack active: statutory tax deduction rules and per-diem calculation rates are disabled.
          </p>
        </div>
      </article>

      <article class="cockpit-card" aria-labelledby="vacation-summary-title">
        <h3 id="vacation-summary-title">Vacation (Urlaub) &amp; Public Holidays (Feiertage) Summary</h3>
        <div v-if="vacationSummary" class="vacation-quota-metrics">
          <dl class="quota-list">
            <div>
              <dt>Entitlement Days:</dt>
              <dd>{{ vacationSummary.entitlement_days }}</dd>
            </div>
            <div>
              <dt>Carryover Days:</dt>
              <dd>{{ vacationSummary.carryover_days }}</dd>
            </div>
            <div>
              <dt>Taken Vacation Days:</dt>
              <dd>{{ vacationSummary.taken_vacation_days }}</dd>
            </div>
            <div>
              <dt>Planned Vacation Days:</dt>
              <dd>{{ vacationSummary.planned_vacation_days }}</dd>
            </div>
            <div>
              <dt>Remaining Vacation Days (Resturlaub):</dt>
              <dd><strong>{{ vacationSummary.remaining_vacation_days }}</strong></dd>
            </div>
            <div>
              <dt>Weekday Public Holidays ({{ vacationSummary.subdivision }}):</dt>
              <dd>{{ vacationSummary.weekday_public_holiday_count }}</dd>
            </div>
          </dl>
        </div>

        <div
          tabindex="0"
          role="region"
          aria-labelledby="travel-table-caption"
          class="table-wrapper"
        >
          <table class="responsive-data-table">
            <caption id="travel-table-caption">
              Business Travel (Reisekosten &amp; Verpflegungsmehraufwand)
            </caption>
            <thead>
              <tr>
                <th scope="col">Trip Title</th>
                <th scope="col">Destination</th>
                <th scope="col">Dates</th>
                <th scope="col">Net Per-Diem</th>
                <th scope="col">Mileage</th>
                <th scope="col">Total Reimbursement</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="trip in travelTrips" :key="trip.id">
                <th scope="row">{{ trip.title }}</th>
                <td>{{ trip.destination }} ({{ trip.country_code }})</td>
                <td>{{ trip.started_at.slice(0, 10) }} – {{ trip.ended_at.slice(0, 10) }}</td>
                <td>{{ trip.net_per_diem_total.toFixed(2) }} {{ trip.currency }}</td>
                <td>{{ trip.mileage_allowance.toFixed(2) }} {{ trip.currency }}</td>
                <td><strong>{{ trip.total_reimbursement.toFixed(2) }} {{ trip.currency }}</strong></td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="cockpit-card" aria-labelledby="tax-tally-title">
        <h3 id="tax-tally-title">Annual Workplace Tax-Return Tally</h3>
        <div
          tabindex="0"
          role="region"
          aria-labelledby="tax-table-caption"
          class="table-wrapper"
        >
          <table class="responsive-data-table">
            <caption id="tax-table-caption">
              Classified Workplace Days and Tracked Hours by Provenance
            </caption>
            <thead>
              <tr>
                <th scope="col">Workplace</th>
                <th scope="col">Scope Date</th>
                <th scope="col">Provenance</th>
                <th scope="col">Tracked Duration</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="fact in workplaceFacts" :key="fact.id">
                <th scope="row"><code>{{ fact.value }}</code></th>
                <td>{{ fact.scope_date ?? '2026-11-07' }}</td>
                <td><span class="badge">{{ fact.source }}</span></td>
                <td>
                  <time :datetime="formatIsoDuration(totalIntervalSeconds)">
                    {{ formatHumanDuration(totalIntervalSeconds) }}
                  </time>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="cockpit-card" aria-labelledby="entity-statement-title">
        <h3 id="entity-statement-title">Weighted Multi-Entity Attribution Statement</h3>
        <div
          tabindex="0"
          role="region"
          aria-labelledby="attribution-export-caption"
          class="table-wrapper"
        >
          <table class="responsive-data-table">
            <caption id="attribution-export-caption">
              DAG Attribution Shares Across Customer, Project, Component, Epic, and CostCenter
            </caption>
            <thead>
              <tr>
                <th scope="col">Entity Name</th>
                <th scope="col">Entity Kind</th>
                <th scope="col">Share Weight</th>
                <th scope="col">Attributed Duration</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="edge in attributions" :key="edge.id">
                <th scope="row">{{ edge.target_entity?.name ?? edge.target_entity_id }}</th>
                <td><code>{{ edge.target_entity?.kind ?? 'project' }}</code></td>
                <td>{{ Math.round(edge.weight * 100) }}%</td>
                <td>
                  <time :datetime="formatIsoDuration(Math.round(totalIntervalSeconds * edge.weight))">
                    {{ formatHumanDuration(Math.round(totalIntervalSeconds * edge.weight)) }}
                  </time>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>
    </div>

    <article class="cockpit-card" aria-labelledby="export-format-preview-title">
      <h3 id="export-format-preview-title">Deterministic Export Renderer Preview</h3>
      <form class="cockpit-form" action="/api/v1/exports/workplace-tax/" method="GET" @submit.prevent>
        <fieldset>
          <legend>Select Export Serialization Format</legend>
          <div class="radio-group">
            <label for="export-format-markdown">
              <input
                id="export-format-markdown"
                v-model="selectedFormat"
                type="radio"
                name="export_format"
                value="markdown"
              />
              Markdown
            </label>
            <label for="export-format-csv">
              <input
                id="export-format-csv"
                v-model="selectedFormat"
                type="radio"
                name="export_format"
                value="csv"
              />
              CSV
            </label>
            <label for="export-format-json">
              <input
                id="export-format-json"
                v-model="selectedFormat"
                type="radio"
                name="export_format"
                value="json"
              />
              JSON
            </label>
          </div>
        </fieldset>
      </form>
      <pre data-testid="export-preview-output"><code>{{ activePreviewText }}</code></pre>
    </article>
  </section>
</template>
