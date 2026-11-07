/**
 * Native `<dialog>` modal helpers and pointer-coordinate light-dismiss fallback
 * (`dialog` & `light-dismiss-a-dialog`).
 */

/** Options for attaching the `<dialog closedby="any">` fallback. */
export interface DialogFallbackOptions {
  forceFallback?: boolean;
}

/**
 * Test whether a pointer event's coordinates lie outside a `<dialog>` bounding box.
 * Tests pointer position rather than `event.target` so clicks on `<dialog>` padding
 * do not falsely close the modal.
 * @param event Pointer or mouse event carrying `clientX` and `clientY`.
 * @param dialog Native `HTMLDialogElement` to measure.
 * @returns True when the pointer is outside the dialog's bounding client rect.
 */
export function isPointerOutsideDialog(event: MouseEvent, dialog: HTMLDialogElement): boolean {
  const rect = dialog.getBoundingClientRect();
  return (
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom
  );
}

/**
 * Attach pointer-coordinate light-dismiss listeners to a `<dialog closedby="any">` element
 * when the browser lacks native `HTMLDialogElement.prototype.closedBy` support.
 * Requires both `pointerdown` and `pointerup` outside the bounding box so text-selection
 * drags ending on the backdrop do not dismiss the modal.
 * @param dialog Native `HTMLDialogElement` with `closedby="any"`.
 * @param options Optional `{ forceFallback }` override for unit tests.
 */
export function attachDialogLightDismissFallback(
  dialog: HTMLDialogElement | null,
  options: DialogFallbackOptions = {},
): void {
  if (!dialog) {
    return;
  }
  const hasNativeClosedBy =
    typeof HTMLDialogElement !== 'undefined' && 'closedBy' in HTMLDialogElement.prototype;
  if (hasNativeClosedBy && !options.forceFallback) {
    return;
  }

  let pressedOutside = false;
  dialog.addEventListener('pointerdown', (event: MouseEvent) => {
    pressedOutside = isPointerOutsideDialog(event, dialog);
  });
  dialog.addEventListener('pointerup', (event: MouseEvent) => {
    if (pressedOutside && isPointerOutsideDialog(event, dialog)) {
      dialog.close();
    }
    pressedOutside = false;
  });
}

/**
 * Open a native `<dialog>` element as a top-layer modal via `.showModal()`.
 * @param dialog Target `HTMLDialogElement`.
 */
export function openNativeModalDialog(dialog: HTMLDialogElement | null): void {
  if (!dialog || dialog.open) {
    return;
  }
  if (typeof dialog.showModal === 'function') {
    dialog.showModal();
  } else {
    dialog.setAttribute('open', '');
  }
}

/**
 * Read `dialog.returnValue` after `close` and reset it to `''` so stale values never leak
 * across subsequent `Esc` or backdrop dismissals.
 * @param dialog Target `HTMLDialogElement`.
 * @returns Consumed `returnValue` string prior to reset.
 */
export function resetDialogReturnValue(dialog: HTMLDialogElement | null): string {
  if (!dialog) {
    return '';
  }
  const consumed = dialog.returnValue ?? '';
  dialog.returnValue = '';
  return consumed;
}
