/* Mock jobs: the mockup's seed (a home run, a rented run, two waiting, one to review) and a small
   simulation. Progress, phases and cost follow the wall clock, so the pages visibly move. */
import type { Job, JobLog, JobLogLine, JobSegment, OpId, QualityId, SubmitBody } from '../types';
import { fail, route } from './router';
import { BASE_SCENES, sceneTitle } from './scenes';
import { ltxFrame, ltxScene } from './ltxScript';
import { BUDGET } from './shared';

const GB = 1024 ** 3;
const FPS = 24;
const HOME = 'This PC';
const CLOUD_LABEL = 'RTX 5090 · Frankfurt';
const CLOUD_PROVIDER = 'Vast.ai';
/** seconds a new job waits (queued) before it may start */
const QUEUE_MS = 1500;
/** how long a replace or discard takes to settle */
const SETTLE_MS = 1600;
/** the mockup's restored moments on the 35:20 line: [start %, width %] */
const MOMENTS: readonly (readonly [number, number])[] = [[6, 4], [13, 3], [22, 5], [34, 2], [41, 6], [57, 3], [66, 4], [78, 3], [90, 4]];
/** unique-looking log lines (the progress line between them merges into one, as in the real log) */
const EVENTS: readonly string[] = [
  'Loading detection model',
  'Detected 3 regions in segment 2',
  'Restoring segment 3 (96 frames)',
  'Writing segment 3 to the output',
  'Detected 5 regions in segment 4',
  'Restoring segment 4 (120 frames)',
];

interface Sim {
  job: Job;
  cloud: boolean;
  /** ms the job (or rental) began */
  t0: number;
  /** progress at `ref`, and the ms it counts from (null while paused or waiting) */
  p0: number;
  ref: number | null;
  /** progress per second */
  rate: number;
  fps: number;
  /** seconds: rental boot, upload; then after the run: download, clean-up */
  pre: readonly [number, number];
  post: readonly [number, number];
  /** cloud money: dollars at `ref`, dollars per second, expected total */
  usd0: number;
  usdRate: number;
  queuedAt: number;
  settleAt: number | null;
  /** fixed numbers for the seeded running jobs; new jobs have none */
  gpu: Job['gpu_stats'];
  /** seeded running jobs stand still at their mockup numbers unless the page has `?live=1` */
  freeze: boolean;
  /** the scripted LTX job (`?job=ltx`): its fields come from the script, not from a rate */
  script: boolean;
}

const params = new URLSearchParams(location.search);
const SEED = params.get('job') !== '0' && params.get('job') !== 'ltx';
const SCRIPT = params.get('job') === 'ltx';
/** `?job=ltx&at=<moment>` holds one moment of the script */
const HOLD = params.get('at');
const T0 = Date.now();
const sims: Sim[] = [];
let counter = 0;

const sceneDur = (id: string): number => BASE_SCENES.find((s) => s.id === id)?.files[0]?.duration ?? 1800;
const sceneH = (id: string): number => BASE_SCENES.find((s) => s.id === id)?.files[0]?.height ?? 1080;
const sceneBytes = (id: string): number => BASE_SCENES.find((s) => s.id === id)?.files[0]?.size ?? GB;

function segmentsFor(duration: number, count: number): JobSegment[] {
  return MOMENTS.slice(0, count).map(([a, w], n) => ({ n, start: Math.round((a / 100) * duration), end: Math.round(((a + w) / 100) * duration) }));
}

function baseJob(sceneId: string, op: OpId, quality: QualityId, state: Job['state']): Job {
  counter++;
  return {
    id: `j${counter}`,
    scene_id: sceneId,
    scene_title: sceneTitle(sceneId) ?? null,
    state,
    progress: 0,
    backend: op === 'upscale' ? 'upscale' : op === 'transcode' ? 'transcode' : 'decensor',
    op,
    quality,
    runner: HOME,
    total_frames: Math.round(sceneDur(sceneId) * FPS),
    created: Math.floor(Date.now() / 1000),
  };
}

function makeSim(job: Job, over: Partial<Sim>): Sim {
  const s: Sim = {
    job, cloud: false, t0: Date.now(), p0: 0, ref: null, rate: 0, fps: 0, pre: [0, 0], post: [0, 0],
    usd0: 0, usdRate: 0, queuedAt: Date.now(), settleAt: null, gpu: null, freeze: false, script: false, ...over,
  };
  sims.push(s);
  return s;
}

function progressOf(s: Sim, now: number): number {
  return s.ref === null ? s.p0 : Math.min(1, s.p0 + (Math.max(0, now - s.ref) / 1000) * s.rate);
}

function finish(s: Sim): void {
  const j = s.job;
  const dur = sceneDur(j.scene_id);
  const up = j.op === 'decensor_up' || j.op === 'upscale';
  j.state = 'review_ready';
  j.progress = 1;
  j.message = null;
  j.stage = null;
  j.eta = null;
  j.cloud_phase = null;
  j.gpu_stats = null;
  j.paused = false;
  j.frame = j.total_frames;
  j.review_scene_id = `p${j.scene_id}`;
  j.sample = true;
  j.segments = segmentsFor(dur, MOMENTS.length);
  j.result = {
    duration: dur,
    from_height: sceneH(j.scene_id),
    to_height: up ? sceneH(j.scene_id) * 2 : sceneH(j.scene_id),
    from_bytes: sceneBytes(j.scene_id),
    to_bytes: Math.round(sceneBytes(j.scene_id) * (up ? 2.1 : 1)),
  };
}

function startHome(s: Sim, now: number): void {
  const j = s.job;
  j.state = 'running';
  s.t0 = now;
  s.ref = now;
  j.message = 'Restoring';
  j.stage = 'Restoring';
}

function startCloud(s: Sim, now: number): void {
  s.job.state = 'running';
  s.t0 = now;
  s.ref = now + (s.pre[0] + s.pre[1]) * 1000;
  s.job.message = 'Renting a machine';
}

const PHASES = ['upload', 'boot'] as const;

/** Bring every job up to `now`. Cheap: a handful of jobs. */
function advance(now: number): void {
  for (const s of sims) {
    const j = s.job;
    if (j.state === 'replacing' || j.state === 'discarding') {
      if (s.settleAt !== null && now >= s.settleAt) j.state = j.state === 'replacing' ? 'replaced' : 'discarded';
      continue;
    }
    if (j.state === 'queued' && now >= s.queuedAt + QUEUE_MS) {
      if (s.cloud) startCloud(s, now);
      else if (!sims.some((o) => !o.cloud && o.job.state === 'running')) startHome(s, now);
    }
    if (j.state !== 'running') continue;
    if (s.script) {
      const f = HOLD ? ltxScene(HOLD) : ltxFrame((now - s.t0) / 1000);
      if (f) Object.assign(j, f, { frame: null, gpu_stats: s.gpu });
      else finish(s);
      continue;
    }

    const at = s.freeze ? T0 : now;
    const p = progressOf(s, at);
    const el = (at - s.t0) / 1000;
    j.elapsed = Math.round(el);
    j.progress = p;
    if (s.cloud) {
      const [boot, up] = s.pre;
      const runDone = (s.ref ?? at) + ((1 - s.p0) / s.rate) * 1000;
      const after = (at - runDone) / 1000;
      if (el < boot) j.cloud_phase = PHASES[0];
      else if (el < boot + up) j.cloud_phase = PHASES[1];
      else if (p < 1) j.cloud_phase = 'run';
      else if (after < s.post[0]) j.cloud_phase = 'download';
      else if (after < s.post[0] + s.post[1]) j.cloud_phase = 'cleanup';
      else { finish(s); continue; }
      j.message = { upload: 'Sending the video', boot: 'Renting a machine', run: 'Restoring', download: 'Fetching the result', cleanup: 'Cleaning up' }[j.cloud_phase];
      const spent = Math.min(j.cloud_cap ?? Infinity, s.usd0 + (Math.max(0, at - (s.ref ?? at)) / 1000) * s.usdRate);
      j.cloud_usd = Math.round(spent * 100) / 100;
    } else if (p >= 1) {
      finish(s);
      continue;
    }
    const running = !s.cloud || j.cloud_phase === 'run';
    j.stage = running ? 'Restoring' : null;
    j.frame = running ? Math.round(p * (j.total_frames ?? 0)) : null;
    j.fps = running && !j.paused ? s.fps : null;
    j.eta = running && s.rate > 0 && !j.paused ? Math.round((1 - p) / s.rate) : null;
    j.gpu_stats = running && !j.paused ? s.gpu ?? null : null;
    if (j.backend === 'decensor' && running) j.segments = segmentsFor(sceneDur(j.scene_id), Math.floor(p * MOMENTS.length));
  }
}

const LIVE = params.get('live') === '1';

function seedScript(): void {
  const t = Date.now();
  const secs = Math.floor(t / 1000);
  if (HOLD === 'error') {
    const bad = baseJob('6', 'decensor_up', 'high', 'error');
    Object.assign(bad, { runner: `${HOME} · RTX 3080`, stage: 'decensor', message: 'decensor exited 1', error: 'decensor exited 1', created: secs - 3000, elapsed: 2400, passes: 2, pass_no: 1 });
    makeSim(bad, { t0: t - 2400_000 });
    return;
  }
  const job = baseJob('6', 'decensor_up', 'high', 'running');
  Object.assign(job, { runner: `${HOME} · RTX 3080`, preview: false, created: secs - 60 });
  const gpu = { util: 96, mem_used: 9120, mem_total: 10240, temp: 70, power: 301 };
  makeSim(job, { t0: t, ref: t, gpu, script: true });
  advance(t);
}

function seed(): void {
  const t = T0;
  const secs = Math.floor(t / 1000);
  // oldest first, like the real server: the page shows newest first
  const review = baseJob('11', 'decensor_up', 'high', 'review_ready');
  const rs = makeSim(review, { cloud: true, t0: t - 7500_000 });
  review.cloud = true;
  review.cloud_label = CLOUD_LABEL;
  review.provider = CLOUD_PROVIDER;
  review.cloud_usd = 0.3;
  review.cloud_cap = BUDGET.jobCap;
  review.elapsed = 7500;
  review.created = secs - 9000;
  finish(rs);
  review.result = { duration: 2120, from_height: 1080, to_height: 2160, from_bytes: Math.round(1.6 * GB), to_bytes: Math.round(3.4 * GB) };

  // the mockup's two running jobs both have 34,776 frames
  const cloudP = 13214 / 34776;
  const cloud = baseJob('12', 'decensor', 'high', 'running');
  Object.assign(cloud, {
    cloud: true, runner: null, cloud_label: CLOUD_LABEL, provider: CLOUD_PROVIDER, cloud_tier: 'dc', sent_bytes: 2 * GB,
    cloud_usd: 0.15, cloud_cap: BUDGET.jobCap, cloud_est_usd: 0.37, preview: true, total_frames: 34776, created: secs - 900,
  });
  makeSim(cloud, {
    cloud: true, t0: t - 840_000, p0: cloudP, ref: t, rate: (1 - cloudP) / 860, fps: 40.1, pre: [40, 80], post: [6, 3],
    usd0: 0.15, usdRate: (0.37 - 0.15) / 860, gpu: { util: 96, mem_used: 18329, mem_total: 32768, temp: 64, power: 402 }, freeze: !LIVE,
  });

  const homeP = 22257 / 34776;
  const home = baseJob('6', 'decensor', 'balanced', 'running');
  Object.assign(home, { preview: true, runner: `${HOME} · RTX 3080`, total_frames: 34776, created: secs - 1000 });
  makeSim(home, { t0: t - 951_000, p0: homeP, ref: t, rate: (1 - homeP) / 522, fps: 23.4, gpu: { util: 97, mem_used: 8602, mem_total: 10240, temp: 71, power: 318 }, freeze: !LIVE });

  const q1 = baseJob('8', 'decensor', 'fast', 'queued');
  q1.eta = 11 * 60;
  q1.created = secs - 700;
  makeSim(q1, { rate: 1 / 60, fps: 30 });
  const q2 = baseJob('5', 'upscale', 'balanced', 'queued');
  q2.eta = 80 * 60;
  q2.quality = null;
  q2.created = secs - 600;
  makeSim(q2, { rate: 1 / 60, fps: 30 });
  // the seeded waiters start only after the home run finishes
  advance(t);
}
if (SEED) seed();
if (SCRIPT) seedScript();

/* ---------- log ---------- */
function logFor(s: Sim, now: number, after: number): JobLog {
  const j = s.job;
  const el = Math.max(0, ((j.state === 'running' ? now : s.t0 + (j.elapsed ?? 0) * 1000) - s.t0) / 1000);
  const n = j.state === 'queued' ? 0 : Math.min(800, Math.floor(el * 1.5) + 8);
  const lines: JobLogLine[] = [];
  const total = j.total_frames ?? 0;
  for (let i = Math.max(after, 0); i < n; i++) {
    const seq = i + 1;
    const t = Math.floor((s.t0 + (i / 1.5) * 1000) / 1000);
    if (i % 5 === 4) lines.push({ seq, t, level: 'event', text: EVENTS[Math.floor(i / 5) % EVENTS.length] });
    else {
      const done = Math.min(total, Math.round(((i + 1) / Math.max(n, 1)) * (j.frame ?? Math.round(j.progress * total))));
      lines.push({ seq, t, level: 'proc', live: true, text: `decensor: ${total ? Math.round((100 * done) / total) : 0}%| |Processed: (${done}f) | Speed: ${s.fps || 24} f/s` });
    }
  }
  if (j.state === 'error' && after < n) lines.push({ seq: n + 1, t: Math.floor((s.t0 + el * 1000) / 1000), level: 'error', text: j.error ?? 'failed' });
  return { cursor: j.state === 'error' ? n + 1 : n, lines: lines.slice(-200) };
}

/* ---------- routes ---------- */
function find(id: string): Sim {
  advance(Date.now());
  const s = sims.find((x) => x.job.id === id);
  if (!s) fail(404, 'No such job');
  return s;
}

route('GET', 'api/jobs', () => {
  advance(Date.now());
  return sims.map((s) => s.job);
});
route('GET', 'api/jobs/:id', (c) => find(c.params.id).job);
route('GET', 'api/jobs/:id/log', (c) => {
  const s = find(c.params.id);
  return logFor(s, Date.now(), Number(c.query.get('after') ?? 0) || 0);
});

route('POST', 'api/jobs/:id/cancel', (c) => {
  const s = find(c.params.id);
  if (s.job.state !== 'queued' && s.job.state !== 'running') fail(409, 'That job cannot be cancelled now');
  s.job.state = 'cancelled';
  s.job.message = null;
  s.job.cloud_phase = null;
  return s.job;
});
route('POST', 'api/jobs/:id/pause', (c) => {
  const s = find(c.params.id);
  if (s.job.state !== 'running' || s.cloud || s.job.paused) fail(409, 'That job cannot be paused now');
  const now = Date.now();
  s.p0 = progressOf(s, s.freeze ? T0 : now);
  s.freeze = false;
  s.ref = null;
  s.job.paused = true;
  return s.job;
});
route('POST', 'api/jobs/:id/resume', (c) => {
  const s = find(c.params.id);
  if (s.job.state !== 'running' || !s.job.paused) fail(409, 'That job is not paused');
  s.ref = Date.now();
  s.job.paused = false;
  return s.job;
});
route('POST', 'api/jobs/:id/replace', (c) => {
  const s = find(c.params.id);
  if (s.job.state !== 'review_ready') fail(409, 'Nothing to replace yet');
  s.job.state = 'replacing';
  s.settleAt = Date.now() + SETTLE_MS;
  return s.job;
});
route('POST', 'api/jobs/:id/discard', (c) => {
  const s = find(c.params.id);
  if (s.job.state !== 'review_ready') fail(409, 'Nothing to discard');
  s.job.state = 'discarding';
  s.settleAt = Date.now() + SETTLE_MS;
  return s.job;
});

function isSubmit(b: unknown): b is SubmitBody {
  return typeof b === 'object' && b !== null && 'scene_id' in b && typeof b.scene_id === 'string' && 'backend' in b && typeof b.backend === 'string';
}

const PRESET_QUALITY: Record<string, QualityId> = { 'close-ups': 'fast', detail: 'balanced', 'detail-close-ups': 'high' };

route('POST', 'api/decensor', (c) => {
  const b = c.body;
  if (!isSubmit(b)) fail(400, 'scene_id is required');
  if (!sceneTitle(b.scene_id)) fail(404, 'No such scene');
  const cloud = typeof b.offer_id === 'string';
  const op: OpId = b.backend === 'upscale' ? 'upscale' : b.backend === 'transcode' ? 'transcode' : b.post_upscale ? 'decensor_up' : 'decensor';
  const quality: QualityId = cloud ? PRESET_QUALITY[b.cloud_preset ?? ''] ?? 'balanced' : b.detection_model === 'v4-fast' ? 'fast' : 'balanced';
  const j = baseJob(b.scene_id, op, quality, 'queued');
  j.preview = b.preview ?? true;
  const dur = sceneDur(b.scene_id);
  if (cloud) {
    const est = Math.round((0.12 + (dur / 3600) * 0.55) * 100) / 100;
    Object.assign(j, { cloud: true, runner: null, cloud_label: CLOUD_LABEL, provider: b.offer_id?.startsWith('r') ? 'RunPod' : CLOUD_PROVIDER, cloud_usd: 0, cloud_cap: BUDGET.jobCap, cloud_est_usd: est, cloud_phase: 'boot' });
    const runSec = 100;
    makeSim(j, { cloud: true, rate: 1 / runSec, fps: 40.1, pre: [6, 8], post: [6, 3], usdRate: est / runSec, gpu: { util: 96, mem_used: 18329, mem_total: 32768, temp: 64, power: 402 } });
  } else {
    j.runner = b.runner ?? HOME;
    j.eta = Math.round(dur / 40);
    makeSim(j, { rate: 1 / 120, fps: 23.4, gpu: { util: 97, mem_used: 8602, mem_total: 10240, temp: 71, power: 318 } });
  }
  return j;
});
