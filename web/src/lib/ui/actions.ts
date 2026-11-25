/* Svelte actions shared by floating layers. */

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function visibleFocusables(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement);
}

/** A floating layer (popover, sheet, picker): move focus in when it opens, keep Tab inside, give focus back on close.
 *  Escape is handled once, globally, by `ui.back()`. Pass `{ trap: false }` for a layer that must not hold focus. */
export function focusLayer(node: HTMLElement, opts: { trap?: boolean; initial?: string } = {}) {
  const back = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const first = () =>
    (opts.initial ? node.querySelector<HTMLElement>(opts.initial) : null) ??
    node.querySelector<HTMLElement>('[autofocus],[aria-checked="true"]') ??
    visibleFocusables(node)[0] ??
    node;
  if (!node.hasAttribute('tabindex')) node.tabIndex = -1;
  queueMicrotask(() => first().focus({ preventScroll: true }));

  function onkey(e: KeyboardEvent): void {
    if (e.key !== 'Tab' || opts.trap === false) return;
    const items = visibleFocusables(node);
    if (!items.length) { e.preventDefault(); return; }
    const i = items.indexOf(document.activeElement as HTMLElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); items[items.length - 1].focus(); }
    else if (!e.shiftKey && (i === items.length - 1 || i < 0)) { e.preventDefault(); items[0].focus(); }
  }
  node.addEventListener('keydown', onkey);
  return {
    destroy() {
      node.removeEventListener('keydown', onkey);
      if (back && back.isConnected) back.focus({ preventScroll: true });
    },
  };
}

/** Put on the tray / dock element: publishes its height as `--tray-h` on the shell so the page scrolls clear of it. */
export function trayHeight(node: HTMLElement) {
  const shell = node.closest<HTMLElement>('.a-shell');
  const set = () => shell?.style.setProperty('--tray-h', `${Math.ceil(node.getBoundingClientRect().height)}px`);
  const ro = new ResizeObserver(set);
  ro.observe(node);
  set();
  return {
    destroy() {
      ro.disconnect();
      shell?.style.setProperty('--tray-h', '0px');
    },
  };
}
