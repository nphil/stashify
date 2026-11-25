/* Starting a run: one job per selected scene, built from what the tray is set to.
   The rules (which controls reach the server, when Jasna is forced) come from the old dashboard. */
import { ApiError } from './api/client';
import { submitJob } from './api/submit';
import { WHERE_CLOUD, type Job, type OpId, type QualityId, type Runner, type Scene, type SubmitBody } from './api/types';
import { engineOf, sceneInfo } from './choices';
import { jobs } from './stores/jobs.svelte';
import { options } from './stores/options.svelte';
import { runners } from './stores/runners.svelte';
import { selection } from './stores/selection.svelte';
import { toast } from './stores/toast.svelte';
import { ui } from './stores/ui.svelte';
import { fmtMin, r2 } from './util/format';

export type RunResult =
  | { ok: true; jobs: Job[] }
  | { ok: false; code: string; message: string; detail?: unknown };

/** What the paid confirm dialog hands over: the machine, the total the person agreed to, and the expected length. */
export interface CloudChoice { offerId: string; /** the provider's own id; a pinned machine is looked up on it */ providerId?: string; confirmUsd: number; minutes: number }

const isJasna = (r: Runner, op: OpId): boolean => engineOf(r, op)?.toLowerCase() === 'jasna';

/** Which Advanced controls can have an effect: Jasna ones only when a Jasna runner can take the job, the Lada detection model otherwise. None on the cloud. */
export function knobsFor(op: OpId, where: string | null): { jasna: boolean; detect: boolean } {
  if ((op !== 'decensor' && op !== 'decensor_up') || where === WHERE_CLOUD) return { jasna: false, detect: false };
  const r = runners.resolve(where);
  const own = !!r && isJasna(r, op);
  return { jasna: where === null ? runners.usable.some((x) => isJasna(x, op)) : own, detect: !own };
}

const CLOUD_PRESET: Record<QualityId, string> = { fast: 'close-ups', balanced: 'detail', high: 'detail-close-ups', max: 'detail-close-ups' };

/** The body for one scene on a runner you own. */
function homeBody(scene: Scene): SubmitBody {
  const { op, q, where } = options;
  if (op === 'upscale') return { scene_id: scene.id, backend: 'upscale', ...(where ? { runner: where } : {}) };
  if (op === 'transcode') {
    return { scene_id: scene.id, backend: 'transcode', ...(options.size !== 'keep' ? { transcode_height: options.size } : {}), ...(where ? { runner: where } : {}) };
  }
  const k = knobsFor(op, where);
  const target = runners.resolve(where);
  const body: SubmitBody = { scene_id: scene.id, backend: 'decensor', detection_model: k.detect && options.detect === 'accurate' ? 'v4-accurate' : 'v4-fast' };
  if (op === 'decensor_up') body.post_upscale = true;
  if (where) body.runner = where;

  /* Quality is Jasna's restoration model. Only a runner known to be Jasna gets it; a Lada runner can only do Fast. */
  if (target && isJasna(target, op)) {
    body.restoration = q === 'fast' ? 'basicvsrpp' : 'ltx';
    if (q === 'balanced') body.ltx_auto_split = 12;
    body.engine = 'jasna';
  }
  if (k.jasna) {
    let used = false;
    if (options.denoise !== 'off') { body.denoise = options.denoise; used = true; }
    if (options.rtx !== 'off') {
      body.secondary = 'rtx-super-res';
      body.rtx_quality = options.rtx === '4' ? 'ultra' : 'high';
      body.rtx_scale = options.rtx;
      if (options.rtxFx) { body.rtx_denoise = 'medium'; body.rtx_deblur = 'medium'; }
      used = true;
    }
    if (options.preview) { body.preview = true; used = true; }
    if (used) body.engine = 'jasna'; /* so the job cannot be routed to a runner that ignores these */
  }
  return body;
}

/** The body for one scene on a rented machine. `confirm_usd` is this scene's share of what the person agreed to. */
function cloudBody(scene: Scene, c: CloudChoice, share: number): SubmitBody {
  const { op, q } = options;
  const body: SubmitBody = { scene_id: scene.id, backend: op === 'upscale' ? 'upscale' : 'decensor', cloud: true, offer_id: c.offerId, ...(c.providerId ? { provider: c.providerId } : {}), confirm_usd: r2(c.confirmUsd * share) };
  if (op === 'decensor_up') body.post_upscale = true;
  if (op !== 'upscale') body.cloud_preset = CLOUD_PRESET[q];
  return body;
}

/** The server sends the new quote with a `price_changed` refusal. */
function newQuote(body: unknown): unknown {
  return body && typeof body === 'object' && 'quote' in body ? body.quote : body;
}

/** Submit one job per selected scene. Home run: no argument. Paid run: the machine and the agreed price.
 *  Jobs that were accepted stay accepted: they leave the selection, the rest stay selected so the person can retry. */
export async function startRun(cloud?: CloudChoice): Promise<RunResult> {
  const scenes = selection.list;
  if (!scenes.length) return { ok: false, code: 'empty', message: 'Pick a scene first' };
  if (options.cloud && !cloud) return { ok: false, code: 'confirm_required', message: 'Confirm the price first' };
  if (cloud && options.op === 'transcode') return { ok: false, code: 'not_for_cloud', message: 'Transcode runs at home only' };

  const total = sceneInfo(scenes).eff;
  const made: Job[] = [];
  let failure: RunResult | null = null;
  for (const scene of scenes) {
    try {
      const share = total > 0 ? sceneInfo([scene]).eff / total : 1 / scenes.length;
      const job = await submitJob(cloud ? cloudBody(scene, cloud, share) : homeBody(scene));
      made.push(job);
      jobs.add(job);
      selection.remove(scene.id);
    } catch (e) {
      failure = e instanceof ApiError
        ? { ok: false, code: e.code ?? `http_${e.status}`, message: e.message, detail: e.code === 'price_changed' ? newQuote(e.body) : e.body }
        : { ok: false, code: 'network', message: e instanceof Error ? e.message : 'Could not reach the server' };
      break;
    }
  }
  if (failure) return failure;

  ui.overlay = null;
  ui.browse = false;
  if (cloud) toast.show(`Renting a machine · about ${fmtMin(cloud.minutes)}`);
  else {
    const name = made[0]?.runner ?? runners.resolve(options.where)?.name;
    toast.show(name ? `Queued on ${name}` : 'Queued');
  }
  return { ok: true, jobs: made };
}
