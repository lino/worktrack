<script setup lang="ts">
/**
 * Root Worktrack (`wt`) Browser Cockpit application component.
 * Coordinates the 4 views (*Now & Insights*, *Today / Week Agenda & FocusBlocks*,
 * *Chaotic Interval & Attribution Editor*, *Outbox Diff Preview & EgressLedger*),
 * global keyboard shortcuts (`Ctrl+K`, `:`, `?`, `c`, `p`, `o`, `f`), native `<dialog>`
 * modals, and OS-aware color-scheme toggling.
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';
import AgendaFocusView from './components/AgendaFocusView.vue';
import CaptureAttributionView from './components/CaptureAttributionView.vue';
import ExportsHubView from './components/ExportsHubView.vue';
import NowInsightsView from './components/NowInsightsView.vue';
import OutboxLedgerView from './components/OutboxLedgerView.vue';
import ReconcilerCluesView from './components/ReconcilerCluesView.vue';
import {
  type ColorSchemeState,
  getStoredColorScheme,
  toggleColorScheme,
} from './composables/useColorScheme';
import { createKeyboardShortcutHandler } from './composables/useKeyboardShortcuts';
import {
  attachDialogLightDismissFallback,
  openNativeModalDialog,
  resetDialogReturnValue,
} from './composables/useNativeDialog';
import type { CockpitViewId } from './config/constants';
import { seedCockpitDemoState } from './models';
import { WorktrackTransportClient } from './transport/client';

seedCockpitDemoState();

const activeView = ref<CockpitViewId>('now');
const colorSchemeState = ref<ColorSchemeState>(getStoredColorScheme());
const commandSearchQuery = ref('');
const commandDialogRef = ref<HTMLDialogElement | null>(null);
const helpDialogRef = ref<HTMLDialogElement | null>(null);
const transportClient = new WorktrackTransportClient();
const transportErrorCount = ref(transportClient.getTransportMetrics().totalErrorCount);

/**
 * Switch the active cockpit view (`now`, `agenda`, `capture`, or `outbox`).
 * @param view Target view identifier.
 */
function selectView(view: CockpitViewId): void {
  activeView.value = view;
}

/**
 * Open the native `<dialog>` Command Palette (`Ctrl+K` or `:`).
 */
function openCommandPalette(): void {
  openNativeModalDialog(commandDialogRef.value);
}

/**
 * Open the native `<dialog>` Keyboard Shortcut Help modal (`?`).
 */
function openShortcutHelp(): void {
  openNativeModalDialog(helpDialogRef.value);
}

const VALID_VIEW_IDS = new Set<CockpitViewId>([
  'now',
  'agenda',
  'capture',
  'outbox',
  'reconcile',
  'exports',
]);

const COMMAND_PALETTE_MATCHERS: ReadonlyArray<readonly [readonly string[], CockpitViewId]> = [
  [['outbox', 'o'], 'outbox'],
  [['agenda', 'plan', 'p'], 'agenda'],
  [['capture', 'interval', 'c'], 'capture'],
  [['reconcile', 'clue', 's'], 'reconcile'],
  [['export', 'tax'], 'exports'],
];

/**
 * Resolve a free-text Command Palette query to a target `CockpitViewId`.
 * @param rawQuery Raw search input string.
 * @returns Matched cockpit view identifier.
 */
function resolveCommandPaletteTarget(rawQuery: string): CockpitViewId {
  const q = rawQuery.trim().toLowerCase();
  for (const [tokens, viewId] of COMMAND_PALETTE_MATCHERS) {
    if (tokens.some((token) => (token.length === 1 ? q === token : q.includes(token)))) {
      return viewId;
    }
  }
  return 'now';
}

/**
 * Handle native `<dialog>` `close` events, consuming and resetting `dialog.returnValue`.
 * @param event Native dialog close event.
 */
function handleCommandDialogClose(event: Event): void {
  const dialog = event.currentTarget as HTMLDialogElement | null;
  const action = resetDialogReturnValue(dialog) as CockpitViewId;
  if (VALID_VIEW_IDS.has(action)) {
    activeView.value = action;
  }
}

/**
 * Handle `close` on the Keyboard Shortcut Help `<dialog>`, resetting `returnValue`.
 * @param event Native dialog close event.
 */
function handleHelpDialogClose(event: Event): void {
  resetDialogReturnValue(event.currentTarget as HTMLDialogElement | null);
}

/**
 * Execute a command from the Command Palette form and close the native modal.
 */
function executeCommandPaletteSearch(): void {
  activeView.value = resolveCommandPaletteTarget(commandSearchQuery.value);
  commandSearchQuery.value = '';
  commandDialogRef.value?.close();
}

/**
 * Toggle between light/dark override and OS system default (`useColorScheme`).
 */
function handleThemeToggle(): void {
  colorSchemeState.value = toggleColorScheme();
}

const shortcutHandler = createKeyboardShortcutHandler({
  onNavigate: selectView,
  onOpenCommandPalette: openCommandPalette,
  onOpenShortcutHelp: openShortcutHelp,
});

onMounted(() => {
  window.addEventListener('keydown', shortcutHandler);
  attachDialogLightDismissFallback(commandDialogRef.value);
  attachDialogLightDismissFallback(helpDialogRef.value);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', shortcutHandler);
});
</script>

<template>
  <div class="cockpit-shell">
    <a href="#cockpit-heading" class="skip-link visually-hidden">Skip to main content</a>

    <header class="cockpit-header">
      <div class="header-top">
        <h1 id="cockpit-heading" tabindex="-1">
          Worktrack (`wt`) — Anti-Surveillance Time Ledger &amp; Cockpit
        </h1>
        <div class="header-controls">
          <span class="badge" data-testid="transport-error-counter">
            API Transport Errors: {{ transportErrorCount }}
          </span>
          <button type="button" data-testid="btn-command-palette" @click="openCommandPalette">
            Command Palette (Ctrl+K / :)
          </button>
          <button type="button" data-testid="btn-shortcut-help" @click="openShortcutHelp">
            Shortcuts (?)
          </button>
          <button type="button" data-testid="btn-theme-toggle" @click="handleThemeToggle">
            Theme ({{ colorSchemeState }})
          </button>
        </div>
      </div>

      <nav aria-label="Primary">
        <ul class="nav-list">
          <li>
            <button
              type="button"
              data-testid="nav-now"
              :aria-current="activeView === 'now' ? 'page' : undefined"
              @click="selectView('now')"
            >
              Now &amp; Insights (f)
            </button>
          </li>
          <li>
            <button
              type="button"
              data-testid="nav-agenda"
              :aria-current="activeView === 'agenda' ? 'page' : undefined"
              @click="selectView('agenda')"
            >
              Agenda &amp; FocusBlocks (p)
            </button>
          </li>
          <li>
            <button
              type="button"
              data-testid="nav-capture"
              :aria-current="activeView === 'capture' ? 'page' : undefined"
              @click="selectView('capture')"
            >
              Chaotic Interval &amp; Attribution (c)
            </button>
          </li>
          <li>
            <button
              type="button"
              data-testid="nav-outbox"
              :aria-current="activeView === 'outbox' ? 'page' : undefined"
              @click="selectView('outbox')"
            >
              Outbox Diff &amp; EgressLedger (o)
            </button>
          </li>
          <li>
            <button
              type="button"
              data-testid="nav-reconcile"
              :aria-current="activeView === 'reconcile' ? 'page' : undefined"
              @click="selectView('reconcile')"
            >
              Reconciler &amp; Clues (s)
            </button>
          </li>
          <li>
            <button
              type="button"
              data-testid="nav-exports"
              :aria-current="activeView === 'exports' ? 'page' : undefined"
              @click="selectView('exports')"
            >
              Tax &amp; Attribution Exports
            </button>
          </li>
        </ul>
      </nav>
    </header>

    <main id="main-content" class="cockpit-main">
      <NowInsightsView v-if="activeView === 'now'" />
      <AgendaFocusView v-else-if="activeView === 'agenda'" />
      <CaptureAttributionView v-else-if="activeView === 'capture'" />
      <ReconcilerCluesView v-else-if="activeView === 'reconcile'" />
      <ExportsHubView v-else-if="activeView === 'exports'" />
      <OutboxLedgerView v-else />
    </main>

    <dialog
      id="command-palette-dialog"
      ref="commandDialogRef"
      closedby="any"
      aria-labelledby="command-palette-title"
      @close="handleCommandDialogClose"
    >
      <h2 id="command-palette-title">Command Palette</h2>
      <form method="dialog" @submit.prevent="executeCommandPaletteSearch">
        <div class="form-group">
          <label for="command-palette-input">Jump to view or execute command:</label>
          <span id="command-palette-hint" class="field-hint">
            Type `now`, `agenda`, `capture`, `outbox`, `reconcile`, or `export` and press Enter.
          </span>
          <input
            id="command-palette-input"
            v-model="commandSearchQuery"
            name="command"
            type="search"
            autofocus
            autocorrect="off"
            autocapitalize="off"
            spellcheck="false"
            aria-describedby="command-palette-hint"
          />
        </div>
        <div class="button-row">
          <button type="submit">Execute Command</button>
          <button type="button" @click="commandDialogRef?.close()">Close</button>
        </div>
      </form>
    </dialog>

    <dialog
      id="shortcut-help-dialog"
      ref="helpDialogRef"
      closedby="any"
      aria-labelledby="shortcut-help-title"
      @close="handleHelpDialogClose"
    >
      <h2 id="shortcut-help-title">Keyboard Shortcuts</h2>
      <ul>
        <li><kbd>Ctrl+K</kbd> or <kbd>:</kbd> — Open Command Palette</li>
        <li><kbd>?</kbd> — Open Keyboard Shortcut Help</li>
        <li><kbd>f</kbd> — Switch to Now &amp; Insights View</li>
        <li><kbd>p</kbd> — Switch to Today / Week Agenda &amp; FocusBlocks View</li>
        <li><kbd>c</kbd> — Switch to Chaotic Interval &amp; Attribution Editor</li>
        <li><kbd>o</kbd> — Switch to Outbox Diff Preview &amp; EgressLedger</li>
        <li><kbd>s</kbd> — Switch to Reconciler, Clues &amp; Workplace Sensors</li>
      </ul>
      <form method="dialog">
        <button value="close" autofocus>Close Shortcuts</button>
      </form>
    </dialog>
  </div>
</template>
