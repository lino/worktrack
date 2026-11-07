<script setup lang="ts">
/**
 * Tax & Multi-Entity Attribution Exports Cockpit View.
 * Renders annual `workplace` tax-return tallies (`home_office`, `office`, `mobile_work`)
 * and weighted multi-entity `Attribution` statements with `CSV`, `JSON`, and `Markdown` previews.
 */
import { useRepo } from 'pinia-orm';
import { computed, ref } from 'vue';
import { formatHumanDuration, formatIsoDuration } from '../composables/useDurationFormat';
import {
  AttributionModel,
  ContextFactModel,
  ExportReportModel,
  IntervalModel,
} from '../models';

const selectedFormat = ref<'markdown' | 'csv' | 'json'>('markdown');

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
