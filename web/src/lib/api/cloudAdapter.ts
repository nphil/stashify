/* Wire -> UI for the GPU rental market. This is the only file that knows the server's field names.
   Two payload shapes are read, field by field, and nothing is trusted:
     (a) what the hireling-based server sends today: price_per_hour, num_gpus, reliability 0..1,
         estimate.breakdown{compute, storage, bandwidth, gb_down, gb_up}, hardware_presets[], countries facet.
     (b) the flatter shape the backend plans: cpu, perf, flops, estimate{minutes, compute, transfer, storage, total},
         presets{role: offer id}, regions facet, caps.
   The views only ever see Offer / Quote / OffersResult from types.ts. */
import { r2, usd } from '../util/format';
import type { Caps, CloudStatus, Job, Offer, OfferFacets, OffersResult, Quote, RecRole, Recommendation, SortOption, Tier } from './types';

type Rec = Record<string, unknown>;

export const isRec = (v: unknown): v is Rec => typeof v === 'object' && v !== null && !Array.isArray(v);
/** `v` as a record, or an empty one: reading a missing branch never throws */
const rec = (v: unknown): Rec => (isRec(v) ? v : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/** a finite number from a number or a numeric string, else null */
function numOf(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}
const num = (v: unknown, d = 0): number => numOf(v) ?? d;
function firstNum(...vs: unknown[]): number | null {
  for (const v of vs) {
    const n = numOf(v);
    if (n !== null) return n;
  }
  return null;
}
const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : d);

/* ---------- names ---------- */

export const ROLE_LABEL: Record<RecRole, string> = { value: 'Best value', fast: 'Fastest', cheap: 'Cheapest', reliable: 'Most reliable' };
const ROLE_ORDER: readonly RecRole[] = ['value', 'fast', 'cheap', 'reliable'];
const ROLE_OF: Readonly<Record<string, RecRole>> = { best_value: 'value', fastest: 'fast', cheapest: 'cheap', most_reliable: 'reliable' };

const PROVIDER_NAMES: Readonly<Record<string, string>> = { vast: 'Vast.ai', runpod: 'RunPod' };
/** what to call a provider: the server's display name, else a known one, else its id with a capital */
export function providerName(id: string, names?: ReadonlyMap<string, string>): string {
  return names?.get(id) ?? PROVIDER_NAMES[id] ?? (id ? id.charAt(0).toUpperCase() + id.slice(1) : '');
}

/** `RTX_4090` and `RTX 4090` are one model */
export const cleanGpu = (name: string): string => name.replace(/[_\s]+/g, ' ').trim();

/* ---------- regions (a country list per region; the server only knows countries) ---------- */

export const REGIONS = ['Europe', 'N. America', 'Asia', 'S. America', 'Oceania', 'Africa'] as const;
const REGION_COUNTRIES: Readonly<Record<(typeof REGIONS)[number], string>> = {
  Europe: 'AD AL AT AX BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI GR HR HU IE IM IS IT JE LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM UA VA XK',
  'N. America': 'US CA MX PR GT BZ SV HN NI CR PA CU DO JM HT BS BB TT',
  Asia: 'JP KR CN TW HK MO SG MY TH VN ID PH IN PK BD LK NP KH LA MM MN KZ UZ KG TJ TM AF AE SA IL TR QA KW BH OM JO LB IR IQ AM AZ GE BN',
  'S. America': 'BR AR CL CO PE UY PY BO EC VE GY SR',
  Oceania: 'AU NZ FJ PG',
  Africa: 'ZA EG NG KE MA TN DZ GH ET TZ UG SN CI',
};
const REGION_OF = new Map<string, string>();
for (const [region, codes] of Object.entries(REGION_COUNTRIES)) for (const c of codes.split(' ')) REGION_OF.set(c, region);

/** the region a two-letter country code is in, or '' when unknown */
export const regionOf = (code: string): string => REGION_OF.get(code.trim().toUpperCase()) ?? '';
/** every country code of a region, for the `countries` filter */
export function countriesOf(region: string): string[] {
  const out: string[] = [];
  for (const [c, r] of REGION_OF) if (r === region) out.push(c);
  return out;
}
function countryOf(w: Rec): string {
  const c = str(w.country).trim();
  if (/^[A-Za-z]{2}$/.test(c)) return c.toUpperCase();
  const m = /,\s*([A-Za-z]{2})\s*$/.exec(str(w.location));
  return m ? m[1].toUpperCase() : '';
}

/* ---------- money ---------- */

/** Round each bill line to cents so that the lines add up to the total the server stated.
 *  Without this a receipt can read 0.11 + 0.00 + 0.01 = 0.13. Lines that cannot be reconciled are rounded plainly. */
function splitCents(total: number, lines: readonly number[]): number[] {
  const goal = Math.round(total * 100);
  const raw = lines.map((v) => Math.max(0, v) * 100);
  const cents = raw.map((v) => Math.floor(v + 1e-6));
  let rest = goal - cents.reduce((a, b) => a + b, 0);
  if (rest < 0 || rest > lines.length) return lines.map(r2);
  const order = raw.map((v, i) => ({ frac: v - cents[i], i })).sort((a, b) => b.frac - a.frac);
  for (const { i } of order) {
    if (rest <= 0) break;
    cents[i] += 1;
    rest -= 1;
  }
  return cents.map((c) => c / 100);
}

/** Why a price is over the owner's own limits (null = within them). Hardware reasons come from the server. */
export function budgetReason(total: number, pricePerHour: number, caps: Caps | null): string | null {
  if (!caps) return null;
  if (caps.maxPrice > 0 && pricePerHour > caps.maxPrice + 1e-9) return `Over your ${usd(caps.maxPrice)}/h limit`;
  if (caps.jobCap > 0 && total > caps.jobCap + 1e-9) return `Over your ${usd(caps.jobCap)} stop per rental`;
  if (caps.monthCap > 0 && total > caps.monthCap - caps.monthToDate + 1e-9) return 'Over this month’s limit';
  if (caps.credit !== null && total > caps.credit + 1e-9) return `Needs ${usd(total)}, you have ${usd(caps.credit)}`;
  return null;
}

interface Ctx {
  caps: Caps | null;
  /** GB of the job's own video, to tell "your upload" from "setup and models" in what the machine downloads */
  sceneGb: number;
  names: ReadonlyMap<string, string>;
}

function stringList(v: unknown[]): string[] {
  const out: string[] = [];
  for (const x of v) if (typeof x === 'string' && x !== '' && !out.includes(x)) out.push(x);
  return out;
}

/** One bill: the server's `estimate` of an offer, or a quote line (same keys). null = nothing was priced. */
function adaptEstimate(raw: unknown, ctx: Ctx): Quote | null {
  const e = rec(raw);
  const b = rec(e.breakdown);
  if (!('breakdown' in e || 'est_usd' in e || 'compute' in e || 'total' in e)) return null;
  const hours0 = firstNum(e.hours, b.hours);
  const minutes = firstNum(e.total_min, e.minutes) ?? (hours0 !== null ? hours0 * 60 : 0);
  const compute = firstNum(b.compute, e.compute) ?? 0;
  const storage = firstNum(b.storage, e.storage) ?? 0;
  const transfer = firstNum(b.bandwidth, e.transfer) ?? num(b.bw_down) + num(b.bw_up);
  const total = firstNum(e.est_usd, b.total, e.total) ?? compute + transfer + storage;
  if (total <= 0 && minutes <= 0) return null;
  const [c, t, s] = splitCents(total, [compute, transfer, storage]);
  // The machine downloads our video plus setup and models; it uploads the result.
  const gbIn = Math.max(0, num(b.gb_down));
  const gbUp = ctx.sceneGb > 0 ? Math.min(ctx.sceneGb, gbIn) : 0;
  const q: Quote = {
    minutes,
    hours: hours0 ?? minutes / 60,
    compute: c,
    transfer: t,
    storage: s,
    total: r2(total),
    max: r2(total * 1.25),
    gbUp,
    gbDown: Math.max(0, num(b.gb_up)),
    gbModels: gbIn - gbUp,
    diskGb: num(e.disk_gb),
    notes: stringList([...arr(e.assumptions), ...arr(b.notes)]),
  };
  const run = firstNum(e.processing_min);
  if (run !== null) q.runMin = run;
  const setup = firstNum(e.setup_min);
  if (setup !== null) q.setupMin = setup;
  const up = firstNum(e.up_min);
  if (up !== null) q.upMin = up;
  const down = firstNum(e.down_min);
  if (down !== null) q.downMin = down;
  return q;
}

function sumQuotes(qs: readonly Quote[], totalUsd: number | null): Quote | null {
  if (qs.length === 0) return null;
  if (qs.length === 1 && totalUsd === null) return qs[0];
  const out: Quote = { minutes: 0, hours: 0, compute: 0, transfer: 0, storage: 0, total: 0, max: 0, gbUp: 0, gbDown: 0, gbModels: 0, diskGb: 0, notes: [] };
  let run = 0, setup = 0, up = 0, down = 0, hasRun = false, hasSetup = false, hasUp = false, hasDown = false;
  for (const q of qs) {
    out.minutes += q.minutes; out.hours += q.hours;
    out.compute += q.compute; out.transfer += q.transfer; out.storage += q.storage; out.total += q.total;
    out.gbUp += q.gbUp; out.gbDown += q.gbDown; out.gbModels += q.gbModels;
    out.diskGb = Math.max(out.diskGb, q.diskGb);
    if (q.runMin !== undefined) { run += q.runMin; hasRun = true; }
    if (q.setupMin !== undefined) { setup += q.setupMin; hasSetup = true; }
    if (q.upMin !== undefined) { up += q.upMin; hasUp = true; }
    if (q.downMin !== undefined) { down += q.downMin; hasDown = true; }
    for (const n of q.notes) if (!out.notes.includes(n)) out.notes.push(n);
  }
  out.compute = r2(out.compute); out.transfer = r2(out.transfer); out.storage = r2(out.storage);
  out.total = r2(totalUsd ?? out.total);
  out.max = r2(out.total * 1.25);
  if (hasRun) out.runMin = run;
  if (hasSetup) out.setupMin = setup;
  if (hasUp) out.upMin = up;
  if (hasDown) out.downMin = down;
  return out;
}

/* ---------- offers ---------- */

const driverOf = (v: unknown): number => (typeof v === 'string' ? Number.parseInt(v, 10) || 0 : num(v));

function adaptOffer(w: Rec, ctx: Ctx): Offer | null {
  const id = str(w.id);
  if (id === '') return null;
  const providerId = str(w.provider);
  const count = Math.max(1, Math.round(num(w.num_gpus, num(w.count, 1))));
  const price = firstNum(w.price_per_hour, w.price) ?? 0;
  const country = countryOf(w);
  const tier: Tier = w.datacenter === true || str(w.host_type) === 'datacenter' || str(w.tier) === 'dc' ? 'dc' : 'comm';
  const rel = firstNum(w.reliability) ?? 0;
  const hours = firstNum(w.max_duration_hours);
  const pcieBw = num(w.pcie_bw);
  const memBw = num(w.gpu_mem_bw);
  const estimate = adaptEstimate(w.estimate, ctx);
  const fit = w.compatible === false ? str(w.incompatible_reason) || 'Can’t run this job' : null;
  const reason = fit ?? (estimate ? budgetReason(estimate.total, price, ctx.caps) : null);
  return {
    id,
    provider: providerName(providerId, ctx.names),
    providerId,
    gpu: cleanGpu(str(w.gpu)),
    count,
    vramGb: firstNum(w.vram_gb) ?? num(w.total_vram_gb) / count,
    tier,
    pricePerHour: price,
    reliability: Math.round((rel <= 1 ? rel * 100 : rel) * 10) / 10,
    verified: w.verified === true || w.verified === 1,
    days: firstNum(w.days) ?? (hours !== null ? Math.round((hours / 24) * 10) / 10 : 0),
    location: str(w.location),
    region: regionOf(country) || str(w.region),
    cpu: str(w.cpu_name, str(w.cpu)),
    cores: num(w.cpu_cores, num(w.cores)),
    ramGb: num(w.ram_gb, num(w.ram)),
    disk: str(w.disk_name, str(w.disk)),
    diskGb: num(w.disk_gb, num(w.free)),
    diskMbs: num(w.disk_bw, num(w.disk_mbs)),
    netDown: num(w.inet_down),
    netUp: num(w.inet_up),
    ports: num(w.open_ports),
    cuda: num(w.cuda),
    driver: driverOf(w.driver),
    pcie: pcieBw > 0 ? `${pcieBw.toFixed(pcieBw < 10 ? 1 : 0)} GB/s` : str(w.pcie),
    board: str(w.board, str(w.mobo)),
    perf: firstNum(w.dlperf, w.perf) ?? 0,
    tflops: firstNum(w.total_flops, w.flops) ?? 0,
    ...(memBw > 0 && { memGbs: memBw }),
    storagePerGbHr: num(w.storage_per_gb_hr),
    bwDownPerGb: num(w.bw_down_per_gb),
    bwUpPerGb: num(w.bw_up_per_gb),
    rented: w.rented === true,
    compatible: reason === null,
    incompatibleReason: reason,
    estimate,
  };
}

function adaptFacets(raw: unknown, names: ReadonlyMap<string, string>): OfferFacets {
  const f = rec(raw);
  const gpus = arr(f.gpus).map(rec)
    .map((g) => ({ name: cleanGpu(str(g.name)), count: num(g.count), minPrice: num(g.min_price, num(g.minPrice)), vramGb: num(g.vram_gb, num(g.vramGb)) }))
    .filter((g) => g.name !== '');
  const counts = arr(f.num_gpus ?? f.counts).map(rec)
    .map((c) => ({ value: num(c.value), count: num(c.count) }))
    .filter((c) => c.value > 0);
  const byRegion = new Map<string, number>();
  for (const c of arr(f.countries).map(rec)) {
    const r = regionOf(str(c.code));
    if (r !== '') byRegion.set(r, (byRegion.get(r) ?? 0) + num(c.count));
  }
  for (const r of arr(f.regions).map(rec)) {
    const code = str(r.code);
    if (code !== '') byRegion.set(code, (byRegion.get(code) ?? 0) + num(r.count));
  }
  const order = (code: string): number => {
    const i = (REGIONS as readonly string[]).indexOf(code);
    return i < 0 ? REGIONS.length : i;
  };
  const regions = [...byRegion].map(([code, count]) => ({ code, count })).sort((a, b) => order(a.code) - order(b.code));
  const providers = arr(f.providers).map(rec).map((p) => ({ name: providerName(str(p.name), names), count: num(p.count) }));
  const out: OfferFacets = { gpus, counts: counts.sort((a, b) => a.value - b.value), regions, providers };
  const rented = numOf(f.rented);
  if (rented !== null) out.rented = rented;
  return out;
}

/** what the server offers to sort by; used when it does not say */
export const SORTS_FALLBACK: readonly SortOption[] = [
  { id: 'auto', label: 'Auto', needsJob: false, available: true },
  { id: 'est_total', label: 'Estimated total', needsJob: true, available: true },
  { id: 'est_time', label: 'Estimated time', needsJob: true, available: true },
  { id: 'perf_per_dollar', label: 'Performance per $/h', needsJob: false, available: true },
  { id: 'perf', label: 'Performance', needsJob: false, available: true },
  { id: 'flops_per_dollar', label: 'TFLOPS per $/h', needsJob: false, available: true },
  { id: 'flops', label: 'TFLOPS', needsJob: false, available: true },
  { id: 'price_asc', label: 'Price: low to high', needsJob: false, available: true },
  { id: 'price_desc', label: 'Price: high to low', needsJob: false, available: true },
  { id: 'vram', label: 'Video RAM', needsJob: false, available: true },
  { id: 'vram_per_dollar', label: 'Video RAM per $/h', needsJob: false, available: true },
  { id: 'reliability', label: 'Reliability', needsJob: false, available: true },
];

function adaptSorts(raw: unknown): SortOption[] {
  const out: SortOption[] = [];
  for (const s of arr(raw).map(rec)) {
    const id = str(s.id);
    if (id === '') continue;
    out.push({ id, label: str(s.label, id), needsJob: s.needs_estimate === true || s.needsJob === true, available: s.available !== false });
  }
  return out.length ? out : SORTS_FALLBACK.map((s) => ({ ...s }));
}

function providerNames(p: Rec): Map<string, string> {
  const m = new Map<string, string>();
  for (const x of arr(p.providers).map(rec)) {
    const id = str(x.id) || str(x.name);
    const label = str(x.display_name) || str(x.label);
    if (id !== '' && label !== '') m.set(id, label);
  }
  return m;
}

function adaptProviders(p: Rec, names: ReadonlyMap<string, string>): OffersResult['providers'] {
  const errors = rec(p.errors);
  const out: OffersResult['providers'] = [];
  for (const x of arr(p.providers).map(rec)) {
    const id = str(x.id) || str(x.name);
    if (id === '' || x.enabled === false || x.implemented === false) continue;
    const err = str(errors[id]);
    const row: OffersResult['providers'][number] = { id, name: providerName(id, names), ok: err === '' };
    if (err !== '') row.error = err;
    out.push(row);
  }
  if (out.length) return out;
  // a payload without a provider list: the offers' own provider facet is all there is
  return arr(rec(p.facets).providers).map(rec).map((x) => {
    const name = str(x.name);
    return { id: name.toLowerCase(), name: providerName(name, names), ok: true };
  });
}

/** The four one-click choices the server picked. Roles that picked the same machine share one tile. */
function adaptRecs(p: Rec): Recommendation[] {
  const byRole = new Map<RecRole, string>();
  for (const h of arr(p.hardware_presets).map(rec)) {
    const role = ROLE_OF[str(h.id)];
    const offerId = str(h.offer_id);
    if (role !== undefined && offerId !== '') byRole.set(role, offerId);
  }
  for (const [key, v] of Object.entries(rec(p.presets))) {
    const role = ROLE_OF[key];
    const offerId = str(isRec(v) ? (v.offer_id ?? v.id) : v);
    if (role !== undefined && offerId !== '' && !byRole.has(role)) byRole.set(role, offerId);
  }
  const out: Recommendation[] = [];
  for (const role of ROLE_ORDER) {
    const offerId = byRole.get(role);
    if (offerId === undefined) continue;
    const same = out.find((r) => r.offerId === offerId);
    if (same) same.tags.push(ROLE_LABEL[role]);
    else out.push({ role, label: ROLE_LABEL[role], offerId, tags: [ROLE_LABEL[role]] });
  }
  return out;
}

/** The owner's limits as the payload states them (shape b), else the ones the app already has. */
function capsOf(raw: unknown, base: Caps | null): Caps | null {
  if (!isRec(raw)) return base;
  return {
    maxPrice: num(raw.max_price, base?.maxPrice ?? 0),
    jobCap: num(raw.job_cap, base?.jobCap ?? 0),
    monthCap: num(raw.month_cap, base?.monthCap ?? 0),
    monthToDate: num(raw.month_to_date, base?.monthToDate ?? 0),
    credit: numOf(raw.credit) ?? base?.credit ?? null,
    dcOnly: base?.dcOnly ?? false,
  };
}

/** A page of the market. `sceneGb` is the job's own video size (0 when browsing without a job). */
export function adaptOffers(payload: unknown, caps: Caps | null, sceneGb = 0): OffersResult {
  const p = rec(payload);
  const names = providerNames(p);
  const ctx: Ctx = { caps: capsOf(p.caps, caps), sceneGb, names };
  const offers = arr(p.offers).map(rec).map((w) => adaptOffer(w, ctx)).filter((o): o is Offer => o !== null);
  const total = num(p.total, offers.length);
  return {
    offers,
    total,
    all: Math.max(total, num(p.all, total)),
    offset: num(p.offset),
    facets: adaptFacets(p.facets, names),
    sorts: adaptSorts(p.sorts),
    recommendations: adaptRecs(p),
    providers: adaptProviders(p, names),
    truncated: p.truncated === true,
  };
}

/* ---------- quotes ---------- */

export interface QuoteMachine { id: string; provider: string; providerId: string; gpu: string; pricePerHour: number }
export interface QuoteReply {
  /** the whole selection's bill (scenes added up); null when nothing could be priced */
  quote: Quote | null;
  refusal: { code: string; message: string } | null;
  /** the machine the server priced */
  machine: QuoteMachine | null;
}

/** The answer of `GET api/cloud/quote` (one line per scene), or one quote line on its own (a 409 `price_changed` carries one). */
export function adaptQuote(payload: unknown, sceneGb = 0): QuoteReply {
  const p = rec(payload);
  const ctx: Ctx = { caps: null, sceneGb, names: new Map() };
  const lines = Array.isArray(p.quotes) ? arr(p.quotes).map(rec) : [isRec(p.quote) ? p.quote : p];
  const quotes: Quote[] = [];
  let refusal: QuoteReply['refusal'] = null;
  let machine: QuoteMachine | null = null;
  for (const line of lines) {
    const q = adaptEstimate(line, ctx);
    if (q) quotes.push(q);
    const r = rec(line.refusal);
    if (refusal === null && str(r.code) !== '') refusal = { code: str(r.code), message: str(r.message, 'This rental was refused.') };
    const o = rec(line.offer);
    if (machine === null && str(o.id) !== '') {
      const pid = str(o.provider);
      machine = { id: str(o.id), provider: providerName(pid), providerId: pid, gpu: cleanGpu(str(o.gpu)), pricePerHour: num(o.price_per_hour) };
    }
  }
  return { quote: sumQuotes(quotes, quotes.length > 1 ? numOf(p.total_usd) : null), refusal, machine };
}

/* ---------- jobs and status ---------- */

/** The server's paid-job fields -> what the views read. `cloud_label` is the machine's GPU (the server's own `cloud_label` names the quality plan),
 *  `provider` the provider's display name, `cloud_est_usd` the estimate. Safe to apply twice, and to a partial job. */
export function adaptJob<T extends Partial<Job>>(job: T): T {
  const w: Rec = job;
  if (w.cloud !== true) return job;
  const gpu = str(w.cloud_gpu);
  const out: Rec = { ...w, cloud_label: gpu !== '' ? cleanGpu(gpu) : null };
  if (!str(w.provider) && str(w.cloud_provider) !== '') out.provider = providerName(str(w.cloud_provider));
  if (numOf(w.cloud_est_usd) === null && numOf(w.cloud_est) !== null) out.cloud_est_usd = numOf(w.cloud_est);
  return out as T;
}

/** `GET api/cloud/status` and the `cloud` event -> the shape the views read: provider rows as {id, name, connected, balance, error}, the credit
 *  of the first provider that has one, and provider ids in the machine lists turned into names. Safe to apply twice. */
export function adaptStatus(raw: CloudStatus): CloudStatus {
  const p = rec(raw);
  const providers = arr(p.providers).map(rec).map((r) => {
    const id = str(r.id, str(r.name));
    return {
      id, name: str(r.display_name, str(r.name, providerName(id))), connected: typeof r.connected === 'boolean' ? r.connected : r.enabled === true,
      balance: numOf(r.balance) ?? numOf(r.credit), error: str(r.error, str(r.credit_error)) || null,
    };
  });
  const named = (rows: unknown): unknown[] => arr(rows).map(rec).map((r) => ({ ...r, provider: str(r.provider) !== '' ? providerName(str(r.provider)) : null }));
  return {
    ...raw,
    credit: numOf(p.credit) ?? providers.find((r) => r.balance !== null)?.balance ?? null,
    providers,
    active: named(p.active) as CloudStatus['active'],
    recent: named(p.recent) as CloudStatus['recent'],
  };
}
