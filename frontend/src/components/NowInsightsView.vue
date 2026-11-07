<script setup lang="ts">
/**
 * Now & Insights Cockpit View (`f` chord).
 * Displays the active `Interval`, balanced `Intl.DurationFormat` elapsed time,
 * `Activity` and `Entity` attribution breakdown, and local `workplace` `ContextFacts`.
 */
import { useRepo } from 'pinia-orm';
import { computed } from 'vue';
import { formatHumanDuration, formatIsoDuration } from '../composables/useDurationFormat';
import {
  ActivityModel,
  AttributionModel,
  ContextFactModel,
  IntervalModel,
} from '../models';

const intervals = computed(() => useRepo(IntervalModel).with('activity').all());
const activeInterval = computed(
  () => intervals.value.find((item) => item.is_open) ?? intervals.value[0] ?? null,
);
const totalTrackedSeconds = computed(() =>
  intervals.value.reduce((acc, item) => acc + (item.duration_seconds ?? 0), 0),
);
const workplaceFacts = computed(() =>
  useRepo(ContextFactModel)
    .where('facet_key', 'workplace')
    .get(),
);
const attributions = computed(() =>
  useRepo(AttributionModel).with('target_entity').all(),
);
const activities = computed(() => useRepo(ActivityModel).all());

/**
 * Update the local `workplace` `ContextFact` assertion (`home_office`, `office`, `mobile_work`).
 * @param workplace Target workplace enum value.
 */
function assertWorkplaceFact(workplace: string): void {
  useRepo(ContextFactModel).save({
    id: 'fact-workplace-today',
    facet_key: 'workplace',
    value: workplace,
    scope_date: '2026-11-07',
    interval_id: null,
    source: 'manual',
    updated_at: new Date().toISOString(),
  });
}
</script>

<template>
  <section aria-labelledby="now-insights-heading" class="cockpit-panel">
    <h2 id="now-insights-heading">Now &amp; Insights</h2>
    <div class="cockpit-grid">
      <article class="cockpit-card" aria-labelledby="active-interval-title">
        <h3 id="active-interval-title">Active Interval</h3>
        <template v-if="activeInterval">
          <p class="primary-metric">
            <strong>{{ activeInterval.activity?.name ?? 'Unallocated Chaotic Interval' }}</strong>
            <span class="badge">{{ activeInterval.capture_mode }}</span>
          </p>
          <p>
            Elapsed:
            <time :datetime="formatIsoDuration(activeInterval.duration_seconds)">
              {{ formatHumanDuration(activeInterval.duration_seconds) }}
            </time>
          </p>
        </template>
        <p v-else>No active interval clocked in.</p>
        <p>
          Daily Tracked Total:
          <time :datetime="formatIsoDuration(totalTrackedSeconds)">
            {{ formatHumanDuration(totalTrackedSeconds) }}
          </time>
        </p>
      </article>

      <article class="cockpit-card" aria-labelledby="workplace-fact-title">
        <h3 id="workplace-fact-title">Local Workplace ContextFact (Tax Ledger)</h3>
        <p v-for="fact in workplaceFacts" :key="fact.id">
          Current `workplace`: <code>{{ fact.value }}</code> (scope: {{ fact.scope_date ?? 'interval' }})
        </p>
        <div class="button-row" role="group" aria-label="Set workplace ContextFact">
          <button type="button" @click="assertWorkplaceFact('home_office')">
            home_office
          </button>
          <button type="button" @click="assertWorkplaceFact('office')">
            office
          </button>
          <button type="button" @click="assertWorkplaceFact('mobile_work')">
            mobile_work
          </button>
        </div>
      </article>

      <article class="cockpit-card" aria-labelledby="attribution-shares-title">
        <h3 id="attribution-shares-title">Entity Attribution Shares ({{ activities.length }} Activities)</h3>
        <ul>
          <li v-for="edge in attributions" :key="edge.id">
            <strong>{{ edge.target_entity?.name ?? edge.target_entity_id }}</strong>
            ({{ edge.target_entity?.kind ?? 'entity' }}):
            <span>{{ Math.round(edge.weight * 100) }}%</span>
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>
