<script setup lang="ts">
/**
 * Chaotic Interval & Attribution Editor View (`c` chord).
 * Provides accessible identifier forms (`autocorrect="off" autocapitalize="off" spellcheck="false"`)
 * with blur validation and input error clearing, plus a responsive `<table>` for `Intervals`.
 */
import { useRepo } from 'pinia-orm';
import { computed, ref } from 'vue';
import { formatHumanDuration, formatIsoDuration } from '../composables/useDurationFormat';
import { ActivityModel, AttributionModel, EntityModel, IntervalModel } from '../models';

const activityKey = ref('PROJ-142');
const entityKey = ref('WT');
const durationMinutes = ref('45');
const notesText = ref('');
const activityError = ref('');

const intervals = computed(() => useRepo(IntervalModel).with('activity').all());
const attributions = computed(() => useRepo(AttributionModel).with('target_entity').all());

/**
 * Validate the `Activity` identifier field on `blur` (`forms` Validation Event Timing Matrix).
 */
function validateActivityKeyOnBlur(): void {
  const trimmed = activityKey.value.trim();
  if (trimmed.length === 0) {
    activityError.value = 'Enter an Activity key or slug (for example PROJ-142) or leave chaotic.';
    return;
  }
  activityError.value = '';
}

/**
 * Clear validation error messages immediately on `input` as the user types a correction.
 */
function clearActivityErrorOnInput(): void {
  if (activityError.value) {
    activityError.value = '';
  }
}

/**
 * Record a new chaotic or structured `Interval` and optional `Attribution` edge in `pinia-orm`.
 */
function submitIntervalCapture(): void {
  const trimmedKey = activityKey.value.trim();
  const seconds = Math.max(60, (Number.parseInt(durationMinutes.value, 10) || 30) * 60);
  let matchedActivity = useRepo(ActivityModel)
    .where('external_ref', trimmedKey)
    .first();

  if (!matchedActivity && trimmedKey.length > 0) {
    matchedActivity = useRepo(ActivityModel).save({
      id: `act-${trimmedKey.toLowerCase()}`,
      name: trimmedKey,
      kind: 'targeted',
      source: 'local',
      external_ref: trimmedKey,
      rollup_target_ref: trimmedKey,
      tags: ['captured'],
      archived: false,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  }

  useRepo(IntervalModel).save({
    id: `int-${Date.now()}`,
    activity_id: matchedActivity ? matchedActivity.id : null,
    started_at: '2026-11-07T13:00:00Z',
    ended_at: '2026-11-07T13:45:00Z',
    duration_seconds: seconds,
    capture_mode: matchedActivity ? 'structured' : 'chaotic',
    notes: notesText.value.trim() || null,
    tags: ['cockpit'],
    is_open: false,
    is_allocated: Boolean(matchedActivity),
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  });

  const targetEntity = useRepo(EntityModel).where('key', entityKey.value.trim()).first();
  if (matchedActivity && targetEntity) {
    useRepo(AttributionModel).save({
      id: `attr-${Date.now()}`,
      source_activity_id: matchedActivity.id,
      source_entity_id: null,
      target_entity_id: targetEntity.id,
      weight: 1.0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  }
  notesText.value = '';
}
</script>

<template>
  <section aria-labelledby="capture-editor-heading" class="cockpit-panel">
    <h2 id="capture-editor-heading">Chaotic Interval &amp; Attribution Editor</h2>

    <form class="cockpit-form" action="/api/v1/intervals/" method="POST" @submit.prevent="submitIntervalCapture">
      <fieldset>
        <legend>Record Structured or Chaotic Interval</legend>
        <div class="form-group">
          <label for="activity-key-input">Activity Key or Jira Issue:</label>
          <span id="activity-key-hint" class="field-hint">
            Identifier code (e.g., PROJ-142). Leave blank for unallocated chaotic capture.
          </span>
          <input
            id="activity-key-input"
            v-model="activityKey"
            name="activity_key"
            type="text"
            autocorrect="off"
            autocapitalize="off"
            spellcheck="false"
            :aria-invalid="activityError ? 'true' : undefined"
            aria-describedby="activity-key-hint activity-key-error"
            @blur="validateActivityKeyOnBlur"
            @input="clearActivityErrorOnInput"
          />
          <p v-if="activityError" id="activity-key-error" class="field-error" role="alert">
            {{ activityError }}
          </p>
        </div>

        <div class="form-group">
          <label for="entity-key-input">Attribution Entity Key:</label>
          <span id="entity-key-hint" class="field-hint">
            Target Project or Customer Entity code (e.g., WT, BABELSBERG).
          </span>
          <input
            id="entity-key-input"
            v-model="entityKey"
            name="entity_key"
            type="text"
            autocorrect="off"
            autocapitalize="off"
            spellcheck="false"
            aria-describedby="entity-key-hint"
          />
        </div>

        <div class="form-group">
          <label for="interval-notes-input">Private Local Notes (Never Sent to Sinks):</label>
          <textarea
            id="interval-notes-input"
            v-model="notesText"
            name="notes"
            rows="2"
          ></textarea>
        </div>

        <button type="submit">Save Interval &amp; Attribution</button>
      </fieldset>
    </form>

    <div
      tabindex="0"
      role="region"
      aria-labelledby="intervals-table-caption"
      class="table-wrapper"
    >
      <table class="responsive-data-table">
        <caption id="intervals-table-caption">
          Recorded Structured and Chaotic Intervals ({{ attributions.length }} Attribution Edges)
        </caption>
        <thead>
          <tr>
            <th scope="col">Interval ID</th>
            <th scope="col">Activity</th>
            <th scope="col">Capture Mode</th>
            <th scope="col">Duration</th>
            <th scope="col">Local Notes</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="interval in intervals" :key="interval.id">
            <th scope="row"><code>{{ interval.id }}</code></th>
            <td>{{ interval.activity?.name ?? 'Unallocated (Chaotic)' }}</td>
            <td><span class="badge">{{ interval.capture_mode }}</span></td>
            <td>
              <time :datetime="formatIsoDuration(interval.duration_seconds)">
                {{ formatHumanDuration(interval.duration_seconds) }}
              </time>
            </td>
            <td>{{ interval.notes ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
