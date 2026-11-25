/* Where jobs can run: the runner list and the live GPU meters. */
import { live } from '../api/events';
import { listGpus, listRunners } from '../api/runners';
import { WHERE_CLOUD, type GpuMeter, type Runner } from '../api/types';

/** A runner is shown offline only after this many polls in a row said so: one dropped connection must not flip a healthy PC. */
const MISSES_TO_OFFLINE = 2;

class Runners {
  /** consecutive polls that found each runner (by url) offline */
  #misses = new Map<string, number>();

  list = $state.raw<Runner[]>([]);
  gpus = $state.raw<GpuMeter[]>([]);
  loaded = $state(false);
  error = $state<string | null>(null);

  /** online and not paused */
  usable: Runner[] = $derived(this.list.filter((r) => r.online && !r.paused));
  /** the runner a job goes to when none is pinned */
  fallback: Runner | undefined = $derived(this.usable.find((r) => r.prefer?.length) ?? this.usable[0] ?? this.list[0]);

  /** The runner a `where` value means (a url, or null for the fallback). `cloud` is not a runner. */
  resolve(where: string | null): Runner | undefined {
    if (where === WHERE_CLOUD) return undefined;
    return where ? this.list.find((r) => r.url === where) ?? this.fallback : this.fallback;
  }
  meter(url: string): GpuMeter | undefined { return this.gpus.find((g) => g.url === url); }

  start(): () => void {
    const a = live<Runner[]>('runners', {
      poll: (s) => listRunners(s),
      everyMs: 5000,
      onData: (l) => { this.list = this.#settle(l); this.loaded = true; this.error = null; },
      onError: (e) => { this.error = e instanceof Error ? e.message : 'Could not load runners'; this.loaded = true; },
    });
    const b = live<GpuMeter[]>('gpus', { poll: (s) => listGpus(s), everyMs: 3000, onData: (g) => { this.gpus = g; } });
    return () => { a(); b(); };
  }

  /** Keep a runner online until it has been missed twice in a row. A runner that was never seen online (or is asleep: it says so itself) shows as it is. */
  #settle(next: Runner[]): Runner[] {
    const before = new Map(this.list.map((r) => [r.url, r]));
    const seen = new Set<string>();
    const out = next.map((r) => {
      seen.add(r.url);
      if (r.online || r.asleep) { this.#misses.delete(r.url); return r; }
      const n = (this.#misses.get(r.url) ?? 0) + 1;
      this.#misses.set(r.url, n);
      const was = before.get(r.url);
      return n < MISSES_TO_OFFLINE && was?.online ? was : r;
    });
    for (const url of [...this.#misses.keys()]) if (!seen.has(url)) this.#misses.delete(url);
    return out;
  }

  async refresh(): Promise<void> {
    try { this.list = this.#settle(await listRunners()); this.loaded = true; } catch { /* the next poll retries */ }
  }
}

export const runners = new Runners();
