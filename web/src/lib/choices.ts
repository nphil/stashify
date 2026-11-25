/* The options a person picks (what / quality / where) and the rough maths behind "Takes about ...".
   Speeds are multiples of real time on an RTX 3080 (= 1.0). The server's own numbers win when it sends them. */
import type { OpId, QualityId, Runner, Scene, SceneInfo, SizeId } from './api/types';
import type { IconName } from './ui/icons';

export interface OpDef { id: OpId; name: string; hint: string; icon: IconName; cloud: boolean }
export const OPS: Record<OpId, OpDef> = {
  decensor: { id: 'decensor', name: 'Decensor', hint: 'Restore the pixelated areas', icon: 'wand', cloud: true },
  decensor_up: { id: 'decensor_up', name: 'Decensor + upscale', hint: 'Restore, then 2× bigger', icon: 'sparkles', cloud: true },
  upscale: { id: 'upscale', name: 'Upscale', hint: '2× bigger and sharper', icon: 'expand', cloud: true },
  transcode: { id: 'transcode', name: 'Transcode', hint: 'Smaller file, same look', icon: 'shrink', cloud: false },
};
export const OP_ORDER: readonly OpId[] = ['decensor', 'decensor_up', 'upscale', 'transcode'];

export interface QualityDef { id: QualityId; name: string; hint: string; speed: number; models: number }
export const QUALITIES: Record<QualityId, QualityDef> = {
  fast: { id: 'fast', name: 'Fast', hint: 'Quickest. Fine for most scenes.', speed: 1.7, models: 2.5 },
  balanced: { id: 'balanced', name: 'Balanced', hint: 'Extra detail on big areas only.', speed: 1.0, models: 12 },
  high: { id: 'high', name: 'High detail', hint: 'Most natural result. Much slower.', speed: 0.38, models: 12 },
  max: { id: 'max', name: 'Max detail', hint: 'High detail plus sharper close-ups.', speed: 0.3, models: 14 },
};
export const QUALITY_ORDER: readonly QualityId[] = ['fast', 'balanced', 'high', 'max'];

export const SIZES: Record<SizeId, string> = { keep: 'Keep size', '1080': '1080p', '720': '720p' };
export const SIZE_ORDER: readonly SizeId[] = ['keep', '1080', '720'];

/** Output size relative to the input, by operation (drives download time and cost). */
export const OUT_F: Record<OpId, number> = { decensor: 1.1, decensor_up: 2.2, upscale: 2.2, transcode: 0.45 };
/** Mb/s on the owner's home line (used for rental transfer-time estimates). */
export const NET = { ourUp: 400, ourDown: 900 };

/** How much slower a taller video is: 720p ~0.55, 1080p 1, 4K ~3.4 (straight lines between those points). */
const RES_POINTS: readonly [number, number][] = [[360, 0.3], [720, 0.55], [1080, 1], [1440, 1.9], [2160, 3.4]];
export function resFactor(h: number): number {
  if (h <= RES_POINTS[0][0]) return RES_POINTS[0][1];
  for (let i = 1; i < RES_POINTS.length; i++) {
    const [h1, f1] = RES_POINTS[i];
    if (h <= h1) { const [h0, f0] = RES_POINTS[i - 1]; return f0 + ((f1 - f0) * (h - h0)) / (h1 - h0); }
  }
  return RES_POINTS[RES_POINTS.length - 1][1] * (h / 2160);
}

export function sceneInfo(scenes: readonly Scene[]): SceneInfo {
  let dur = 0, eff = 0, gb = 0;
  for (const s of scenes) {
    const f = s.files[0];
    if (!f) continue;
    dur += f.duration;
    eff += f.duration * resFactor(f.height);
    gb += f.size / 1e9;
  }
  return { n: scenes.length, dur, eff, gb };
}

/** x real time on the reference GPU. */
export function baseSpeed(op: OpId, q: QualityId): number {
  if (op === 'upscale') return 2.7;
  if (op === 'transcode') return 6.0;
  const b = QUALITIES[q].speed;
  return op === 'decensor_up' ? b * 0.75 : b;
}
/** Model download size in GB for a rented machine. */
export const modelsGB = (op: OpId, q: QualityId): number => (op === 'upscale' ? 1.5 : QUALITIES[q].models + (op === 'decensor_up' ? 1.5 : 0));

/** What a rented machine must be able to do for this job. */
export function jobReq(op: OpId, q: QualityId, info: SceneInfo): { vram: number; diskGB: number; cuda: number } {
  const qq: QualityId = op === 'transcode' ? 'fast' : q;
  const vram = op === 'upscale' ? 6 : { fast: 6, balanced: 8, high: 10, max: 12 }[qq];
  return { vram, diskGB: Math.max(40, Math.ceil(info.gb * 3 + modelsGB(op, qq) + 10)), cuda: 12.4 };
}

/** The engine a runner uses for an op (`lada`, `jasna`, ...), if it says. */
export const engineOf = (r: Runner, op: OpId): string | undefined => r.engines[op === 'decensor_up' ? 'decensor' : op];

/** Qualities a runner can do. The server may say; otherwise the Lada engine only does Fast. */
export function qualitiesOf(r: Runner, op: OpId): readonly QualityId[] {
  if (r.qualities) return r.qualities;
  return engineOf(r, op)?.toLowerCase() === 'lada' ? ['fast'] : QUALITY_ORDER;
}

export interface HomeQuote { ok: boolean; why: string | null; min: number }
/** Minutes on a runner you own, and whether it can do the job. */
export function homeQuote(info: SceneInfo, op: OpId, q: QualityId, r: Runner | undefined): HomeQuote {
  if (!r) return { ok: false, why: 'No runner is set up', min: 0 };
  let why: string | null = null;
  if ((op === 'decensor' || op === 'decensor_up') && !qualitiesOf(r, op).includes(q)) why = 'Fast quality only';
  const g = r.speed ?? 1;
  const speed = op === 'transcode' ? baseSpeed(op, 'fast') * Math.pow(g, 0.3) : baseSpeed(op, q) * g;
  return { ok: !why, why, min: info.eff / speed / 60 };
}
