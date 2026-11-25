/* One shared clock for the "Updated 12 s ago" labels: a single 5-second timer that runs only while a label is on screen. */
import type { StatusState } from '../../ui/Status.svelte';

class Clock {
  now = $state(Date.now());
  #users = 0;
  #timer: number | null = null;

  /** call from an `$effect` and return the result, so the timer stops with the last label */
  use(): () => void {
    this.now = Date.now();
    if (this.#users++ === 0) this.#timer = window.setInterval(() => { this.now = Date.now(); }, 5000);
    return () => {
      if (--this.#users === 0 && this.#timer !== null) {
        window.clearInterval(this.#timer);
        this.#timer = null;
      }
    };
  }
}

export const clock = new Clock();

/** "Updated just now", "Updated 12 s ago", "Updated 3 min ago" */
export function agoText(at: number, now: number): string {
  const s = Math.max(0, Math.round((now - at) / 1000));
  return s < 5 ? 'Updated just now' : s < 60 ? `Updated ${s} s ago` : `Updated ${Math.floor(s / 60)} min ago`;
}

/** the status word beside "Updated …": glyph and word, never colour alone */
export function liveStatus(phase: 'busy' | 'error' | 'ready', at: number, now: number): { state: StatusState; word: string } {
  if (phase === 'error') return { state: 'offline', word: 'Not available' };
  if (phase === 'busy') return { state: 'busy', word: 'Updating' };
  return now - at > 60_000 ? { state: 'warn', word: 'Out of date' } : { state: 'ok', word: 'Live' };
}
