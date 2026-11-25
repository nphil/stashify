/* What the tray's choice would do: the time and price on a runner you own, the rented machine the cloud store picked,
   and the one reason Run is off (if any). Everything the tray and its popovers show is read from here. */
import type { IconName } from '../../ui/icons';
import type { OpId, QualityId, Quote, Runner, SceneInfo } from '../../api/types';
import { OPS, QUALITIES, SIZES, baseSpeed, homeQuote } from '../../choices';
import { knobsFor } from '../../run';
import { cloud, type Pick } from '../../stores/cloud.svelte';
import { options } from '../../stores/options.svelte';
import { runners } from '../../stores/runners.svelte';
import { selection } from '../../stores/selection.svelte';

export interface Est { ok: boolean; why: string | null; min: number }

/** Can this runner do this operation, and is it reachable? A runner that is asleep but can be woken counts, with the wake-up added. */
export function homeFor(info: SceneInfo, op: OpId, q: QualityId, r: Runner | undefined): Est {
  const h = homeQuote(info, op, q, r);
  if (!r || !h.ok) return h;
  const key = op === 'decensor_up' ? 'decensor' : op;
  if (r.paused) return { ...h, ok: false, why: 'Paused' };
  if (r.ops.length && !r.ops.includes(key)) return { ...h, ok: false, why: `Not for ${OPS[op].name}` };
  if (!r.online) {
    if (r.asleep && r.can_wake) return { ...h, min: h.min + (r.wake_eta_s ?? 0) / 60 };
    return { ...h, ok: false, why: r.asleep ? 'Asleep' : 'Offline' };
  }
  return h;
}

/** The server prices only the chosen quality. For the other rows, scale the run part of the same machine's quote by how much slower
 *  that quality is. An estimate (the rows say "~"), not a quote; null when the quote does not say how long the run itself takes. */
export function cloudScaled(q: Quote, op: OpId, from: QualityId, to: QualityId): { min: number; usd: number } | null {
  if (from === to) return { min: q.minutes, usd: q.total };
  if (q.runMin === undefined || q.minutes <= 0) return null;
  const min = q.minutes + q.runMin * (baseSpeed(op, from) / baseSpeed(op, to) - 1);
  return { min, usd: q.total + (q.compute + q.storage) * (min / q.minutes - 1) };
}

/** The word shown on the Quality token. */
export function qualityLabel(op: OpId): string {
  if (op === 'transcode') return SIZES[options.size];
  return op === 'upscale' ? 'Standard' : QUALITIES[options.q].name;
}

export function runnerIcon(r: Runner | undefined): IconName {
  const k = `${r?.kind ?? ''} ${r?.name ?? ''}`.toLowerCase();
  return k.includes('nas') || k.includes('server') ? 'server' : 'monitor';
}

class Plan {
  /** the runner a home run goes to (null while none is set up) */
  runner: Runner | undefined = $derived(runners.resolve(options.where));
  home: Est = $derived(homeFor(selection.info, options.op, options.q, this.runner));
  /** the first runner, shown as the "run it at home instead" alternative */
  alt: Est = $derived(homeFor(selection.info, options.op, options.q, runners.fallback));
  canCloud: boolean = $derived(OPS[options.op].cloud);
  /** a price is on its way: show skeletons, never block */
  waiting: boolean = $derived(this.canCloud && (cloud.state === 'idle' || cloud.state === 'loading'));
  pick: Pick | null = $derived(this.canCloud && !this.waiting ? cloud.picked : null);
  quote: Quote | null = $derived(this.pick?.quote ?? null);
  /** which Advanced controls apply to this choice */
  knobs: { jasna: boolean; detect: boolean } = $derived(knobsFor(options.op, options.where));
  /** how many of those differ from their defaults */
  knobCount: number = $derived(
    (this.knobs.jasna ? Number(options.denoise !== 'off') + Number(options.rtx !== 'off') + Number(options.rtxFx) + Number(options.preview) : 0) +
      (this.knobs.detect ? Number(options.detect !== 'fast') : 0),
  );
  /** changes whenever the thing being priced changes */
  jobKey: string = $derived(`${options.op}|${options.q}|${selection.ids.join(',')}`);

  /** why this choice cannot run (a problem the person can fix), or null */
  why: string | null = $derived.by(() => {
    if (options.cloud) {
      if (!this.canCloud) return 'Transcode runs at home only';
      if (this.waiting) return null;
      if (cloud.blocked) return cloud.blocked;
      if (cloud.state === 'error') return cloud.error ?? 'Could not get a price';
      if (cloud.state === 'empty') return 'No machine fits this job';
      return null;
    }
    if (this.home.ok) return null;
    if (!runners.loaded) return null;
    return runners.usable.length || this.runner?.asleep ? this.home.why : 'No runner is online';
  });
  /** the reason Run is off, including "still looking" */
  blocker: string | null = $derived(this.why ?? (options.cloud && (this.waiting || !this.pick) ? 'Getting a price' : !options.cloud && !runners.loaded ? 'Looking for runners' : null));

  whereLabel: string = $derived.by(() => {
    if (options.cloud) {
      const o = this.pick?.offer;
      return o ? `${o.gpu} · ${o.location.split(',')[0]}` : 'Cloud GPU';
    }
    return this.runner?.name ?? 'No runner';
  });
  whereIcon: IconName = $derived(options.cloud ? 'cloud' : runnerIcon(this.runner));
}

export const plan = new Plan();
