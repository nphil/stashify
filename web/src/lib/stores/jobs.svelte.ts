/* The job list, kept fresh by the event stream (or a 1.5 s poll), plus the actions on a job. */
import { SvelteSet } from 'svelte/reactivity';
import { ApiError } from '../api/client';
import { live } from '../api/events';
import { adaptJob } from '../api/cloudAdapter';
import { jobAction, listJobs, type JobAction } from '../api/jobs';
import { ACTIVE_STATES, type Job } from '../api/types';
import { loadJson, saveJson } from '../util/persist';
import { toast } from './toast.svelte';

const KEY = 'stashify.dismissed';

class Jobs {
  /** newest first */
  all = $state.raw<Job[]>([]);
  loaded = $state(false);
  error = $state<string | null>(null);
  /** finished jobs the person tidied away (this browser only) */
  dismissed = new SvelteSet<string>(loadJson<string[]>(KEY, []));

  visible: Job[] = $derived(this.all.filter((j) => !this.dismissed.has(j.id)));
  active: Job[] = $derived(this.visible.filter((j) => ACTIVE_STATES.includes(j.state)));
  running: Job[] = $derived(this.active.filter((j) => j.state !== 'queued'));
  waiting: Job[] = $derived(this.active.filter((j) => j.state === 'queued'));
  review: Job[] = $derived(this.visible.filter((j) => j.state === 'review_ready'));
  /** done, failed or cancelled: safe to clear */
  finished: Job[] = $derived(this.visible.filter((j) => !ACTIVE_STATES.includes(j.state) && j.state !== 'review_ready'));
  /** jobs running right now (the tab badge); waiting ones are not counted */
  get activeCount(): number { return this.running.length; }
  get reviewCount(): number { return this.review.length; }

  constructor() {
    $effect.root(() => {
      $effect(() => { saveJson(KEY, [...this.dismissed].slice(-300)); });
    });
  }

  /** Start listening. Returns the stop function. */
  start(): () => void {
    return live<Job[]>('jobs', {
      poll: (signal) => listJobs(signal),
      everyMs: 1500,
      onData: (list) => this.set(list),
      onError: (e) => { this.error = e instanceof Error ? e.message : 'Could not load jobs'; this.loaded = true; },
    });
  }

  /** accepts the server order (oldest first) */
  set(list: Job[]): void {
    const rx = Date.now();
    this.all = list.map((j) => ({ ...adaptJob(j), rx })).reverse();
    this.loaded = true;
    this.error = null;
  }

  byId(id: string): Job | undefined { return this.all.find((j) => j.id === id); }

  /** a job just created by a submit: show it at once */
  add(job: Job): void {
    this.all = [{ ...adaptJob(job), rx: Date.now() }, ...this.all.filter((j) => j.id !== job.id)];
  }

  async act(id: string, action: JobAction): Promise<Job | null> {
    if (action === 'pause' || action === 'resume') this.#patch(id, { paused: action === 'pause' });
    try {
      const j = await jobAction(id, action);
      if (j && j.id) this.#patch(id, j);
      return j;
    } catch (e) {
      toast.show(e instanceof ApiError && e.status === 409 ? 'That job already changed' : e instanceof Error ? e.message : 'Could not do that');
      void this.refresh();
      return null;
    }
  }

  async refresh(): Promise<void> {
    try { this.set(await listJobs()); } catch { /* the next poll retries */ }
  }

  dismiss(id: string): void { this.dismissed.add(id); }
  clearFinished(): void { for (const j of this.finished) this.dismissed.add(j.id); }

  #patch(id: string, p: Partial<Job>): void {
    this.all = this.all.map((j) => (j.id === id ? { ...j, ...adaptJob({ ...j, ...p }) } : j));
  }
}

export const jobs = new Jobs();
