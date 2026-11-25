/* Which floating layer is open. One place, so Escape, the scrim and the mock `?s=` states all agree. */
import { SvelteSet } from 'svelte/reactivity';

export type Overlay = 'op' | 'quality' | 'where' | 'settings' | 'picker' | 'confirm' | 'replace' | null;

class Ui {
  /** the tray popovers, the cloud picker and the two dialogs (only one at a time) */
  overlay = $state<Overlay>(null);
  /** the full-screen rental browser (opened from the picker; the Rent page shows it inline instead) */
  browse = $state(false);
  /** filter rail (wide) or filter sheet (compact) inside the browser */
  filters = $state(false);
  /** open chip menu inside the browser: 'sort' | 'model' | 'count' | 'region' | 'rental' | 'provider' */
  menu = $state<string | null>(null);
  /** offer id whose cost breakdown is pinned open (a hover or tap sets and clears it) */
  receipt = $state<string | null>(null);
  /** offer ids whose detail grid is open in the browser */
  expanded = new SvelteSet<string>();
  /** the "Advanced" disclosure of the quality popover */
  advanced = $state(false);

  toggle(o: Exclude<Overlay, null>): void {
    this.overlay = this.overlay === o ? null : o;
    this.menu = null;
  }

  /** Escape or a click on the scrim: close the topmost layer. Returns whether anything closed. */
  back(): boolean {
    if (this.menu) { this.menu = null; return true; }
    if (this.filters) { this.filters = false; return true; }
    if (this.receipt) { this.receipt = null; return true; }
    if (this.overlay === 'confirm' || this.overlay === 'replace') { this.overlay = null; return true; }
    if (this.browse) { this.browse = false; return true; }
    if (this.overlay) { this.overlay = null; return true; }
    return false;
  }

  reset(): void {
    this.overlay = null;
    this.browse = false;
    this.filters = false;
    this.menu = null;
    this.receipt = null;
    this.expanded.clear();
    this.advanced = false;
  }
}

export const ui = new Ui();
