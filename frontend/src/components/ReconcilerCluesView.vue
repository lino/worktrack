<script setup lang="ts">
/**
 * Reconciler, Clues & Workplace Sensors Cockpit View (`s` chord).
 * Displays local `Clue` hints, `SensorSuggestion` workplace inference rules, and
 * duration-conserving `RetroSplit` proposals with interactive strategy selection.
 */
import { useRepo } from 'pinia-orm';
import { computed, ref } from 'vue';
import { formatHumanDuration, formatIsoDuration } from '../composables/useDurationFormat';
import {
  ClueModel,
  ContextFactModel,
  IntervalModel,
  ReconcileProposalModel,
  SensorSuggestionModel,
} from '../models';

const selectedStrategy = ref<'proportional' | 'equal' | 'weights'>('proportional');

const clues = computed(() => useRepo(ClueModel).with('activity').get());
const sensorSuggestions = computed(() => useRepo(SensorSuggestionModel).all());
const proposals = computed(() => useRepo(ReconcileProposalModel).all());
const activeProposal = computed(() => proposals.value[0] ?? null);

/**
 * Apply a local `SensorSuggestionModel` into `ContextFactModel` with `source='sensor'`.
 * @param suggestion Evaluated workplace sensor suggestion.
 */
function applySensorSuggestion(suggestion: SensorSuggestionModel): void {
  useRepo(ContextFactModel).save({
    id: suggestion.context_fact_id ?? `fact-workplace-${suggestion.scope_date}`,
    facet_key: 'workplace',
    value: suggestion.workplace,
    scope_date: suggestion.scope_date,
    interval_id: null,
    source: 'sensor',
    updated_at: new Date().toISOString(),
  });
}

/**
 * Recompute slice durations for the active `ReconcileProposalModel` and commit
 * structured child `IntervalModel` slices into `pinia-orm`.
 */
function applyRetroSplitProposal(): void {
  const proposal = activeProposal.value;
  if (!proposal || proposal.slices.length === 0) {
    return;
  }
  const count = proposal.slices.length;
  const totalSeconds = proposal.total_duration_seconds;
  const equalSliceSeconds = Math.floor(totalSeconds / count);
  const updatedSlices = proposal.slices.map((slc, idx) => {
    const duration =
      selectedStrategy.value === 'equal'
        ? idx === count - 1
          ? totalSeconds - equalSliceSeconds * (count - 1)
          : equalSliceSeconds
        : slc.duration_seconds;
    return {
      ...slc,
      duration_seconds: duration,
      share: Number((duration / Math.max(1, totalSeconds)).toFixed(2)),
    };
  });

  useRepo(ReconcileProposalModel)
    .where('proposal_id', proposal.proposal_id)
    .update({
      strategy: selectedStrategy.value,
      slices: updatedSlices,
    });

  for (const rawId of proposal.interval_ids) {
    useRepo(IntervalModel).destroy(rawId);
  }
  useRepo(IntervalModel).save(
    updatedSlices.map((slc, idx) => ({
      id: `${slc.interval_id}-split-${idx + 1}`,
      activity_id: slc.activity_id,
      started_at: slc.started_at,
      ended_at: slc.ended_at,
      duration_seconds: slc.duration_seconds,
      capture_mode: 'structured' as const,
      notes: slc.notes,
      tags: slc.tags,
      is_open: false,
      is_allocated: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    })),
  );
}
</script>

<template>
  <section aria-labelledby="reconciler-clues-heading" class="cockpit-panel">
    <h2 id="reconciler-clues-heading">Reconciler, Clues &amp; Workplace Sensors</h2>

    <div class="cockpit-grid">
      <article class="cockpit-card" aria-labelledby="sensor-suggestions-title">
        <h3 id="sensor-suggestions-title">Local Workplace Sensor Inference</h3>
        <div
          v-for="suggestion in sensorSuggestions"
          :key="suggestion.scope_date"
          class="sensor-summary"
        >
          <p>
            Scope Date: <strong>{{ suggestion.scope_date }}</strong> &mdash; Inferred Workplace:
            <code>{{ suggestion.workplace }}</code> (rule: <code>{{ suggestion.rule_id }}</code>,
            confidence: {{ Math.round(suggestion.confidence * 100) }}%)
          </p>
          <p>
            Matched Local Signals:
            <code>{{ suggestion.matched_signals.join(', ') }}</code>
          </p>
          <button
            type="button"
            data-testid="btn-apply-sensor"
            @click="applySensorSuggestion(suggestion)"
          >
            Apply Sensor Suggestion ({{ suggestion.workplace }})
          </button>
        </div>
      </article>

      <article class="cockpit-card" aria-labelledby="retrosplit-engine-title">
        <h3 id="retrosplit-engine-title">Plausibility Reconciler &amp; RetroSplit Engine</h3>
        <form
          class="cockpit-form"
          action="/api/v1/reconciler/apply/"
          method="POST"
          @submit.prevent="applyRetroSplitProposal"
        >
          <fieldset>
            <legend>Select Duration-Conserving RetroSplit Strategy</legend>
            <div class="radio-group">
              <label for="strategy-proportional">
                <input
                  id="strategy-proportional"
                  v-model="selectedStrategy"
                  type="radio"
                  name="retrosplit_strategy"
                  value="proportional"
                />
                Proportional (Clue &amp; Agenda Weights)
              </label>
              <label for="strategy-equal">
                <input
                  id="strategy-equal"
                  v-model="selectedStrategy"
                  type="radio"
                  name="retrosplit_strategy"
                  value="equal"
                />
                Equal Split
              </label>
              <label for="strategy-weights">
                <input
                  id="strategy-weights"
                  v-model="selectedStrategy"
                  type="radio"
                  name="retrosplit_strategy"
                  value="weights"
                />
                Explicit Weights
              </label>
            </div>
            <button type="submit" data-testid="btn-apply-retrosplit">
              Apply RetroSplit Proposal
            </button>
          </fieldset>
        </form>

        <div
          v-if="activeProposal"
          tabindex="0"
          role="region"
          aria-labelledby="retrosplit-table-caption"
          class="table-wrapper"
        >
          <table class="responsive-data-table">
            <caption id="retrosplit-table-caption">
              Proposed RetroSplit Slices ({{ activeProposal.strategy }}, Total:
              <time :datetime="formatIsoDuration(activeProposal.total_duration_seconds)">
                {{ formatHumanDuration(activeProposal.total_duration_seconds) }}
              </time>)
            </caption>
            <thead>
              <tr>
                <th scope="col">Target Activity</th>
                <th scope="col">Source Interval</th>
                <th scope="col">Proposed Duration</th>
                <th scope="col">Share</th>
                <th scope="col">Notes</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="slc in activeProposal.slices"
                :key="`${slc.interval_id}-${slc.activity_id}`"
              >
                <th scope="row">{{ slc.activity_name }}</th>
                <td><code>{{ slc.interval_id }}</code></td>
                <td>
                  <time :datetime="formatIsoDuration(slc.duration_seconds)">
                    {{ formatHumanDuration(slc.duration_seconds) }}
                  </time>
                </td>
                <td>{{ Math.round(slc.share * 100) }}%</td>
                <td>{{ slc.notes ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>
    </div>

    <div
      tabindex="0"
      role="region"
      aria-labelledby="clues-table-caption"
      class="table-wrapper"
    >
      <table class="responsive-data-table">
        <caption id="clues-table-caption">
          Ambient Local Clues (Git Commits, Forge MRs &amp; Calendar Hints — Never Sent to Sinks)
        </caption>
        <thead>
          <tr>
            <th scope="col">External Key</th>
            <th scope="col">Collector Source</th>
            <th scope="col">Inferred Activity</th>
            <th scope="col">Summary</th>
            <th scope="col">Confidence</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="clue in clues" :key="clue.id">
            <th scope="row"><code>{{ clue.external_key }}</code></th>
            <td><span class="badge">{{ clue.source }}</span></td>
            <td><code>{{ clue.inferred_activity_key ?? 'Unlinked' }}</code></td>
            <td>{{ clue.summary }}</td>
            <td>{{ Math.round(clue.confidence * 100) }}%</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
