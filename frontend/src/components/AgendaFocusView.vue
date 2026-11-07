<script setup lang="ts">
/**
 * Today / Week Agenda & FocusBlocks View (`p` chord).
 * Displays prioritized `AgendaItem` queues, remaining `<time :datetime>` budgets,
 * approaching deadlines, and scheduled `FocusBlocks`.
 */
import { useRepo } from 'pinia-orm';
import { computed } from 'vue';
import { formatHumanDuration, formatIsoDuration } from '../composables/useDurationFormat';
import { AgendaItemModel, FocusBlockModel } from '../models';

const agendaItems = computed(() =>
  useRepo(AgendaItemModel)
    .with('activity')
    .orderBy('priority', 'asc')
    .get(),
);
const focusBlocks = computed(() =>
  useRepo(FocusBlockModel)
    .with('activity')
    .all(),
);

/**
 * Mark an `AgendaItem` as `done` or `in_progress` inside the normalized store.
 * @param itemId Target `AgendaItem` identifier.
 * @param nextStatus Next `AgendaItem` lifecycle status.
 */
function updateAgendaStatus(
  itemId: string,
  nextStatus: 'planned' | 'in_progress' | 'done' | 'deferred',
): void {
  useRepo(AgendaItemModel).where('id', itemId).update({ status: nextStatus });
}
</script>

<template>
  <section aria-labelledby="agenda-focus-heading" class="cockpit-panel">
    <h2 id="agenda-focus-heading">Today / Week Agenda &amp; FocusBlocks</h2>
    <div class="cockpit-grid">
      <article class="cockpit-card" aria-labelledby="agenda-queue-title">
        <h3 id="agenda-queue-title">Prioritized Agenda Queue</h3>
        <div
          tabindex="0"
          role="region"
          aria-labelledby="agenda-table-caption"
          class="table-wrapper"
        >
          <table class="responsive-data-table">
            <caption id="agenda-table-caption">
              Scheduled Today and This Week Agenda items with time budgets
            </caption>
            <thead>
              <tr>
                <th scope="col">Activity</th>
                <th scope="col">Horizon</th>
                <th scope="col">Budget</th>
                <th scope="col">Deadline</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in agendaItems" :key="item.id">
                <th scope="row">{{ item.activity?.name ?? item.activity_id }}</th>
                <td><code>{{ item.horizon }}</code></td>
                <td>
                  <time :datetime="formatIsoDuration(item.budget_seconds)">
                    {{ formatHumanDuration(item.budget_seconds) }}
                  </time>
                </td>
                <td>{{ item.deadline ?? 'None' }}</td>
                <td>
                  <span class="badge">{{ item.status }}</span>
                  <button
                    v-if="item.status !== 'done'"
                    type="button"
                    @click="updateAgendaStatus(item.id, 'done')"
                  >
                    Complete {{ item.activity?.external_ref ?? item.id }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="cockpit-card" aria-labelledby="focus-blocks-title">
        <h3 id="focus-blocks-title">Scheduled FocusBlocks</h3>
        <ul>
          <li v-for="block in focusBlocks" :key="block.id">
            <strong>{{ block.label }}</strong>
            ({{ block.activity?.name ?? 'General Focus' }}) —
            <time :datetime="formatIsoDuration(block.duration_seconds)">
              {{ formatHumanDuration(block.duration_seconds) }}
            </time>
            <span class="badge">{{ block.expose_busy ? 'Busy Marker On' : 'Private' }}</span>
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>
