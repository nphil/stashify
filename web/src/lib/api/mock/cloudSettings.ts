/* Mock server for the cloud settings page: limits, saved keys, this month's spend and the key test.
   Saving writes into BUDGET, so the cloud mock (offers, quotes, confirm) sees the new limits at once. */
import type { CloudActive, CloudRental, CloudSettings, CloudStatus, CloudTest, ProviderState, SecretState } from '../types';
import { BUDGET, PROVIDERS } from './shared';
import { fail, route } from './router';

/** `?keybusy=1` makes a key change refuse with 409, like the server does while a rental is running */
const KEY_BUSY = new URLSearchParams(location.search).get('keybusy') === '1';

type KeyField = 'vast_api_key' | 'runpod_api_key' | 'jasna_license';
const KEY_FIELDS: readonly KeyField[] = ['vast_api_key', 'runpod_api_key', 'jasna_license'];
const keys: Record<KeyField, SecretState> = {
  vast_api_key: { set: true, last4: 'x7Kq', source: 'saved' },
  runpod_api_key: PROVIDERS === 2 ? { set: true, last4: '9aD2', source: 'saved' } : { set: false, last4: '', source: 'none' },
  jasna_license: { set: true, last4: '7c2d', source: 'saved' },
};
const extras = { max_bw_cost: 0.02, max_job_hours: 3 };

/** largest sane value per limit (same as the real settings) */
const LIMITS = {
  max_price: 50, job_cap: 1000, month_cap: 10000, max_bw_cost: 10, max_job_hours: 168,
} as const;
type LimitField = keyof typeof LIMITS;
const LIMIT_FIELDS: readonly LimitField[] = ['max_price', 'job_cap', 'month_cap', 'max_bw_cost', 'max_job_hours'];

function settings(): CloudSettings {
  return {
    vast_api_key: keys.vast_api_key,
    ...(PROVIDERS === 2 && { runpod_api_key: keys.runpod_api_key }),
    jasna_license: keys.jasna_license,
    max_price: BUDGET.maxHour, job_cap: BUDGET.jobCap, month_cap: BUDGET.month, datacenter_only: BUDGET.dcOnly,
    ...extras,
  };
}

const at = (month: number, day: number, h: number, m: number): number => new Date(2026, month - 1, day, h, m).getTime() / 1000;
const RECENT: CloudRental[] = [
  { ended: at(11, 23, 18, 4), scene: 'City walk after rain', preset: 'detail', gpu: 'RTX 4090', usd: 0.28, provider: 'Vast.ai' },
  { ended: at(11, 22, 21, 40), scene: 'Garden party', preset: 'detail-close-ups', gpu: 'RTX 5090', usd: 1.12, provider: 'Vast.ai' },
  { ended: at(11, 20, 9, 15), scene: 'Summer rental', preset: 'close-ups', gpu: 'RTX 4090', usd: 0.64, provider: 'Vast.ai' },
  { ended: at(11, 12, 20, 2), scene: 'Rooftop evening', preset: 'detail', gpu: 'RTX 4090', usd: 0.52, provider: 'Vast.ai' },
  { ended: at(11, 6, 7, 51), scene: 'Market morning', preset: 'close-ups', gpu: 'RTX 4090', usd: 0.28, provider: 'Vast.ai' },
];

function status(): CloudStatus {
  const active: CloudActive[] = BUDGET.running > 0
    ? [{ id: 'rent-1', scene_id: '12', scene: 'Winter cabin', preset: 'detail-close-ups', gpu: 'RTX 5090', usd: 0.31, started: Date.now() / 1000 - 1500, provider: 'Vast.ai' }]
    : [];
  const providers: ProviderState[] = [{ id: 'vast', name: 'Vast.ai', connected: keys.vast_api_key.set, balance: keys.vast_api_key.set ? BUDGET.credit : null }];
  if (PROVIDERS === 2) providers.push({ id: 'runpod', name: 'RunPod', connected: keys.runpod_api_key.set, balance: keys.runpod_api_key.set ? 21.5 : null });
  return { month_to_date: BUDGET.spent, credit: keys.vast_api_key.set ? BUDGET.credit : null, active, recent: RECENT, providers };
}

route('GET', 'api/cloud/settings', () => settings());

route('POST', 'api/cloud/settings', ({ body }) => {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) fail(400, 'expected a JSON object');
  const patch = new Map(Object.entries(body));

  // validate everything first, so a bad value changes nothing
  const limits = new Map<LimitField, number>();
  for (const f of LIMIT_FIELDS) {
    if (!patch.has(f)) continue;
    const v = Number(patch.get(f));
    if (!Number.isFinite(v)) fail(400, `${f} must be a number`);
    if (v <= 0 || v > LIMITS[f]) fail(400, `${f} must be more than 0 and at most ${LIMITS[f]}`);
    limits.set(f, f === 'max_bw_cost' ? Math.round(v * 10000) / 10000 : Math.round(v * 100) / 100);
  }
  if (patch.has('datacenter_only') && typeof patch.get('datacenter_only') !== 'boolean') fail(400, 'datacenter_only must be true or false');

  const secrets = new Map<KeyField, SecretState | null>();
  for (const f of KEY_FIELDS) {
    if (!patch.has(f)) continue;
    const v = patch.get(f);
    if (v === null) { secrets.set(f, null); continue; }
    if (typeof v !== 'string' || v.trim().length < 8 || v.trim().length > 512 || /\s/.test(v.trim())) fail(400, `${f} looks wrong (8-512 characters, no spaces)`);
    secrets.set(f, { set: true, last4: v.trim().slice(-4), source: 'saved' });
  }
  if (KEY_BUSY && (secrets.has('vast_api_key') || secrets.has('runpod_api_key'))) {
    fail(409, 'A cloud machine is still running, or not yet confirmed destroyed. Change the key after it is gone.', { code: 'busy' });
  }

  for (const [f, v] of limits) {
    if (f === 'max_price') BUDGET.maxHour = v;
    else if (f === 'job_cap') BUDGET.jobCap = v;
    else if (f === 'month_cap') BUDGET.month = v;
    else if (f === 'max_bw_cost') extras.max_bw_cost = v;
    else extras.max_job_hours = v;
  }
  const dc = patch.get('datacenter_only');
  if (typeof dc === 'boolean') BUDGET.dcOnly = dc;
  for (const [f, v] of secrets) keys[f] = v ?? { set: false, last4: '', source: 'none' };
  return settings();
});

route('GET', 'api/cloud/status', () => status());

route('POST', 'api/cloud/test', ({ body }): CloudTest => {
  const rec = body !== null && typeof body === 'object' ? new Map(Object.entries(body)) : new Map<string, unknown>();
  const provider = rec.get('provider') === 'runpod' ? 'runpod' : 'vast';
  const field: KeyField = provider === 'runpod' ? 'runpod_api_key' : 'vast_api_key';
  const typed = rec.get(field);
  const stored = keys[field];
  if (typeof typed === 'string' && typed.trim()) {
    if (typed.trim().startsWith('bad')) return { ok: false, error: 'The provider rejected this key (401).' };
    return { ok: true, balance: provider === 'vast' ? BUDGET.credit : 21.5 };
  }
  if (!stored.set) return { ok: false, error: 'No key is saved yet.' };
  return { ok: true, balance: provider === 'vast' ? BUDGET.credit : 21.5 };
});
