/* What the Review page, its dock and the Replace dialog share: which waiting preview is in front,
   and what was just decided. */
import type { Job } from '../../api/types';
import { jobs } from '../../stores/jobs.svelte';
import { ui } from '../../stores/ui.svelte';
import { jobTitle } from '../jobs/jobInfo';

export interface Decided { kind: 'replaced' | 'discarded'; title: string }

class Review {
  pickedId = $state<string | null>(null);
  decided = $state<Decided | null>(null);
  busy = $state(false);

  /** previews waiting for a decision, in the order they finished */
  waiting: Job[] = $derived([...jobs.review].reverse());
  /** the one in front: the picked one, else the first */
  current: Job | undefined = $derived(this.waiting.find((j) => j.id === this.pickedId) ?? this.waiting[0]);

  pick(id: string): void {
    this.pickedId = id;
    this.decided = null;
  }

  async decide(kind: 'replace' | 'discard'): Promise<void> {
    const j = this.current;
    if (!j || this.busy) return;
    this.busy = true;
    try {
      const title = jobTitle(j);
      const done = await jobs.act(j.id, kind);
      if (done) this.decided = { kind: kind === 'replace' ? 'replaced' : 'discarded', title };
    } finally {
      this.busy = false;
      ui.overlay = null;
    }
  }
}

export const review = new Review();
