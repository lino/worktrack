<script setup lang="ts">
/**
 * Outbox Diff Preview & Immutable EgressLedger View (`o` chord).
 * Enforces the anti-surveillance Outbox gate with before/after duration diffing,
 * explicit user approval/rejection, and SHA-256 `EgressLedger` audit receipts.
 */
import { useRepo } from 'pinia-orm';
import { computed } from 'vue';
import { formatHumanDuration, formatIsoDuration } from '../composables/useDurationFormat';
import { EgressReceiptModel, OutboxItemModel } from '../models';

const outboxItems = computed(() => useRepo(OutboxItemModel).all());
const egressReceipts = computed(() => useRepo(EgressReceiptModel).all());

/**
 * Approve a staged `OutboxItem` proposal so it is eligible for `Sink` flush.
 * @param itemId Target `OutboxItem` identifier.
 */
function approveOutboxProposal(itemId: string): void {
  useRepo(OutboxItemModel).where('id', itemId).update({ status: 'approved' });
}

/**
 * Reject a staged `OutboxItem` proposal so it is never transmitted to an external `Sink`.
 * @param itemId Target `OutboxItem` identifier.
 */
function rejectOutboxProposal(itemId: string): void {
  useRepo(OutboxItemModel).where('id', itemId).update({
    status: 'rejected',
    rejection_reason: 'Rejected in browser cockpit',
  });
}
</script>

<template>
  <section aria-labelledby="outbox-ledger-heading" class="cockpit-panel">
    <h2 id="outbox-ledger-heading">Outbox Diff Preview &amp; EgressLedger</h2>

    <div
      tabindex="0"
      role="region"
      aria-labelledby="outbox-table-caption"
      class="table-wrapper"
    >
      <table class="responsive-data-table">
        <caption id="outbox-table-caption">
          Staged Outbox Rollup Proposals (Raw Notes Redacted Before Sink Egress)
        </caption>
        <thead>
          <tr>
            <th scope="col">Target Ref</th>
            <th scope="col">Sink &amp; Cadence</th>
            <th scope="col">Raw vs Rounded Diff</th>
            <th scope="col">Redacted Summary &amp; SHA-256</th>
            <th scope="col">Status &amp; Gate Action</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in outboxItems" :key="item.id">
            <th scope="row"><code>{{ item.target_ref }}</code></th>
            <td>{{ item.sink }} ({{ item.cadence }})</td>
            <td>
              <time :datetime="formatIsoDuration(item.raw_duration_seconds)">
                {{ formatHumanDuration(item.raw_duration_seconds) }}
              </time>
              &rarr;
              <time :datetime="formatIsoDuration(item.rounded_duration_seconds)">
                {{ formatHumanDuration(item.rounded_duration_seconds) }}
              </time>
            </td>
            <td>
              <div>{{ item.summary }}</div>
              <small>SHA-256: <code>{{ item.payload_sha256.slice(0, 16) }}…</code></small>
            </td>
            <td>
              <span class="badge">{{ item.status }}</span>
              <button
                type="button"
                :aria-label="`Approve Outbox item ${item.target_ref}`"
                @click="approveOutboxProposal(item.id)"
              >
                Approve
              </button>
              <button
                type="button"
                :aria-label="`Reject Outbox item ${item.target_ref}`"
                @click="rejectOutboxProposal(item.id)"
              >
                Reject
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div
      tabindex="0"
      role="region"
      aria-labelledby="egress-ledger-caption"
      class="table-wrapper"
    >
      <table class="responsive-data-table">
        <caption id="egress-ledger-caption">
          Immutable EgressLedger SHA-256 Audit Receipts
        </caption>
        <thead>
          <tr>
            <th scope="col">Receipt ID</th>
            <th scope="col">Sink &amp; Outcome</th>
            <th scope="col">Destination URI</th>
            <th scope="col">Payload SHA-256</th>
            <th scope="col">FRITZ_TRACE_ID</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="rec in egressReceipts" :key="rec.id">
            <th scope="row"><code>{{ rec.external_receipt_id ?? rec.id }}</code></th>
            <td>{{ rec.sink }} ({{ rec.outcome }} / HTTP {{ rec.http_status ?? 0 }})</td>
            <td><code>{{ rec.destination_uri }}</code></td>
            <td><code>{{ rec.payload_sha256.slice(0, 16) }}…</code></td>
            <td><code>{{ rec.trace_id }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
