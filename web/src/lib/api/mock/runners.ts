/* Mock server for the runner list, the live GPU meters, discovery, connection tests and wake.
   Seeded from the mockup: "This PC" (busy, 97%) and "NAS" (asleep, wakes in about 40 s). Changes stay in memory. */
import type { DiscoveredRunner, GpuMeter, GpuStats, Health, RoutePreview, Runner } from '../types';
import { fail, route } from './router';

const PC_URL = 'http://desktop-3080:8711';
const NAS_URL = 'http://p40-nas:8711';

const pc: Runner = {
  name: 'This PC', url: PC_URL, node: 'desktop-3080', kind: 'desktop', source: 'manual',
  ops: ['decensor', 'upscale', 'transcode'],
  engines: { decensor: 'jasna', upscale: 'jasna', transcode: 'ffmpeg' },
  prefer: ['decensor'], online: true, busy: true,
  gpu: 'RTX 3080', vram_gb: 10, speed: 1, qualities: ['fast', 'balanced', 'high', 'max'], queue_eta_s: 540,
};
const nas: Runner = {
  name: 'NAS', url: NAS_URL, node: 'p40-nas', kind: 'nas', source: 'manual',
  ops: ['decensor', 'transcode'],
  engines: { decensor: 'lada', transcode: 'ffmpeg' },
  prefer: null, online: false, asleep: true, can_wake: true, wake_eta_s: 40,
  gpu: 'Tesla P40', vram_gb: 24, speed: 0.45, qualities: ['fast'], note: 'asleep - wakes when a job needs it',
};

const store: Runner[] = [pc, nas];
/** runner url -> when its wake finishes (ms) */
const waking = new Map<string, number>();
const WAKE_ETA_S = 40;

/** A woken runner comes online once its time is up; until then it counts down. */
function settle(): void {
  const now = Date.now();
  for (const [url, at] of waking) {
    const r = store.find((x) => x.url === url);
    if (!r) { waking.delete(url); continue; }
    if (now >= at) {
      waking.delete(url);
      Object.assign(r, { online: true, asleep: false, can_wake: false, wake_eta_s: null, note: null, busy: false });
    } else {
      r.wake_eta_s = Math.ceil((at - now) / 1000);
    }
  }
}

const slow = (ms: number, a: number): number => Math.sin(Date.now() / ms) * a;

/** utilisation wanders so the meters visibly move */
function statsFor(r: Runner): GpuStats {
  const vramMb = (r.vram_gb ?? 8) * 1024;
  if (r.busy) {
    const util = Math.round(97 + slow(2300, 2) + slow(700, 1));
    return { util, mem_used: Math.round(vramMb * 0.84 + slow(5000, 40)), mem_total: vramMb, temp: Math.round(71 + slow(9000, 2)), power: Math.round(318 + slow(1500, 9)) };
  }
  return { util: Math.max(0, Math.round(2 + slow(3000, 2))), mem_used: Math.round(vramMb * 0.07), mem_total: vramMb, temp: 38, power: 24 };
}

/** a trimmed string field of a request body, or '' */
function textOf(body: unknown, key: string): string {
  if (body === null || typeof body !== 'object') return '';
  const v: unknown = Object.entries(body).find(([k]) => k === key)?.[1];
  return typeof v === 'string' ? v.trim() : '';
}
const urlOf = (body: unknown): string => textOf(body, 'url').replace(/\/+$/, '');

const DISCOVERED: readonly DiscoveredRunner[] = [
  { name: 'Studio PC', url: 'http://192.168.1.52:8711', ops: ['decensor', 'upscale', 'transcode'], engines: { decensor: 'jasna', upscale: 'jasna', transcode: 'ffmpeg' }, kind: 'desktop', registered: false },
  { name: 'Basement box', url: 'http://192.168.1.77:8712', ops: ['decensor', 'transcode'], engines: { decensor: 'lada', transcode: 'ffmpeg' }, kind: 'nas', registered: false },
  { name: 'NAS', url: NAS_URL, ops: ['decensor', 'transcode'], engines: { decensor: 'lada', transcode: 'ffmpeg' }, kind: 'nas', registered: true },
];

route('GET', 'api/runners', () => { settle(); return store; });

route('POST', 'api/runners', ({ body }) => {
  const url = urlOf(body);
  if (!/^https?:\/\/\S+$/.test(url)) fail(400, 'a valid http(s) url is required');
  const found = DISCOVERED.find((d) => d.url === url);
  const prefer = body && typeof body === 'object' && 'prefer' in body ? body.prefer : null;
  const wantsPrefer = Array.isArray(prefer) ? prefer.length > 0 : typeof prefer === 'string' && prefer !== '';
  const existing = store.find((r) => r.url === url);
  const entry: Runner = existing ?? {
    name: textOf(body, 'name') || found?.name || url, url, node: new URL(url).hostname, kind: found?.kind ?? 'desktop', source: 'manual',
    ops: found?.ops ?? ['decensor', 'upscale', 'transcode'], engines: found?.engines ?? { decensor: 'jasna', upscale: 'jasna', transcode: 'ffmpeg' },
    online: true, busy: false, gpu: 'RTX 4070', vram_gb: 12, speed: 1.1, qualities: ['fast', 'balanced', 'high', 'max'],
  };
  if (existing && textOf(body, 'name')) existing.name = textOf(body, 'name');
  entry.prefer = wantsPrefer ? [...entry.ops] : [];
  if (!existing) store.push(entry);
  return entry;
});

route('POST', 'api/runners/remove', ({ body }) => {
  const url = urlOf(body);
  const i = store.findIndex((r) => r.url === url);
  if (i >= 0 && store[i].source === 'env') fail(400, 'this runner comes from the environment and cannot be removed here');
  if (i >= 0) store.splice(i, 1);
  waking.delete(url);
  return { ok: true };
});

route('POST', 'api/runners/test', ({ body }) => {
  const url = urlOf(body);
  if (!/^https?:\/\/\S+$/.test(url)) fail(400, 'a valid http(s) url is required');
  const host = new URL(url).hostname;
  if (/offline|down|unreachable/.test(host)) return { ok: false, error: 'Could not reach that address. Check the machine is on and the port is right.' };
  if (textOf(body, 'token') === 'wrong') return { ok: false, error: 'The machine answered but refused the access token.' };
  const known = store.find((r) => r.url === url) ?? DISCOVERED.find((d) => d.url === url);
  return { ok: true, ops: known?.ops ?? ['decensor', 'upscale', 'transcode'], engines: known?.engines ?? { decensor: 'jasna', upscale: 'jasna', transcode: 'ffmpeg' } };
});

route('POST', 'api/runners/discover', () => DISCOVERED.map((d) => ({ ...d, registered: store.some((r) => r.url === d.url) })));

/** proposed endpoint */
route('POST', 'api/runners/wake', ({ body }) => {
  const url = urlOf(body);
  const r = store.find((x) => x.url === url);
  if (!r) fail(404, 'no such runner');
  if (r.online) return { ok: true, eta_s: 0 };
  if (!r.can_wake) fail(409, 'this runner has no wake address (add its MAC address to wake it)');
  if (!waking.has(url)) waking.set(url, Date.now() + WAKE_ETA_S * 1000);
  settle();
  return { ok: true, eta_s: r.wake_eta_s ?? WAKE_ETA_S };
});

route('GET', 'api/gpus', (): GpuMeter[] => {
  settle();
  return store.map((r) => ({ name: r.name, url: r.url, online: r.online, active: !!r.busy, gpu: r.online ? statsFor(r) : null }));
});

route('GET', 'api/health', (): Health => {
  settle();
  const r = store.find((x) => x.online) ?? pc;
  return { ok: true, gpu: r.gpu ?? null, backend: r.engines.decensor ?? null, postUpscale: r.ops.includes('upscale'), runner: r.name, lada: r.engines.decensor === 'lada', gpu_stats: statsFor(r) };
});

route('GET', 'api/route-preview', ({ query }): RoutePreview => {
  settle();
  const op = (query.get('op') ?? '').replace(' ', '+');
  if (!['decensor', 'decensor+upscale', 'upscale', 'transcode'].includes(op)) fail(400, 'valid op required');
  const key = op === 'decensor+upscale' ? 'decensor' : op;
  const pinned = query.get('runner');
  const candidates = store.filter((r) => r.online && r.ops.includes(key) && (!pinned || pinned === 'auto' || r.url === pinned));
  const pick = candidates.find((r) => !r.busy) ?? candidates[0];
  if (!pick) return { error: 'No runner is online for this job.' };
  return { runner: pick.name, engine: pick.engines[key] };
});
