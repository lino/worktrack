/**
 * Behavioural tests for the keyboard shortcut registry (`Ctrl+K`, `:`, `?`, `c`, `p`, `o`, `f`),
 * editable/IME suppression, native `<dialog closedby="any">` pointer-coordinate light-dismiss
 * fallback, and OS-aware `color-scheme` dark-mode toggle.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { applyColorSchemeSelection, toggleColorScheme } from '../src/composables/useColorScheme';
import { createKeyboardShortcutHandler } from '../src/composables/useKeyboardShortcuts';
import {
  attachDialogLightDismissFallback,
  resetDialogReturnValue,
} from '../src/composables/useNativeDialog';

describe('Keyboard shortcut registry and native dialog light-dismiss fallback', () => {
  it('routes Ctrl+K, :, ?, and single-key chords c, p, o, f while suppressing editable targets and IME composition', () => {
    const onNavigate = vi.fn();
    const onOpenCommandPalette = vi.fn();
    const onOpenShortcutHelp = vi.fn();

    const handler = createKeyboardShortcutHandler({
      onNavigate,
      onOpenCommandPalette,
      onOpenShortcutHelp,
    });

    handler(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
    expect(onOpenCommandPalette).toHaveBeenCalledTimes(1);

    handler(new KeyboardEvent('keydown', { key: ':' }));
    expect(onOpenCommandPalette).toHaveBeenCalledTimes(2);

    handler(new KeyboardEvent('keydown', { key: '?' }));
    expect(onOpenShortcutHelp).toHaveBeenCalledTimes(1);

    handler(new KeyboardEvent('keydown', { key: 'c' }));
    expect(onNavigate).toHaveBeenLastCalledWith('capture');

    handler(new KeyboardEvent('keydown', { key: 'p' }));
    expect(onNavigate).toHaveBeenLastCalledWith('agenda');

    handler(new KeyboardEvent('keydown', { key: 'o' }));
    expect(onNavigate).toHaveBeenLastCalledWith('outbox');

    handler(new KeyboardEvent('keydown', { key: 'f' }));
    expect(onNavigate).toHaveBeenLastCalledWith('now');

    const inputEl = document.createElement('input');
    const inputEvent = new KeyboardEvent('keydown', { key: 'c' });
    Object.defineProperty(inputEvent, 'target', { value: inputEl });
    handler(inputEvent);
    expect(onNavigate).toHaveBeenCalledTimes(4);

    const imeEvent = new KeyboardEvent('keydown', { key: 'p', isComposing: true });
    handler(imeEvent);
    expect(onNavigate).toHaveBeenCalledTimes(4);
  });

  it('implements pointer-coordinate light-dismiss fallback for <dialog closedby="any"> and resets returnValue', () => {
    const dialog = document.createElement('dialog');
    dialog.setAttribute('closedby', 'any');
    dialog.close = vi.fn();
    dialog.getBoundingClientRect = () =>
      ({
        left: 100,
        top: 100,
        right: 400,
        bottom: 300,
        width: 300,
        height: 200,
      }) as DOMRect;

    attachDialogLightDismissFallback(dialog, { forceFallback: true });

    dialog.dispatchEvent(new MouseEvent('pointerdown', { clientX: 150, clientY: 150 }));
    dialog.dispatchEvent(new MouseEvent('pointerup', { clientX: 150, clientY: 150 }));
    expect(dialog.close).not.toHaveBeenCalled();

    dialog.dispatchEvent(new MouseEvent('pointerdown', { clientX: 150, clientY: 150 }));
    dialog.dispatchEvent(new MouseEvent('pointerup', { clientX: 20, clientY: 20 }));
    expect(dialog.close).not.toHaveBeenCalled();

    dialog.dispatchEvent(new MouseEvent('pointerdown', { clientX: 20, clientY: 20 }));
    dialog.dispatchEvent(new MouseEvent('pointerup', { clientX: 25, clientY: 25 }));
    expect(dialog.close).toHaveBeenCalledTimes(1);

    dialog.returnValue = 'execute-action';
    const consumed = resetDialogReturnValue(dialog);
    expect(consumed).toBe('execute-action');
    expect(dialog.returnValue).toBe('');
  });
});

describe('Color scheme toggle and OS preference synchronization', () => {
  beforeEach(() => {
    document.head.innerHTML = '<meta name="color-scheme" content="light dark" />';
    localStorage.clear();
  });

  it('stores explicit override when diverging from OS preference and removes localStorage entry when matching OS', () => {
    const meta = document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement;

    const firstState = toggleColorScheme({ systemPrefersDark: false });
    expect(firstState).toBe('dark');
    expect(localStorage.getItem('color-scheme')).toBe('dark');
    expect(meta.content).toBe('dark');

    const secondState = toggleColorScheme({ systemPrefersDark: false });
    expect(secondState).toBe('system');
    expect(localStorage.getItem('color-scheme')).toBeNull();
    expect(meta.content).toBe('light dark');

    applyColorSchemeSelection('dark', { systemPrefersDark: true });
    expect(localStorage.getItem('color-scheme')).toBeNull();
    expect(meta.content).toBe('light dark');
  });
});
