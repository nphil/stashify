/* Radio-group keys for a list of `role="radio"` buttons: arrows move focus and choose, Home/End jump, disabled rows are skipped.
   Only one row is a Tab stop (the markup sets `tabindex`), so Tab leaves the list in one press. */
export function roving(node: HTMLElement) {
  function onkey(e: KeyboardEvent): void {
    const items = [...node.querySelectorAll<HTMLElement>('[role="radio"]:not([aria-disabled="true"])')];
    const i = items.findIndex((el) => el === document.activeElement);
    if (i < 0 || !items.length) return;
    let n = -1;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') n = (i + 1) % items.length;
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') n = (i - 1 + items.length) % items.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = items.length - 1;
    if (n < 0) return;
    e.preventDefault();
    items[n].focus();
    items[n].click();
  }
  node.addEventListener('keydown', onkey);
  return { destroy: () => node.removeEventListener('keydown', onkey) };
}
