/* Where jobs can run: the runner list and the live GPU meters. */
import { live } from '../api/events';
import { listGpus, listRunners, wakeRunner } from '../api/runners';
import { WHERE_CLOUD, type GpuMeter, type Runner } from '../api/types';

/** A runner is shown offline only after this many polls in a row said so: one dropped connection must not flip a healthy PC. */
const MISSES_TO_OFFLINE = 2;

/** How long a wake may take before we say it did not come up: the server's guess (or this) plus a grace. */
const DEFAULT_WAKE_S = 120;
const WAKE_GRACE_S = 30;

/** A wake we sent: `until` is when the runner should have answered by, `eta` the promised moment. */
export interface Wake { eta: number; until: number; failed: boolean; error?: string }

class Runners {
  /** wake packets sent, by runner url, until the runner answers or the window runs out */
  wakes = $state.raw<Record<string, Wake>>({});
  /** ticks once a second while any wake is running, so countdowns move */
  now = $state(Date.now());
  #tick: ReturnType<typeof setInterval> | undefined;

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

  /** Seconds left of the promised wake time (0 once it is due), or null when no wake is running. */
  wakeLeft(url: string): number | null {
    const w = this.wakes[url];
    return w && !w.failed ? Math.max(0, Math.ceil((w.eta - this.now) / 1000)) : null;
  }

  #setWake(url: string, w: Wake | null): void {
    const next = { ...this.wakes };
    if (w) next[url] = w; else delete next[url];
    this.wakes = next;
    const running = Object.values(next).some((x) => !x.failed);
    if (running && !this.#tick) {
      this.#tick = setInterval(() => this.#watch(), 1000);
    } else if (!running && this.#tick) {
      clearInterval(this.#tick);
      this.#tick = undefined;
    }
  }

  #watch(): void {
    this.now = Date.now();
    for (const [url, w] of Object.entries(this.wakes)) {
      if (!w.failed && this.now >= w.until) this.#setWake(url, { ...w, failed: true });
    }
  }

  /** Send Wake-on-LAN. The runner flips to online through the normal runner events; no answer in time = `failed`, press again to retry. */
  async wake(r: Runner): Promise<void> {
    if (this.wakes[r.url] && !this.wakes[r.url].failed) return;
    const t = Date.now();
    this.#setWake(r.url, { eta: t + (r.wake_eta_s || DEFAULT_WAKE_S) * 1000, until: t + ((r.wake_eta_s || DEFAULT_WAKE_S) + WAKE_GRACE_S) * 1000, failed: false });
    this.now = t;
    try {
      const res = await wakeRunner(r.name);
      const t2 = Date.now();
      const eta = res.eta_s || DEFAULT_WAKE_S;
      this.#setWake(r.url, { eta: t2 + eta * 1000, until: t2 + (eta + WAKE_GRACE_S) * 1000, failed: false });
      this.now = t2;
    } catch (e) {
      this.#setWake(r.url, { eta: 0, until: 0, failed: true, error: e instanceof Error ? e.message : 'Could not send the wake signal' });
    }
  }

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
    for (const r of out) if (r.online && this.wakes[r.url]) this.#setWake(r.url, null);
    for (const url of [...this.#misses.keys()]) if (!seen.has(url)) this.#misses.delete(url);
    return out;
  }

  async refresh(): Promise<void> {
    try { this.list = this.#settle(await listRunners()); this.loaded = true; } catch { /* the next poll retries */ }
  }
}

export const runners = new Runners();
