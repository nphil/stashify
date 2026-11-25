/* The rental market from the app's side: a page of offers, one machine's price for a job.
   The server is strict (an unknown query parameter is a 400), so everything sent here is a parameter it knows;
   the two exceptions are asked of it politely: `scene_ids` (several scenes as one job) and `rented`. */
import { ApiError, get, type Query } from './client';
import { adaptOffers, adaptQuote, countriesOf, type QuoteReply } from './cloudAdapter';
import type { Caps, OfferFilters, OfferQuery, OffersResult, OpId, QualityId } from './types';

/** rows per request (the server allows 200) */
export const PAGE = 100;

/** the server's names for the tray's three cloud operations */
const OP_WIRE: Readonly<Record<OpId, string | null>> = { decensor: 'decensor', decensor_up: 'decensor+upscale', upscale: 'upscale', transcode: null };
/** the server prices by preset; High and Max are the same preset there */
const PRESET_WIRE: Readonly<Record<QualityId, string>> = { fast: 'close-ups', balanced: 'detail', high: 'detail-close-ups', max: 'detail-close-ups' };

export interface MarketQuery extends OfferQuery {
  /** picker: the server's own defaults and the owner's limits; browser: every filter shown, limits as can't-run rows; lookup: one machine */
  mode: 'picker' | 'browser' | 'lookup';
  /** lookup: the machine wanted */
  only?: { id: string; provider?: string };
}
export interface MarketReply {
  result: OffersResult;
  /** the server cannot price several scenes together yet, so these offers carry no estimates */
  unpriced: boolean;
}

/** an old server answers 400 to `scene_ids`; once seen, several scenes are no longer asked for */
let perSceneOnly = false;
/** the server said it can list rented machines (it sent that facet), so `rented` is a parameter it knows */
let rentedKnown = false;

/** Show everything the filters allow: the server's own limits would hide the machines the owner can't afford,
 *  and those are shown as can't-run rows with their reason. */
const NO_LIMITS: Query = { max_price: 1000, max_bw_cost: 'any' };

function jobParams(q: OfferQuery): Query {
  const op = OP_WIRE[q.op];
  const ids = q.scenes;
  if (op === null || ids.length === 0 || (ids.length > 1 && perSceneOnly)) return {};
  const p: Query = { op };
  if (q.op !== 'upscale') p.preset = PRESET_WIRE[q.quality];
  if (ids.length === 1) p.scene = ids[0];
  else p.scene_ids = ids;
  return p;
}

function filterParams(f: OfferFilters): Query {
  const p: Query = {
    ...NO_LIMITS,
    min_reliability: f.rel > 0 ? f.rel / 100 : 0,
    verified: f.unverified ? 'any' : 1,
    datacenter_only: f.dcOnly,
  };
  if (f.model !== 'any') p.gpu = f.model;
  if (f.count !== 'any') { p.num_gpus = f.count; p.max_gpus = f.count; }
  if (f.region !== 'any') p.countries = countriesOf(f.region);
  if (f.disk > 0) p.min_disk_gb = f.disk;
  if (f.days > 0) p.min_duration_hours = f.days * 24;
  if (f.cuda > 0) p.min_cuda = f.cuda;
  if (f.drv > 0) p.min_driver = f.drv;
  if (f.providers !== null && f.providers.length === 1) p.provider = f.providers[0];
  if (f.rented && rentedKnown) p.rented = 1;
  return p;
}

/** The query string of an offers request. Exported so the store can key its cache on what is really asked. */
export function offerParams(q: MarketQuery): Query {
  const base: Query = { sort: q.sort, limit: q.limit ?? PAGE, offset: q.offset ?? 0, ...jobParams(q) };
  if (q.mode === 'picker') return base;
  if (q.mode === 'lookup' && q.only) return { ...base, ...NO_LIMITS, verified: 'any', min_reliability: 0, limit: 1, offset: 0, offer_id: q.only.id, provider: q.only.provider };
  return { ...base, ...filterParams(q.filters) };
}

/** A page of offers, adapted. `ctx.sceneGb` is the job's video size, to split what the machine downloads. */
export async function fetchOffers(q: MarketQuery, ctx: { caps: Caps | null; sceneGb: number }, signal?: AbortSignal): Promise<MarketReply> {
  const several = q.scenes.length > 1;
  const call = async (): Promise<OffersResult> => {
    const payload = await get<unknown>('api/cloud/offers', { query: offerParams(q), signal });
    return adaptOffers(payload, ctx.caps, ctx.sceneGb);
  };
  try {
    const result = await call();
    if (result.facets.rented !== undefined) rentedKnown = true;
    return { result, unpriced: several && perSceneOnly };
  } catch (e) {
    if (several && !perSceneOnly && e instanceof ApiError && e.status === 400 && e.message.includes('scene_ids')) {
      perSceneOnly = true;
      return { result: await call(), unpriced: true };
    }
    throw e;
  }
}

export interface QuoteRequest {
  scenes: readonly string[];
  op: OpId;
  quality: QualityId;
  offerId: string;
  providerId?: string;
  /** GB of the job's own video, to tell the upload from setup and models */
  sceneGb: number;
}

/** What this machine will cost for these scenes, and whether the server would accept the run (its refusal comes with the code). */
export async function fetchQuote(r: QuoteRequest, signal?: AbortSignal): Promise<QuoteReply> {
  const query: Query = {
    scenes: r.scenes,
    op: OP_WIRE[r.op],
    preset: r.op === 'upscale' ? null : PRESET_WIRE[r.quality],
    offer_id: r.offerId,
    provider: r.providerId,
  };
  return adaptQuote(await get<unknown>('api/cloud/quote', { query, signal }), r.sceneGb);
}
