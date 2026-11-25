/* Plain helpers the Jobs and Review pages share: names, labels, numbers. No state in here. */
import { OPS, QUALITIES } from '../../choices';
import type { IconName } from '../../ui/icons';
import type { Job, JobState } from '../../api/types';
import { fmtDur, fmtMin, gbf, resName, usd } from '../../util/format';

/** The scene's name. The real job has no title yet (BACKEND NEED `scene_title`), so it falls back to the id. */
export function jobTitle(j: Job): string {
  return j.scene_title?.trim() || `Scene ${j.scene_id}`;
}

export function opName(j: Job): string {
  if (j.op) return OPS[j.op].name;
  return j.backend === 'upscale' ? OPS.upscale.name : j.backend === 'transcode' ? OPS.transcode.name : OPS.decensor.name;
}

export function opIcon(j: Job): IconName {
  return j.op ? OPS[j.op].icon : j.backend === 'upscale' ? 'expand' : j.backend === 'transcode' ? 'shrink' : 'wand';
}

/** "Decensor · High detail" */
export function whatLine(j: Job): string {
  return j.quality ? `${opName(j)} · ${QUALITIES[j.quality].name}` : opName(j);
}

/** Where it runs: the runner's name, or the rented machine. */
export function whereLine(j: Job): string {
  if (!j.cloud) return j.runner || 'This PC';
  return [j.provider, j.cloud_label].filter(Boolean).join(' · ') || 'Rented machine';
}

export const CLOUD_STEPS = [
  { id: 'boot', label: 'Machine ready' },
  { id: 'upload', label: 'Upload' },
  { id: 'run', label: 'Processing' },
  { id: 'download', label: 'Download' },
  { id: 'cleanup', label: 'Clean-up' },
] as const;

export function stepIndex(j: Job): number {
  const i = CLOUD_STEPS.findIndex((s) => s.id === j.cloud_phase);
  return i < 0 ? (j.progress > 0 ? 2 : 0) : i;
}

/** just started: nothing has run yet. `now` is ms and is passed in so the page can tick it. */
export function isFresh(j: Job, now: number): boolean {
  if (j.state !== 'queued' && j.state !== 'running') return false;
  if (j.progress > 0 || (j.frame ?? 0) > 0) return false;
  if (j.created) return now / 1000 - (j.created > 1e12 ? j.created / 1000 : j.created) < 30;
  return (j.elapsed ?? 0) < 30;
}

export interface StateWord { state: 'ok' | 'busy' | 'asleep' | 'offline' | 'warn'; word: string }

/** The glyph-plus-word status of a card. */
export function runWord(j: Job, fresh: boolean): StateWord {
  switch (j.state) {
    case 'queued': return { state: 'busy', word: 'Waiting its turn' };
    case 'replacing': return { state: 'busy', word: 'Replacing the original' };
    case 'discarding': return { state: 'busy', word: 'Discarding' };
    default:
      if (j.paused) return { state: 'warn', word: 'Paused' };
      if (j.cloud && (j.cloud_phase === 'boot' || j.cloud_phase === 'upload' || fresh)) return { state: 'busy', word: 'Renting a machine' };
      return { state: 'busy', word: 'Running' };
  }
}

export const pct = (j: Job): number => Math.round(Math.min(1, Math.max(0, j.progress || 0)) * 100);

/** the bar sweeps instead of showing a frozen 0% */
export function isIndeterminate(j: Job): boolean {
  if (j.paused || (j.state !== 'queued' && j.state !== 'running')) return false;
  if (j.indeterminate !== undefined) return j.indeterminate;
  return !(j.progress > 0 || (j.frame != null && j.total_frames));
}

export const fmtN = (n: number): string => Math.round(n).toLocaleString('en-US');
export const dash = '\u2013';

/** MB -> "8.4 GB" (whole numbers lose the decimal) */
export function gbOf(mb: number): string {
  const g = mb / 1024;
  return Number.isInteger(g) ? String(g) : g.toFixed(1);
}

export function etaWord(j: Job): string {
  if (j.paused) return 'Paused';
  if (j.eta != null) return `${fmtDur(j.eta)} left`;
  if (j.state === 'queued') return 'Waiting';
  return j.message || dash;
}

export function costLine(j: Job): { spent: string; of: string | null; cap: string | null; rented: string | null } {
  return {
    spent: usd(j.cloud_usd ?? 0),
    of: j.cloud_est_usd != null ? usd(j.cloud_est_usd) : null,
    cap: j.cloud_cap != null ? usd(j.cloud_cap) : null,
    rented: j.elapsed != null ? fmtMin(Math.max(1, j.elapsed / 60)) : null,
  };
}

export const FINISHED_WORD: Partial<Record<JobState, { icon: IconName; tone: string; word: string; sub: string }>> = {
  replaced: { icon: 'checkCircle', tone: 'ok', word: 'Replaced', sub: 'The original now uses the cleaned file.' },
  discarded: { icon: 'xCircle', tone: 'off', word: 'Discarded', sub: 'The original was not changed.' },
  cancelled: { icon: 'xCircle', tone: 'off', word: 'Cancelled', sub: 'Stopped before it finished.' },
  error: { icon: 'alert', tone: 'bad', word: 'Failed', sub: '' },
};

export interface Fact { k: string; v: string; sub?: string }

/** The facts of a finished preview. Rows without data are left out. */
export function reviewFacts(j: Job): Fact[] {
  const r = j.result;
  const rows: Fact[] = [{ k: 'Operation', v: opName(j), sub: j.quality ? QUALITIES[j.quality].name : undefined }];
  if (r?.from_height != null && r.to_height != null) {
    const bytes = r.from_bytes != null && r.to_bytes != null ? `${gbf(r.from_bytes / 1024 ** 3)} \u2192 ${gbf(r.to_bytes / 1024 ** 3)}` : undefined;
    rows.push({ k: 'Size', v: r.from_height === r.to_height ? resName(r.to_height) : `${resName(r.from_height)} \u2192 ${resName(r.to_height)}`, sub: bytes });
  }
  const took = j.elapsed != null ? `took ${fmtMin(j.elapsed / 60)}` : undefined;
  rows.push({ k: 'Machine', v: j.cloud ? j.cloud_label || 'Rented machine' : j.runner || 'This PC', sub: [j.cloud ? j.provider : null, took].filter(Boolean).join(' \u00b7 ') || undefined });
  if (j.cloud && j.cloud_usd != null) rows.push({ k: 'Cost', v: usd(j.cloud_usd), sub: 'compute, transfer and storage' });
  return rows;
}

/** One-line summary of a finished preview, for the dock. */
export function reviewLine(j: Job): string {
  const r = j.result;
  const parts: string[] = [opName(j)];
  if (r?.from_height != null && r.to_height != null && r.from_height !== r.to_height) parts.push(`${resName(r.from_height)} \u2192 ${resName(r.to_height)}`);
  if (r?.from_bytes != null && r.to_bytes != null) parts.push(`${gbf(r.from_bytes / 1024 ** 3).replace(' GB', '')} \u2192 ${gbf(r.to_bytes / 1024 ** 3)}`);
  if (j.cloud && j.cloud_usd != null) parts.push(usd(j.cloud_usd));
  return parts.join(' \u00b7 ');
}

/* ---------- log ---------- */
const RE_NUM = /\d[\d.,:]*/g;
const RE_BAR = /\|[#\s.\u2581-\u2589]*\|/g;
/** Lines that differ only by numbers (or a progress bar) share a key, so they merge into one. */
export const logNorm = (s: string): string => s.replace(RE_BAR, '|').replace(RE_NUM, '#');

/* ---------- placeholder art ---------- */
/** the mockup's seeded "mosaic" heights */
export const BLOCKS: readonly number[] = (() => {
  const o: number[] = [];
  let x = 7;
  for (let i = 0; i < 72; i++) { x = (x * 1103515245 + 12345) & 0x7fffffff; o.push(14 + (x % 30)); }
  return o;
})();
