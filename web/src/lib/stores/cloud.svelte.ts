/* The GPU rental market, seen by the tray, the picker and the all-machines browser.

   The tray calls `ensure()` once a cloud run is on the table and reads `picked` / `blocked` / `state`.
   The picker shows `recs`; the browser pages through `rows`. Every request is keyed by what was asked
   (job, limits, filters, sort): a late answer for an old question is dropped, never shown, and a
   question asked a moment ago is answered from memory first. */
import { untrack } from 'svelte';
import { drop, peek, swr } from '../api/cache';
import { ApiError, isAbort } from '../api/client';
import { PAGE, fetchOffers, type MarketQuery } from '../api/cloud';
import { ROLE_LABEL, SORTS_FALLBACK } from '../api/cloudAdapter';
import type { Caps, Offer, OfferFacets, OfferFilters, OffersResult, Quote, RecRole, Recommendation, SortOption } from '../api/types';
import { OPS } from '../choices';
import { budget } from './budget.svelte';
import { options } from './options.svelte';
import { selection } from './selection.svelte';
import { toast } from './toast.svelte';

/** The machine the tray would run on, with the server's price for this job. */
export interface Pick { offer: Offer; quote: Quote }
export type CloudState = 'idle' | 'loading' | 'ready' | 'empty' | 'error';
export type BrowseState = 'idle' | 'loading' | 'ready' | 'error';
export type StepKey = 'disk' | 'rel' | 'days' | 'cuda' | 'drv';

/** the values the filter rail's steppers walk through; 0 = no minimum */
export const STEPS: Readonly<Record<StepKey, readonly number[]>> = {
  disk: [0, 100, 250, 500, 1000],
  rel: [0, 95, 97, 98, 99, 99.5],
  days: [0, 7, 14, 30, 90],
  cuda: [0, 12.0, 12.4, 12.6, 12.8],
  drv: [0, 535, 550, 560, 570],
};
export const DEFAULT_FILTERS: Readonly<OfferFilters> = {
  model: 'any', count: 'any', region: 'any', disk: 0, rel: 0, days: 0, cuda: 0, drv: 0,
  unverified: true, cantRun: true, rented: false, dcOnly: false, providers: null,
};

const TTL = 15_000;
const DEBOUNCE = 250;
const PICK_PREFIX = 'cloud:pick:';
const ROWS_PREFIX = 'cloud:rows:';
const SEVERAL = 'This server prices one scene at a time. Pick a single scene to rent a GPU.';

interface Wanted { id: string; provider?: string }
interface PickerData {
  key: string;
  offers: readonly Offer[];
  recs: readonly Recommendation[];
  /** every machine the owner's switches allow, for "Browse all N" */
  all: number;
  unpriced: boolean;
  providers: OffersResult['providers'];
  at: number;
}
interface RowsData {
  offers: readonly Offer[];
  total: number;
  all: number;
  facets: OfferFacets;
  sorts: SortOption[];
  providers: OffersResult['providers'];
  unpriced: boolean;
  /** which menus these facets may feed (they are counted over the filtered set) */
  modelAny: boolean;
  countAny: boolean;
  at: number;
}

function describe(e: unknown): { message: string; code: string | null } {
  if (e instanceof ApiError) return { message: e.message, code: e.code ?? null };
  return { message: e instanceof Error ? e.message : 'Could not load GPU offers', code: null };
}

/** The mockup's four picks, over the machines the server did price: used when the server names no usable recommendation. */
function rankLocally(offers: readonly Offer[]): Recommendation[] {
  const ok = offers.filter((o) => o.count === 1 && o.compatible && !o.rented && o.estimate !== null);
  const total = (o: Offer): number => o.estimate?.total ?? 0;
  const by = (f: (o: Offer) => number, pool: readonly Offer[] = ok): Offer[] => pool.slice().sort((a, b) => f(a) - f(b));
  const roles: [RecRole, Offer[]][] = [
    ['value', by((o) => total(o) * Math.pow(o.estimate?.hours ?? 1, 0.6), ok.filter((o) => o.reliability >= 98.5))],
    ['fast', by((o) => o.estimate?.minutes ?? 0)],
    ['cheap', by(total)],
    ['reliable', by((o) => total(o) - o.reliability * 1000)],
  ];
  const used = new Set<string>();
  const out: Recommendation[] = [];
  for (const [role, list] of roles) {
    const o = list.find((x) => !used.has(x.id));
    if (!o) continue;
    used.add(o.id);
    out.push({ role, label: ROLE_LABEL[role], offerId: o.id, tags: [ROLE_LABEL[role]] });
  }
  return out;
}

/** One page for the picker, plus the machines it needs that are not on that page (a recommendation, the one picked before). */
async function loadPickerData(key: string, q: MarketQuery, ctx: { caps: Caps | null; sceneGb: number }, chosen: Wanted | null, signal: AbortSignal): Promise<PickerData> {
  const { result, unpriced } = await fetchOffers(q, ctx, signal);
  const offers = [...result.offers];
  if (unpriced) return { key, offers, recs: [], all: result.total, unpriced, providers: result.providers, at: Date.now() };

  const have = new Set(offers.map((o) => o.id));
  const want: Wanted[] = [];
  const add = (w: Wanted): void => { if (!have.has(w.id) && !want.some((x) => x.id === w.id)) want.push(w); };
  for (const r of result.recommendations) add({ id: r.offerId });
  if (chosen) add(chosen);
  const found = await Promise.all(want.slice(0, 5).map(async (w) => {
    try {
      return (await fetchOffers({ ...q, mode: 'lookup', only: w }, ctx, signal)).result.offers[0] ?? null;
    } catch (e) {
      if (isAbort(e)) throw e;
      return null;
    }
  }));
  for (const o of found) if (o) offers.push(o);

  const byId = new Map(offers.map((o) => [o.id, o]));
  let recs = result.recommendations.filter((r) => {
    const o = byId.get(r.offerId);
    return o !== undefined && o.compatible && !o.rented && o.estimate !== null;
  });
  if (recs.length === 0) recs = rankLocally(offers);
  return { key, offers, recs, all: result.total, unpriced, providers: result.providers, at: Date.now() };
}

class CloudStore {
  /* ---------- what the tray reads ---------- */

  /** why there is no list: a sentence for the person */
  error = $state<string | null>(null);
  /** the server's code for it (`no_key`: the cloud settings need a key) */
  errorCode = $state<string | null>(null);
  /** the machine picked in the browser, kept whole so it still shows when it is off the picker's page */
  chosen = $state.raw<{ offer: Offer; key: string } | null>(null);

  #phase = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
  #pick = $state.raw<PickerData | null>(null);
  #pickKey = '';
  #stopPick: (() => void) | null = null;
  #wanted = $state(false);
  #lastSync = 0;
  #announce = false;
  #dcPending = $state<boolean | null>(null);

  /** the job as the server prices it; null when nothing is selected or the operation cannot run in the cloud */
  #job = $derived.by<{ ids: string[]; key: string } | null>(() => {
    if (!OPS[options.op].cloud || selection.count === 0) return null;
    const ids = selection.ids;
    return { ids, key: `${options.op}|${options.q}|${ids.join(',')}` };
  });
  /** only the limits that decide which machines can run the job; dollars are floored so a running rental's spend does not refetch every poll */
  #capsKey = $derived.by(() => {
    const c = budget.caps;
    return c ? `${c.maxPrice}|${c.jobCap}|${Math.floor(c.monthCap - c.monthToDate)}|${Math.floor(c.credit ?? -1)}` : 'none';
  });
  #pickerKey = $derived(`${this.#job?.key ?? 'none'}#${this.#capsKey}#${this.dcOnly ? 1 : 0}`);

  /** data centres only: the saved cloud setting, shown at once while it saves */
  get dcOnly(): boolean { return this.#dcPending ?? budget.caps?.dcOnly ?? false; }

  #offer = $derived.by<Offer | null>(() => {
    const d = this.#pick;
    if (!d) return null;
    const id = options.offerId;
    if (id === null) {
      const first = d.recs[0];
      return first ? (d.offers.find((o) => o.id === first.offerId) ?? null) : null;
    }
    const c = this.chosen;
    return d.offers.find((o) => o.id === id) ?? (c && c.offer.id === id && c.key === d.key ? c.offer : null);
  });

  state: CloudState = $derived.by(() => {
    if (this.#phase !== 'ready') return this.#phase;
    return this.#pick && (this.#pick.recs.length > 0 || options.offerId !== null) ? 'ready' : 'empty';
  });

  /** the machine the run would use (the one picked, else the best value) with the server's price; null until there is a price */
  picked: Pick | null = $derived.by(() => {
    const o = this.#offer;
    return o && o.estimate ? { offer: o, quote: o.estimate } : null;
  });

  /** why the run cannot start with this machine, or null */
  blocked: string | null = $derived.by(() => {
    if (budget.settings && !budget.connected) return 'Add your Vast.ai key in Cloud settings first';
    if (this.state !== 'ready') return null;
    const o = this.#offer;
    if (!o) return 'That machine is no longer available. Pick another.';
    if (o.rented) return 'That machine is rented right now. Pick another.';
    if (!o.estimate) return 'No price for this machine yet';
    return o.compatible ? null : (o.incompatibleReason ?? 'This machine can’t run this job');
  });

  /* ---------- what the picker shows ---------- */

  get recs(): readonly Recommendation[] { return this.#pick?.recs ?? []; }
  get pickerOffers(): readonly Offer[] { return this.#pick?.offers ?? []; }
  get pickerAll(): number { return this.#pick?.all ?? 0; }
  get pickerAt(): number { return this.#pick?.at ?? 0; }
  get pickerProviders(): OffersResult['providers'] { return this.#pick?.providers ?? []; }

  /* ---------- the browser ---------- */

  filters = $state<OfferFilters>({ ...DEFAULT_FILTERS });
  sort = $state('auto');
  rows = $state.raw<readonly Offer[]>([]);
  rowsTotal = $state(0);
  rowsAll = $state(0);
  facets = $state.raw<OfferFacets>({ gpus: [], counts: [], regions: [], providers: [] });
  sorts = $state.raw<readonly SortOption[]>(SORTS_FALLBACK);
  providers = $state.raw<OffersResult['providers']>([]);
  browseState = $state<BrowseState>('idle');
  browseError = $state<string | null>(null);
  browseCode = $state<string | null>(null);
  /** what is on screen answers an older question; the new answer is on its way */
  refreshing = $state(false);
  /** a further page is on its way */
  paging = $state(false);
  moreFailed = $state(false);
  updatedAt = $state(0);
  /** counts up whenever the list is replaced by a different question's answer (the list scrolls back to the top) */
  rowsEpoch = $state(0);
  /** the server cannot price several scenes together yet (the list has no estimates) */
  unpriced = $state(false);

  #browseWanted = $state(false);
  #browseJob = $state(false);
  #rowsKey = '';
  #rowsBase = '';
  #appliedKey = '';
  #stopRows: (() => void) | null = null;
  #moreCtl: AbortController | null = null;
  #lastBrowse = 0;
  #allSeen = 0;
  #gpuMemo = $state.raw<OfferFacets['gpus']>([]);
  #countMemo = $state.raw<OfferFacets['counts']>([]);

  /** the browser prices a job (a Rent page browses without one) */
  jobActive = $derived(this.#browseJob && this.#job !== null && !this.unpriced);
  #browseBase = $derived(`${this.#browseJob ? (this.#job?.key ?? 'none') : 'free'}#${this.#capsKey}#${this.dcOnly ? 1 : 0}`);
  #browseKey = $derived.by(() => {
    const f = this.filters;
    return JSON.stringify([this.#browseBase, this.sort, f.model, f.count, f.region, f.disk, f.rel, f.days, f.cuda, f.drv, f.unverified, f.rented, f.providers]);
  });

  /** rows with the can't-run ones removed when that chip is on */
  visible: readonly Offer[] = $derived(this.#browseJob && !this.filters.cantRun ? this.rows.filter((o) => o.compatible) : this.rows);
  /** loaded rows that cannot run this job */
  blockedCount: number = $derived(this.#browseJob ? this.rows.reduce((n, o) => n + (o.compatible ? 0 : 1), 0) : 0);
  /** the filters that differ from their defaults, as the filter button counts them */
  activeCount: number = $derived.by(() => {
    const f = this.filters;
    return Number(f.disk > 0) + Number(f.rel > 0) + Number(f.days > 0) + Number(f.cuda > 0) + Number(f.drv > 0) + Number(this.dcOnly)
      + Number(!f.unverified) + Number(this.#browseJob && !f.cantRun) + Number(f.rented);
  });
  /** the model and count menus keep listing every choice after one is picked (the server counts over what is left) */
  modelFacets: OfferFacets['gpus'] = $derived(this.filters.model === 'any' || this.#gpuMemo.length === 0 ? this.facets.gpus : this.#gpuMemo);
  countFacets: OfferFacets['counts'] = $derived(this.filters.count === 'any' || this.#countMemo.length === 0 ? this.facets.counts : this.#countMemo);

  constructor() {
    $effect.root(() => {
      $effect(() => {
        if (!this.#wanted) return;
        const key = this.#pickerKey;
        untrack(() => this.#syncPicker(key, false));
      });
      $effect(() => {
        if (!this.#browseWanted) return;
        const key = this.#browseKey;
        untrack(() => this.#syncBrowse(key, false));
      });
    });
  }

  /* ---------- picker ---------- */

  /** Start (or keep) loading the picker's list for the current job. Safe to call again and again: only a changed question asks the server. */
  ensure(): void {
    untrack(() => {
      this.#wanted = true;
      this.#syncPicker(this.#pickerKey, false);
    });
  }

  /** Take a machine for the run. The whole offer is kept, so it still shows when the picker's page changes. */
  choose(offer: Offer): void {
    this.chosen = { offer, key: this.#pickerKey };
    options.offerId = offer.id;
  }

  /** Ask again, and keep showing what is on screen until the answer lands. */
  refresh(): void {
    drop('cloud:');
    this.#announce = true;
    if (this.#wanted) this.#syncPicker(this.#pickerKey, true);
    if (this.#browseWanted) this.#syncBrowse(this.#browseKey, true);
  }

  #syncPicker(key: string, force: boolean): void {
    if (!this.#job) {
      this.#stopPick?.();
      this.#pickKey = '';
      this.#pick = null;
      this.#phase = 'idle';
      this.error = null;
      this.errorCode = null;
      return;
    }
    if (!force && key === this.#pickKey) return;
    this.#stopPick?.();
    const same = key === this.#pickKey;
    this.#pickKey = key;
    const hit = force ? undefined : peek<PickerData>(PICK_PREFIX + key);
    if (hit) this.#showPicker(hit);
    else if (!same || !this.#pick) {
      this.#pick = null;
      this.#phase = 'loading';
      this.error = null;
      this.errorCode = null;
    }
    if (hit && Date.now() - hit.at < TTL) return;
    const quick = Date.now() - this.#lastSync > 1000;
    this.#lastSync = Date.now();
    let stop: (() => void) | null = null;
    const timer = setTimeout(() => { stop = this.#loadPicker(key); }, quick || force ? 0 : DEBOUNCE);
    this.#stopPick = () => { clearTimeout(timer); stop?.(); };
  }

  #loadPicker(key: string): () => void {
    const q = this.#query('picker', { limit: 50 });
    const ctx = { caps: budget.caps, sceneGb: selection.info.gb };
    const id = options.offerId;
    const want: Wanted | null = id === null ? null : { id, provider: this.chosen?.offer.id === id ? this.chosen.offer.providerId : undefined };
    return swr<PickerData>(
      PICK_PREFIX + key,
      (signal) => loadPickerData(key, q, ctx, want, signal),
      (d, stale) => { if (!stale && this.#pickKey === key) { this.#showPicker(d); this.#toastRefreshed(); } },
      { ttlMs: 0, onError: (e) => { if (this.#pickKey === key) this.#failPicker(e); } },
    );
  }

  #showPicker(d: PickerData): void {
    this.#pick = d;
    this.errorCode = null;
    this.error = d.unpriced ? SEVERAL : null;
    this.#phase = d.unpriced ? 'error' : 'ready';
  }

  #failPicker(e: unknown): void {
    this.#announce = false;
    if (this.#pick?.key === this.#pickKey) return; // keep showing the last good answer
    const { message, code } = describe(e);
    this.#pick = null;
    this.#phase = 'error';
    this.error = message;
    this.errorCode = code;
  }

  #toastRefreshed(): void {
    if (!this.#announce) return;
    this.#announce = false;
    toast.show('Offers refreshed', 1600);
  }

  /* ---------- browser ---------- */

  /** The browser is open: load its list for the current filters, and keep it right while they change. `job` = price machines for the selected scenes. */
  ensureBrowse(job: boolean): void {
    untrack(() => {
      this.#browseWanted = true;
      this.#browseJob = job;
      this.#syncBrowse(this.#browseKey, false);
    });
  }

  /** The browser closed: stop asking. */
  releaseBrowse(): void {
    this.#browseWanted = false;
    this.#stopRows?.();
    this.#moreCtl?.abort();
    this.#rowsKey = '';
    this.paging = false;
  }

  /** A machine by id, wherever it is loaded: the browser's rows, the picker's page, or the one picked. */
  offerById(id: string): Offer | null {
    return this.rows.find((o) => o.id === id) ?? this.#pick?.offers.find((o) => o.id === id) ?? (this.chosen?.offer.id === id ? this.chosen.offer : null);
  }

  setFilter<K extends keyof OfferFilters>(k: K, v: OfferFilters[K]): void {
    this.filters[k] = v;
  }

  /** one stepper press: the next value of that filter's steps, clamped */
  step(k: StepKey, dir: -1 | 1): void {
    const steps = STEPS[k];
    const i = Math.max(0, Math.min(steps.length - 1, steps.indexOf(this.filters[k]) + dir));
    this.filters[k] = steps[i];
  }

  /** a provider chip: on = shown. Every chip off means an empty list, as the mockup does. */
  toggleProvider(id: string): void {
    const all = this.providers.map((p) => p.id ?? p.name);
    const now = this.filters.providers ?? all;
    const next = now.includes(id) ? now.filter((x) => x !== id) : [...now, id];
    this.filters.providers = next.length === all.length ? null : next;
  }

  resetFilters(): void {
    this.filters = { ...DEFAULT_FILTERS };
    if (this.dcOnly) void this.setDcOnly(false);
  }

  /** Save "data centres only" (it is a cloud setting, shared with Cloud settings). Shown at once; put back with a note if the server refuses. */
  async setDcOnly(v: boolean): Promise<void> {
    this.#dcPending = v;
    try {
      await budget.save({ datacenter_only: v });
    } catch (e) {
      toast.show(e instanceof Error ? e.message : 'Could not save that setting');
    } finally {
      this.#dcPending = null;
    }
  }

  /** the next page, appended; the list asks for it as the window nears its end */
  loadMore(retry = false): void {
    if (this.paging || this.refreshing || this.browseState !== 'ready' || this.rows.length >= this.rowsTotal) return;
    if (this.moreFailed && !retry) return;
    const key = this.#rowsKey;
    const q = this.#browseQuery(this.rows.length);
    const ctx = { caps: budget.caps, sceneGb: selection.info.gb };
    this.moreFailed = false;
    this.paging = true;
    this.#moreCtl?.abort();
    const ctl = new AbortController();
    this.#moreCtl = ctl;
    fetchOffers(q, ctx, ctl.signal).then(
      ({ result }) => {
        if (ctl.signal.aborted || this.#rowsKey !== key) return;
        const seen = new Set(this.rows.map((o) => o.id));
        const fresh = result.offers.filter((o) => !seen.has(o.id));
        // a page that adds nothing means the server's total was an estimate: stop asking
        if (fresh.length === 0) this.rowsTotal = this.rows.length;
        else this.rows = [...this.rows, ...fresh];
        this.paging = false;
      },
      (e) => {
        if (isAbort(e) || ctl.signal.aborted || this.#rowsKey !== key) return;
        this.paging = false;
        this.moreFailed = true;
      },
    );
  }

  #query(mode: MarketQuery['mode'], over: Partial<MarketQuery> = {}): MarketQuery {
    return {
      mode, scenes: this.#job?.ids ?? [], op: options.op, quality: options.q, size: options.size, sort: 'auto',
      filters: { ...$state.snapshot(this.filters), dcOnly: this.dcOnly }, ...over,
    };
  }

  #browseQuery(offset: number): MarketQuery {
    // a job-only sort needs a job; without one the server's own order is used
    const sort = this.jobActive || !this.sort.startsWith('est_') ? this.sort : 'auto';
    return this.#query('browser', { scenes: this.jobActive ? (this.#job?.ids ?? []) : [], sort, limit: PAGE, offset });
  }

  #syncBrowse(key: string, force: boolean): void {
    if (!force && key === this.#rowsKey) return;
    this.#stopRows?.();
    this.#moreCtl?.abort();
    const base = this.#browseBase;
    const sameBase = base === this.#rowsBase;
    this.#rowsKey = key;
    this.#rowsBase = base;
    this.paging = false;
    this.moreFailed = false;
    if (!sameBase) {
      this.#allSeen = 0;
      this.#appliedKey = '';
      this.rows = [];
      this.rowsTotal = 0;
      this.unpriced = false;
      this.browseState = 'loading';
      this.browseError = null;
      this.browseCode = null;
    }
    const hit = force ? undefined : peek<RowsData>(ROWS_PREFIX + key);
    if (hit) this.#applyRows(hit, key);
    else if (this.rows.length > 0) this.refreshing = true;
    else this.browseState = 'loading';
    if (hit && Date.now() - hit.at < TTL) return;
    const quick = Date.now() - this.#lastBrowse > 1000;
    this.#lastBrowse = Date.now();
    let stop: (() => void) | null = null;
    const timer = setTimeout(() => { stop = this.#loadRows(key); }, quick || force ? 0 : DEBOUNCE);
    this.#stopRows = () => { clearTimeout(timer); stop?.(); };
  }

  #loadRows(key: string): () => void {
    const q = this.#browseQuery(0);
    const ctx = { caps: budget.caps, sceneGb: selection.info.gb };
    if (q.filters.providers !== null && q.filters.providers.length === 0) {
      // every provider is switched off: there is nothing to ask
      this.#applyRows({ offers: [], total: 0, all: 0, facets: this.facets, sorts: this.sorts.slice(), providers: this.providers, unpriced: false, modelAny: false, countAny: false, at: Date.now() }, key);
      return () => {};
    }
    return swr<RowsData>(
      ROWS_PREFIX + key,
      async (signal) => {
        const { result, unpriced } = await fetchOffers(q, ctx, signal);
        return {
          offers: result.offers, total: result.total, all: result.all, facets: result.facets, sorts: result.sorts, providers: result.providers, unpriced,
          modelAny: q.filters.model === 'any', countAny: q.filters.count === 'any', at: Date.now(),
        };
      },
      (d, stale) => { if (!stale && this.#rowsKey === key) { this.#applyRows(d, key); this.#toastRefreshed(); } },
      { ttlMs: 0, onError: (e) => { if (this.#rowsKey === key) this.#failRows(e); } },
    );
  }

  #applyRows(d: RowsData, key: string): void {
    if (key !== this.#appliedKey) this.rowsEpoch += 1;
    this.#appliedKey = key;
    this.rows = d.offers;
    this.rowsTotal = d.total;
    // the real server counts only what matches; the mock sends the pool size. Remember the largest total seen for this job and these limits.
    this.#allSeen = d.all > d.total ? d.all : Math.max(d.total, this.#allSeen);
    this.rowsAll = this.#allSeen;
    this.facets = d.facets;
    if (d.modelAny) this.#gpuMemo = d.facets.gpus;
    if (d.countAny) this.#countMemo = d.facets.counts;
    this.sorts = d.sorts;
    this.providers = d.providers;
    this.unpriced = d.unpriced;
    this.updatedAt = d.at;
    this.refreshing = false;
    this.browseState = 'ready';
    this.browseError = null;
    this.browseCode = null;
  }

  #failRows(e: unknown): void {
    this.#announce = false;
    const { message, code } = describe(e);
    this.refreshing = false;
    this.browseError = message;
    this.browseCode = code;
    if (this.rows.length === 0) this.browseState = 'error';
  }
}

export const cloud = new CloudStore();
