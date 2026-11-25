/* One door to the Python server. Relative URLs (`api/...`) resolve against the page, so the app works
   under any sub-path. Mock mode (`?mock=1`, or VITE_MOCK=1 at build time) swaps the network for fixtures. */

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, message: string, body: unknown = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
  /** machine-readable code the server sent with the error, e.g. `busy` or `price_changed` */
  get code(): string | undefined {
    const b = this.body;
    return b && typeof b === 'object' && 'code' in b && typeof b.code === 'string' ? b.code : undefined;
  }
}

export const isAbort = (e: unknown): boolean => e instanceof Error && e.name === 'AbortError';

function detectMock(): boolean {
  try {
    const q = new URLSearchParams(location.search).get('mock');
    if (q === '1') { sessionStorage.setItem('stashify.mock', '1'); return true; }
    if (q === '0') { sessionStorage.removeItem('stashify.mock'); return false; }
    if (sessionStorage.getItem('stashify.mock') === '1') return true;
  } catch { /* storage blocked: fall through */ }
  const v = import.meta.env.VITE_MOCK;
  return v === '1' || v === 'true';
}
export const MOCK: boolean = detectMock();

export type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[];
export type Query = Record<string, QueryValue>;
export interface ReqOpts { signal?: AbortSignal; body?: unknown; query?: Query }

export function token(): string {
  const t = window.__WT ?? '';
  return t === '__WORKER_TOKEN__' ? '' : t;
}

export function apiUrl(path: string, query?: Query): string {
  const u = new URL(path.replace(/^\/+/, ''), document.baseURI);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      u.searchParams.set(k, Array.isArray(v) ? v.join(',') : String(v));
    }
  }
  return u.toString();
}

function messageOf(data: unknown, fallback: string): string {
  if (data && typeof data === 'object') {
    if ('error' in data && typeof data.error === 'string') return data.error;
    if ('message' in data && typeof data.message === 'string') return data.message;
  }
  return typeof data === 'string' && data ? data.slice(0, 200) : fallback;
}

export async function request<T = unknown>(method: string, path: string, o: ReqOpts = {}): Promise<T> {
  if (MOCK) {
    // Lazy on purpose: the fixtures must stay out of the bundle a real session downloads.
    const m = await import('./mock');
    return m.mockRequest<T>(method, path, o);
  }
  const headers: Record<string, string> = { Accept: 'application/json' };
  const t = token();
  if (t) headers['X-Decensor-Token'] = t;
  let body: string | undefined;
  if (o.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(o.body);
  }
  const res = await fetch(apiUrl(path, o.query), { method, headers, body, signal: o.signal });
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) throw new ApiError(res.status, messageOf(data, res.statusText || `HTTP ${res.status}`), data);
  return data as T;
}

export const get = <T>(path: string, o?: ReqOpts) => request<T>('GET', path, o);
export const post = <T>(path: string, body?: unknown, o?: ReqOpts) => request<T>('POST', path, { ...o, body: body ?? {} });

/** URL for media the browser loads itself (<img>, <video>). These endpoints need no token. */
export const mediaUrl = (path: string, query?: Query): string => apiUrl(path, query);
