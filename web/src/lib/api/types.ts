/* Shared types. "Wire" types mirror what the Python server sends today (snake_case);
   "UI" types (Offer, Quote, ...) are what views use. cloudAdapter.ts converts wire -> UI. */

/* ---------- choices ---------- */
export type OpId = 'decensor' | 'decensor_up' | 'upscale' | 'transcode';
export type QualityId = 'fast' | 'balanced' | 'high' | 'max';
export type SizeId = 'keep' | '1080' | '720';
export type DenoiseId = 'off' | 'low' | 'medium' | 'high';
export type RtxId = 'off' | '2' | '4';
export type DetectId = 'fast' | 'accurate';
/** `options.where` is a runner url, this, or null (= first online runner). */
export const WHERE_CLOUD = 'cloud';

/* ---------- library ---------- */
export interface SceneFile { path: string; width: number; height: number; duration: number; size: number }
export interface Scene {
  id: string;
  title: string | null;
  date?: string | null;
  files: SceneFile[];
  studio?: { name: string } | null;
  tags: { name: string }[];
  /** mock mode only: which neutral gradient (1..6) the thumbnail uses */
  tn?: number;
}
export interface ScenesPage { count: number; scenes: Scene[] }
export interface ScenesQuery {
  q?: string; page?: number; per_page?: number;
  sort?: 'date' | 'created_at' | 'title' | 'file_mod_time' | 'random';
  dir?: 'asc' | 'desc'; tag?: string;
  /** with sort=random: the same seed gives the same order on every page */
  seed?: number;
}
export interface Tag { id: string; name: string; scene_count: number }
/** What the tray needs to know about the selected scenes. dur and eff are seconds; gb is gigabytes. */
export interface SceneInfo { n: number; dur: number; eff: number; gb: number }

/* ---------- jobs ---------- */
export type JobState = 'queued' | 'running' | 'review_ready' | 'replacing' | 'replaced' | 'discarding' | 'discarded' | 'cancelled' | 'error';
export const ACTIVE_STATES: readonly JobState[] = ['queued', 'running', 'replacing', 'discarding'];
export interface GpuStats { util: number; mem_used: number; mem_total: number; temp: number; power: number }
export interface JobSegment { n: number; start: number; end: number }
export interface Job {
  id: string;
  scene_id: string;
  state: JobState;
  message?: string | null;
  error?: string | null;
  /** 0..1 */
  progress: number;
  paused?: boolean;
  indeterminate?: boolean;
  backend: 'decensor' | 'upscale' | 'transcode';
  runner?: string | null;
  engine?: string | null;
  cloud?: boolean;
  cloud_label?: string | null;
  cloud_usd?: number | null;
  cloud_cap?: number | null;
  stage?: string | null;
  frame?: number | null;
  total_frames?: number | null;
  fps?: number | null;
  /** seconds */
  eta?: number | null;
  elapsed?: number | null;
  /** the one live status: what it is doing in plain words, and where it is in its steps and passes */
  phase?: string | null;
  step?: number | null;
  steps?: number | null;
  /** short, e.g. "encode" or "3.2 of 8.1 GB" */
  detail?: string | null;
  /** pass 1 of 2 (decensor, then upscale); only sent for a job with more than one */
  pass_no?: number | null;
  passes?: number | null;
  /** the progress or the time left is a guess (a pass that has not run yet, a clock-based bar) */
  estimate?: boolean;
  /** epoch seconds of the job's last sign of life */
  updated_at?: number | null;
  /** seconds since that; only while it runs locally (the server's clock, so no skew) */
  idle_s?: number | null;
  /** client clock (ms) when this snapshot arrived: idle_s keeps counting from it between updates */
  rx?: number;
  log_cursor?: number;
  gpu_stats?: GpuStats | null;
  preview?: boolean;
  segments?: JobSegment[];
  sample?: boolean;
  review_scene_id?: string | null;
  /** proposed additions (see BACKEND_NEEDS): the UI uses them when present */
  scene_title?: string | null;
  quality?: QualityId | null;
  op?: OpId | null;
  created?: number | null;
  /** proposed: cloud rental phases, in order, and the current one */
  cloud_phase?: 'boot' | 'upload' | 'run' | 'download' | 'cleanup' | null;
  /** proposed: provider name of the rented machine, shown on the Review page */
  provider?: string | null;
  /** proposed: what the whole rental is expected to cost (shown as "so far, of about …") */
  cloud_est_usd?: number | null;
  /** proposed: kind of rented machine (data centre or community), and bytes sent to it so far */
  cloud_tier?: 'dc' | 'comm' | null;
  sent_bytes?: number | null;
  /** proposed: facts about the finished preview for the Review page */
  result?: JobResult | null;
}
/** proposed: what a finished job produced (heights in pixels, bytes, duration in seconds) */
export interface JobResult { from_height?: number; to_height?: number; from_bytes?: number; to_bytes?: number; duration?: number }
export type LogLevel = 'event' | 'info' | 'warn' | 'error' | 'proc';
/** `live` marks a progress-bar line: the server keeps only the newest one */
export interface JobLogLine { seq: number; t: number | string; level: LogLevel; text: string; live?: boolean }
export interface JobLog { cursor: number; lines: JobLogLine[] }

/* ---------- runners ---------- */
export interface Runner {
  name: string;
  url: string;
  ops: string[];
  engines: Record<string, string>;
  /** the ops this runner is preferred for (empty = no preference) */
  prefer?: string[] | null;
  source: 'env' | 'manual' | 'discovered';
  online: boolean;
  busy?: boolean;
  node?: string | null;
  kind?: string | null;
  note?: string | null;
  paused?: boolean;
  /* proposed additions (see BACKEND_NEEDS); the UI works without them */
  gpu?: string | null;
  vram_gb?: number | null;
  /** speed relative to an RTX 3080 (= 1) */
  speed?: number | null;
  asleep?: boolean;
  can_wake?: boolean;
  wake_eta_s?: number | null;
  queue_eta_s?: number | null;
  qualities?: QualityId[] | null;
}
export interface GpuMeter { name: string; url: string; online: boolean; active?: boolean; gpu?: GpuStats | null }
export interface Health { ok: boolean; gpu?: string | null; backend?: string | null; postUpscale?: boolean; runner?: string | null; lada?: boolean; gpu_stats?: GpuStats | null }
export interface DiscoveredRunner { name: string; url: string; ops: string[]; engines: Record<string, string>; kind?: string; registered: boolean }
export interface RoutePreview { runner?: string; engine?: string; error?: string }

/* ---------- submit ---------- */
export interface SubmitBody {
  scene_id: string;
  backend: 'decensor' | 'upscale' | 'transcode';
  detection_model?: 'v4-fast' | 'v4-accurate';
  post_upscale?: boolean;
  transcode_height?: '1080' | '720';
  engine?: string;
  runner?: string;
  denoise?: 'low' | 'medium' | 'high';
  restoration?: 'basicvsrpp' | 'ltx';
  ltx_auto_split?: number;
  secondary?: 'rtx-super-res';
  rtx_quality?: 'high' | 'ultra';
  rtx_scale?: '2' | '4';
  rtx_denoise?: string;
  rtx_deblur?: string;
  preview?: boolean;
  /* paid cloud */
  /** a paid run with no preset (cloud upscale): the server needs this flag to take the paid path */
  cloud?: boolean;
  cloud_preset?: string;
  offer_id?: string;
  /** the provider id of `offer_id` (`vast`, `runpod`): a pinned machine is looked up on its provider */
  provider?: string;
  confirm_usd?: number;
}

/* ---------- cloud: settings and status (wire) ---------- */
export interface SecretState { set: boolean; last4?: string | null; source?: string | null }
export interface CloudSettings {
  vast_api_key: SecretState;
  runpod_api_key?: SecretState;
  jasna_license?: SecretState;
  /** $/hour ceiling */
  max_price: number;
  /** $ stop per rental */
  job_cap: number;
  /** $ per month */
  month_cap: number;
  datacenter_only: boolean;
  /** proposed */
  max_bw_cost?: number | null;
  max_job_hours?: number | null;
}
export interface CloudRental { ended: number | string; scene?: string | null; scene_id?: string | null; preset?: string | null; gpu?: string | null; usd: number; provider?: string | null }
export interface CloudActive { id?: string; scene_id?: string | null; scene?: string | null; preset?: string | null; gpu?: string | null; usd?: number | null; started?: number | null; provider?: string | null }
export interface ProviderState { id: string; name: string; connected: boolean; balance?: number | null; error?: string | null }
export interface CloudStatus {
  month_to_date: number;
  credit?: number | null;
  active: CloudActive[];
  recent: CloudRental[];
  providers?: ProviderState[];
}
export interface CloudTest { ok: boolean; balance?: number | null; error?: string | null }

/* ---------- cloud: offers (UI) ---------- */
export type Tier = 'dc' | 'comm';
export interface Quote {
  /** whole job, boot to cleanup */
  minutes: number;
  hours: number;
  compute: number;
  transfer: number;
  storage: number;
  total: number;
  /** what the rental could cost at most before it stops */
  max: number;
  gbUp: number;
  gbDown: number;
  gbModels: number;
  diskGb: number;
  runMin?: number;
  upMin?: number;
  downMin?: number;
  setupMin?: number;
  notes: string[];
}
export interface Offer {
  id: string;
  provider: string;
  /** the provider's own id (`vast`, `runpod`), as the server expects it */
  providerId?: string;
  gpu: string;
  count: number;
  vramGb: number;
  tier: Tier;
  pricePerHour: number;
  /** percent, 0..100 */
  reliability: number;
  verified: boolean;
  /** longest rental in days; 0 = unknown */
  days: number;
  location: string;
  region: string;
  cpu: string;
  cores: number;
  ramGb: number;
  disk: string;
  diskGb: number;
  diskMbs: number;
  /** Mb/s */
  netDown: number;
  netUp: number;
  ports: number;
  cuda: number;
  driver: number;
  pcie: string;
  board: string;
  perf: number;
  tflops: number;
  /** GPU memory bandwidth, GB/s; absent when the server does not say */
  memGbs?: number;
  storagePerGbHr: number;
  bwDownPerGb: number;
  bwUpPerGb: number;
  rented: boolean;
  compatible: boolean;
  incompatibleReason: string | null;
  /** the server's bill for the current job; null when browsing without a job */
  estimate: Quote | null;
}
export type RecRole = 'value' | 'fast' | 'cheap' | 'reliable';
export interface Recommendation { role: RecRole; label: string; offerId: string; tags: string[] }
export interface OfferFacets {
  gpus: { name: string; count: number; minPrice: number; vramGb: number }[];
  counts: { value: number; count: number }[];
  regions: { code: string; count: number }[];
  providers: { name: string; count: number }[];
  /** machines that are rented right now; only a server that can list them sends this */
  rented?: number;
}
export interface SortOption { id: string; label: string; needsJob: boolean; available: boolean }
export interface OffersResult {
  offers: Offer[];
  /** total matching the filters (not just this page) */
  total: number;
  /** total before the filters */
  all: number;
  offset: number;
  facets: OfferFacets;
  sorts: SortOption[];
  recommendations: Recommendation[];
  providers: { id?: string; name: string; ok: boolean; error?: string | null }[];
  /** true when the server cut the list at its limit */
  truncated: boolean;
}
/** Filters of the rental browser. `any` / 0 mean "no filter". */
export interface OfferFilters {
  model: string;
  count: number | 'any';
  region: string;
  disk: number;
  rel: number;
  days: number;
  cuda: number;
  drv: number;
  unverified: boolean;
  cantRun: boolean;
  rented: boolean;
  dcOnly: boolean;
  providers: string[] | null;
}
export interface OfferQuery {
  /** scene ids of the job, or none when browsing */
  scenes: string[];
  op: OpId;
  quality: QualityId;
  size: SizeId;
  sort: string;
  filters: OfferFilters;
  offset?: number;
  limit?: number;
}
/** The owner's own limits, plus how much of this month is spent. */
export interface Caps { maxPrice: number; jobCap: number; monthCap: number; monthToDate: number; credit: number | null; dcOnly: boolean }
