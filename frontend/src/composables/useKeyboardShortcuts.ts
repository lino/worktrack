/**
 * Global keyboard shortcut registry for the Worktrack cockpit (`Ctrl+K`, `:`, `?`,
 * and single-key chords `c`, `p`, `o`, `f`) with editable-control and IME suppression.
 */
import { type CockpitViewId, KEYBOARD_SHORTCUTS } from '../config/constants';

/** Callback table invoked by the keyboard shortcut listener. */
export interface KeyboardShortcutCallbacks {
  onNavigate: (view: CockpitViewId) => void;
  onOpenCommandPalette: () => void;
  onOpenShortcutHelp: () => void;
}

const EDITABLE_TAG_NAMES = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

const SINGLE_KEY_VIEW_MAP: Readonly<Record<string, CockpitViewId>> = {
  [KEYBOARD_SHORTCUTS.viewCapture]: 'capture',
  [KEYBOARD_SHORTCUTS.viewAgenda]: 'agenda',
  [KEYBOARD_SHORTCUTS.viewOutbox]: 'outbox',
  [KEYBOARD_SHORTCUTS.viewNow]: 'now',
};

/**
 * Determine whether a DOM event target is an editable form field or contenteditable surface.
 * @param target Event target from `KeyboardEvent.target`.
 * @returns True when single-key chords must be suppressed.
 */
export function isEditableEventTarget(target: EventTarget | null): boolean {
  if (!target || typeof target !== 'object') {
    return false;
  }
  const element = target as HTMLElement;
  if (typeof element.tagName === 'string' && EDITABLE_TAG_NAMES.has(element.tagName.toUpperCase())) {
    return true;
  }
  return Boolean(element.isContentEditable);
}

/**
 * Check whether the event is a `Ctrl+K` or `Meta+K` command-palette chord.
 * @param event Keyboard event.
 * @returns True when `Ctrl+K` or `Meta+K` was pressed.
 */
export function isCommandPaletteChord(event: KeyboardEvent): boolean {
  const hasPrimaryModifier = event.ctrlKey || event.metaKey;
  return hasPrimaryModifier && event.key.toLowerCase() === KEYBOARD_SHORTCUTS.commandPaletteKey;
}

/**
 * Check whether single-key chords should be ignored for the given keyboard event.
 * @param event Keyboard event.
 * @returns True when modifiers are held or focus is inside an editable control.
 */
export function shouldIgnoreSingleKeyChord(event: KeyboardEvent): boolean {
  if (event.ctrlKey || event.metaKey || event.altKey) {
    return true;
  }
  return isEditableEventTarget(event.target);
}

/**
 * Dispatch an unchorded single-key shortcut (`:`, `?`, `c`, `p`, `o`, `f`).
 * @param event Keyboard event.
 * @param callbacks Navigation and modal callbacks.
 */
function dispatchSingleKeyShortcut(
  event: KeyboardEvent,
  callbacks: KeyboardShortcutCallbacks,
): void {
  if (event.key === KEYBOARD_SHORTCUTS.commandPaletteColon) {
    event.preventDefault();
    callbacks.onOpenCommandPalette();
    return;
  }
  if (event.key === KEYBOARD_SHORTCUTS.shortcutHelp) {
    event.preventDefault();
    callbacks.onOpenShortcutHelp();
    return;
  }
  const targetView = SINGLE_KEY_VIEW_MAP[event.key.toLowerCase()];
  if (targetView) {
    event.preventDefault();
    callbacks.onNavigate(targetView);
  }
}

/**
 * Create a `keydown` listener routing `Ctrl+K` / `Meta+K`, `:`, `?`, and single-key view
 * chords (`c`, `p`, `o`, `f`) while ignoring IME composition and editable controls.
 * @param callbacks Navigation and modal callbacks.
 * @returns Event handler suitable for `window.addEventListener('keydown', ...)`.
 */
export function createKeyboardShortcutHandler(
  callbacks: KeyboardShortcutCallbacks,
): (event: KeyboardEvent) => void {
  return (event: KeyboardEvent): void => {
    if (event.isComposing) {
      return;
    }
    if (isCommandPaletteChord(event)) {
      event.preventDefault();
      callbacks.onOpenCommandPalette();
      return;
    }
    if (!shouldIgnoreSingleKeyChord(event)) {
      dispatchSingleKeyShortcut(event, callbacks);
    }
  };
}
