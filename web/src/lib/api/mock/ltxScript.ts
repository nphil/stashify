/* A scripted decensor + upscale job on a home PC with the LTX model, walking through every phase the runner
   reports. `?job=ltx` plays it in about two minutes (one demo second stands for ~90 real seconds, so the time
   left reads like a long job); `?job=ltx&at=<scene>` holds one moment for review: see SCENES. */
import type { Job } from '../types';

/** demo seconds -> the seconds a real run would show */
const SCALE = 90;

type Fields = Pick<Job, 'stage' | 'phase' | 'step' | 'steps' | 'detail' | 'pass_no' | 'passes' | 'estimate' | 'indeterminate' | 'progress' | 'eta' | 'fps' | 'idle_s'>;

interface Seg { id: string; secs: number; from: number; to: number; fields: Partial<Fields> }

const LTX_DETAIL = ['prepare', 'denoise', 'refine', 'encode'];
const base = { passes: 2, steps: null, step: null, detail: null, estimate: false, indeterminate: false, fps: null, idle_s: 1 } as const;
const ltx = (n: number): Seg => ({
  id: `ltx${n}`, secs: 14, from: 0.12 + 0.125 * (n - 1), to: 0.12 + 0.125 * n,
  fields: { ...base, stage: 'decensor', phase: 'Restoring in high detail', pass_no: 1, step: n, steps: 4, detail: LTX_DETAIL[n - 1], estimate: true, fps: 79.5 },
});

const SEGS: readonly Seg[] = [
  { id: 'copy', secs: 8, from: 0, to: 0.06, fields: { ...base, stage: 'copy', phase: 'Copying video to the PC', pass_no: 1, detail: '3.2 of 8.1 GB' } },
  { id: 'scan', secs: 8, from: 0.06, to: 0.12, fields: { ...base, stage: 'scan', phase: 'Scanning for mosaics', pass_no: 1, detail: '42%' } },
  { id: 'load', secs: 6, from: 0.12, to: 0.12, fields: { ...base, stage: 'decensor', phase: 'Loading models', pass_no: 1, indeterminate: true } },
  ltx(1), ltx(2), ltx(3), ltx(4),
  { id: 'upscale-load', secs: 5, from: 0.62, to: 0.62, fields: { ...base, stage: 'upscale', phase: 'Loading the upscaler', pass_no: 2, indeterminate: true, estimate: true } },
  { id: 'upscale', secs: 30, from: 0.62, to: 0.97, fields: { ...base, stage: 'upscale', phase: 'Upscaling', pass_no: 2, fps: 31.2 } },
  { id: 'saving', secs: 6, from: 0.97, to: 0.97, fields: { ...base, stage: 'decensor', phase: 'Saving result', pass_no: 2, indeterminate: false } },
];
const TOTAL = SEGS.reduce((n, s) => n + s.secs, 0);
/** moments with no time left to give: a copy, a scan, a load and the save */
const NO_ETA = new Set(['copy', 'scan', 'load', 'saving']);

export const SCENES: readonly string[] = [...SEGS.map((s) => s.id), 'stalled', 'error'];

function at(seg: Seg, into: number, before: number): Partial<Fields> {
  const p = seg.secs > 0 ? Math.min(1, into / seg.secs) : 1;
  const left = TOTAL - before - into;
  return { ...seg.fields, progress: seg.from + (seg.to - seg.from) * p, eta: NO_ETA.has(seg.id) ? null : Math.round(left * SCALE) };
}

/** the job's fields `t` demo seconds in, or null once it has finished */
export function ltxFrame(t: number): (Partial<Fields> & { elapsed: number }) | null {
  if (t >= TOTAL) return null;
  let before = 0;
  for (const seg of SEGS) {
    if (t < before + seg.secs) return { ...at(seg, t - before, before), elapsed: Math.round(t * SCALE) + 40 };
    before += seg.secs;
  }
  return null;
}

/** one held moment, by name */
export function ltxScene(name: string): (Partial<Fields> & { elapsed: number }) | null {
  const id = name === 'stalled' ? 'ltx3' : name;
  let before = 0;
  for (const seg of SEGS) {
    if (seg.id === id) {
      const f = { ...at(seg, seg.secs / 2, before), elapsed: Math.round((before + seg.secs / 2) * SCALE) + 40 };
      return name === 'stalled' ? { ...f, idle_s: 175 } : f;
    }
    before += seg.secs;
  }
  return null;
}
