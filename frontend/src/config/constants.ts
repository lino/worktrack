/**
 * Configurable constants, canonical API routes, keyboard chords, and color-scheme keys
 * for the Worktrack (`wt`) browser cockpit.
 */

/** Default loopback backend origin when `VITE_WORKTRACK_API_URL` is unset. */
export const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000';

/** W3C trace correlation header exchanged with `worktrack.web`. */
export const HEADER_FRITZ_TRACE_ID = 'X-Fritz-Trace-Id';

/** Canonical REST API v1 route paths. */
export const API_ROUTES = {
  activities: '/api/v1/activities/',
  intervals: '/api/v1/intervals/',
  intervalsClockIn: '/api/v1/intervals/clock-in/',
  intervalsClockOut: '/api/v1/intervals/clock-out/',
  entities: '/api/v1/entities/',
  attributions: '/api/v1/attributions/',
  contextFacts: '/api/v1/context-facts/',
  agendaItems: '/api/v1/agenda/items/',
  focusBlocks: '/api/v1/agenda/focus-blocks/',
  outboxItems: '/api/v1/outbox/items/',
  outboxPreview: '/api/v1/outbox/preview/',
  outboxFlush: '/api/v1/outbox/flush/',
  egressLedger: '/api/v1/egress-ledger/',
  insightsNow: '/api/v1/insights/now/',
  insightsToday: '/api/v1/insights/today/',
  insightsWeek: '/api/v1/insights/week/',
} as const;

/** Cockpit navigation view identifiers. */
export type CockpitViewId = 'now' | 'agenda' | 'capture' | 'outbox';

/** Single-key and chord bindings for the keyboard-first cockpit. */
export const KEYBOARD_SHORTCUTS = {
  commandPaletteKey: 'k',
  commandPaletteColon: ':',
  shortcutHelp: '?',
  viewNow: 'f',
  viewAgenda: 'p',
  viewCapture: 'c',
  viewOutbox: 'o',
} as const;

/** LocalStorage and `<meta name="color-scheme">` constants. */
export const COLOR_SCHEME_CONFIG = {
  storageKey: 'color-scheme',
  metaDefault: 'light dark',
  light: 'light',
  dark: 'dark',
  system: 'system',
  mediaQueryDark: '(prefers-color-scheme: dark)',
} as const;

/** Time conversion constants for balanced duration formatting. */
export const DURATION_CONSTANTS = {
  secondsPerMinute: 60,
  secondsPerHour: 3600,
  defaultLocale: 'en-US',
} as const;

/** HTTP status class boundaries for bounded telemetry counters. */
export const HTTP_STATUS_BOUNDS = {
  okMin: 200,
  redirectMin: 300,
  clientErrorMin: 400,
  serverErrorMin: 500,
} as const;
