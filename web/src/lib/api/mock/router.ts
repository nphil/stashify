/* A tiny in-browser server. Mock modules register routes; `mockRequest` plays the network:
   simulated latency (60-200 ms, or `?lat=<ms>`), abort support, JSON round-trip (no shared references). */
import { ApiError, type ReqOpts } from '../client';

export interface MockCtx {
  method: string;
  path: string;
  params: Record<string, string>;
  query: URLSearchParams;
  body: unknown;
  signal?: AbortSignal;
}
export type MockHandler = (ctx: MockCtx) => unknown;

interface RouteDef { method: string; re: RegExp; keys: string[]; h: MockHandler }
const routes: RouteDef[] = [];

/** `route('GET', 'api/jobs/:id/log', ctx => ...)`. Handlers may be async and may throw `fail(...)`. */
export function route(method: string, pattern: string, h: MockHandler): void {
  const keys: string[] = [];
  const src = pattern.replace(/^\/+/, '').split('/').map((seg) => {
    if (seg.startsWith(':')) { keys.push(seg.slice(1)); return '([^/]+)'; }
    return seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('/');
  routes.push({ method, re: new RegExp(`^${src}$`), keys, h });
}

export function fail(status: number, message: string, extra: Record<string, unknown> = {}): never {
  throw new ApiError(status, message, { error: message, ...extra });
}

const params = new URLSearchParams(location.search);
const FIXED_LATENCY = params.has('lat') ? Number(params.get('lat')) : null;

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
  });
}

export async function mockRequest<T>(method: string, path: string, o: ReqOpts): Promise<T> {
  const [rawPath, rawQuery = ''] = path.replace(/^\/+/, '').split('?');
  const query = new URLSearchParams(rawQuery);
  for (const [k, v] of Object.entries(o.query ?? {})) {
    if (v === undefined || v === null || v === '') continue;
    query.set(k, Array.isArray(v) ? v.join(',') : String(v));
  }
  await wait(FIXED_LATENCY ?? 60 + Math.random() * 140, o.signal);
  for (const r of routes) {
    if (r.method !== method) continue;
    const m = r.re.exec(rawPath);
    if (!m) continue;
    const p: Record<string, string> = {};
    r.keys.forEach((k, i) => { p[k] = decodeURIComponent(m[i + 1]); });
    const out = await r.h({ method, path: rawPath, params: p, query, body: o.body, signal: o.signal });
    if (o.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    return (out === undefined ? null : structuredClone(out)) as T;
  }
  throw new ApiError(404, `No mock for ${method} ${rawPath}`);
}
