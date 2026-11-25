/* Stale-while-revalidate: show what we already have, fetch fresh in the background, cancel when asked. */
import { isAbort } from './client';

const store = new Map<string, { t: number; v: unknown }>();

export const peek = <T>(key: string): T | undefined => store.get(key)?.v as T | undefined;
export const put = <T>(key: string, v: T): void => { store.set(key, { t: Date.now(), v }); };
export const drop = (prefix: string): void => { for (const k of store.keys()) if (k.startsWith(prefix)) store.delete(k); };

export interface SwrOpts { ttlMs?: number; onError?: (e: unknown) => void }

/** Calls `cb(value, stale)` once from cache (if any), then once with fresh data. Returns `abort()`. */
export function swr<T>(key: string, load: (signal: AbortSignal) => Promise<T>, cb: (v: T, stale: boolean) => void, o: SwrOpts = {}): () => void {
  const hit = store.get(key);
  if (hit) cb(hit.v as T, true);
  if (hit && Date.now() - hit.t < (o.ttlMs ?? 0)) return () => {};
  const ctl = new AbortController();
  load(ctl.signal).then(
    (v) => { put(key, v); if (!ctl.signal.aborted) cb(v, false); },
    (e) => { if (!isAbort(e) && !ctl.signal.aborted) o.onError?.(e); },
  );
  return () => ctl.abort();
}
