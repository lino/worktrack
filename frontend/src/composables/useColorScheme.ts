/**
 * Color scheme state management and OS-aware `<meta name="color-scheme">` synchronization
 * (`dark-mode` & `dark-mode-toggle`).
 */
import { COLOR_SCHEME_CONFIG } from '../config/constants';

/** Supported color scheme state values. */
export type ColorSchemeState = 'system' | 'light' | 'dark';

/** Options for deterministic testing or runtime color scheme evaluation. */
export interface ColorSchemeOptions {
  systemPrefersDark?: boolean;
}

/**
 * Detect whether the user's operating system currently prefers a dark color scheme.
 * @param options Optional override for deterministic tests.
 * @returns True when `(prefers-color-scheme: dark)` matches.
 */
export function getSystemPrefersDark(options: ColorSchemeOptions = {}): boolean {
  if (typeof options.systemPrefersDark === 'boolean') {
    return options.systemPrefersDark;
  }
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    return window.matchMedia(COLOR_SCHEME_CONFIG.mediaQueryDark).matches;
  }
  return false;
}

/**
 * Read the currently stored color scheme override from `localStorage`, if any.
 * @returns `'light'`, `'dark'`, or `'system'` when no override is stored.
 */
export function getStoredColorScheme(): ColorSchemeState {
  try {
    const raw = localStorage.getItem(COLOR_SCHEME_CONFIG.storageKey);
    if (raw === COLOR_SCHEME_CONFIG.light || raw === COLOR_SCHEME_CONFIG.dark) {
      return raw;
    }
  } catch {
    return COLOR_SCHEME_CONFIG.system;
  }
  return COLOR_SCHEME_CONFIG.system;
}

/**
 * Resolve the currently rendered visual scheme (`'light'` or `'dark'`).
 * @param options Optional OS preference override.
 * @returns `'light'` or `'dark'`.
 */
export function getResolvedVisualScheme(options: ColorSchemeOptions = {}): 'light' | 'dark' {
  const stored = getStoredColorScheme();
  if (stored === COLOR_SCHEME_CONFIG.light || stored === COLOR_SCHEME_CONFIG.dark) {
    return stored;
  }
  return getSystemPrefersDark(options) ? COLOR_SCHEME_CONFIG.dark : COLOR_SCHEME_CONFIG.light;
}

/**
 * Update `<meta name="color-scheme">` content in `document.head`.
 * @param content Target `content` attribute value (`'light dark'`, `'light'`, or `'dark'`).
 */
export function syncColorSchemeMeta(content: string): void {
  if (typeof document === 'undefined') {
    return;
  }
  let meta = document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement | null;
  if (!meta) {
    meta = document.createElement('meta');
    meta.name = 'color-scheme';
    document.head.prepend(meta);
  }
  meta.content = content;
}

/**
 * Apply a color scheme selection (`'system'`, `'light'`, or `'dark'`).
 * When the target scheme matches the current OS preference, removes the `localStorage`
 * override and restores `<meta content="light dark">` so the site is not pinned against
 * future OS transitions.
 * @param target Desired color scheme state.
 * @param options Optional OS preference override.
 * @returns Effective persisted state (`'system'`, `'light'`, or `'dark'`).
 */
export function applyColorSchemeSelection(
  target: ColorSchemeState,
  options: ColorSchemeOptions = {},
): ColorSchemeState {
  const osScheme = getSystemPrefersDark(options)
    ? COLOR_SCHEME_CONFIG.dark
    : COLOR_SCHEME_CONFIG.light;

  if (target === COLOR_SCHEME_CONFIG.system || target === osScheme) {
    try {
      localStorage.removeItem(COLOR_SCHEME_CONFIG.storageKey);
    } catch {
      // Ignore storage access errors in restricted contexts
    }
    syncColorSchemeMeta(COLOR_SCHEME_CONFIG.metaDefault);
    return COLOR_SCHEME_CONFIG.system;
  }

  try {
    localStorage.setItem(COLOR_SCHEME_CONFIG.storageKey, target);
  } catch {
    // Ignore storage access errors in restricted contexts
  }
  syncColorSchemeMeta(target);
  return target;
}

/**
 * Toggle the currently rendered color scheme to its opposite, reverting to `'system'`
 * whenever the new target matches the current OS preference.
 * @param options Optional OS preference override.
 * @returns New persisted `ColorSchemeState`.
 */
export function toggleColorScheme(options: ColorSchemeOptions = {}): ColorSchemeState {
  const currentVisual = getResolvedVisualScheme(options);
  const nextTarget =
    currentVisual === COLOR_SCHEME_CONFIG.dark
      ? COLOR_SCHEME_CONFIG.light
      : COLOR_SCHEME_CONFIG.dark;
  return applyColorSchemeSelection(nextTarget, options);
}
