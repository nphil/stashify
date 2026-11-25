/* Live data. One shared Server-Sent-Events stream (`api/events`) pushes snapshots; if the server has no
   stream, drops it, or the tab is hidden, a plain poll takes over. The caller never sees which one ran.

   Events (see BACKEND_NEEDS.md): `jobs`, `gpus`, `runners`, `cloud` carry the same JSON as the matching GET;
   `log` carries { job_id, cursor, lines }; `ping` is a keep-alive. `log` is delivered on topic `log:<job_id>`. */

import { apiUrl, isAbort, MOCK, token } from './client';

type Handler = (data: unknown) => void;
const subs = new Map<string, Set<Handler>>();
const connListeners = new Set<() => void>();
let connected = false;
let users = 0;
let stream: AbortController | null = null;
let lastId = '';
let unsupportedUntil = 0;

export const sseConnected = (): boolean => connected;
export const onConnection = (fn: () => void): (() => void) => { connListeners.add(fn); return () => connListeners.delete(fn); };

function setConnected(v: boolean): void {
  if (connected === v) return;
  connected = v;
  connListeners.forEach((f) => f());
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((res) => {
    const t = setTimeout(res, ms);
    signal.addEventListener('abort', () => { clearTimeout(t); res(); }, { once: true });
  });

function dispatch(frame: string): void {
  let event = 'message';
  let id = '';
  const data: string[] = [];
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const i = line.indexOf(':');
    const field = i < 0 ? line : line.slice(0, i);
    const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
    else if (field === 'id') id = value;
  }
  if (id) lastId = id;
  if (!data.length || event === 'ping') return;
  let parsed: unknown;
  try { parsed = JSON.parse(data.join('\n')); } catch { return; }
  let topic = event;
  if (event === 'log' && parsed && typeof parsed === 'object' && 'job_id' in parsed) topic = `log:${String(parsed.job_id)}`;
  subs.get(topic)?.forEach((h) => h(parsed));
}

async function run(signal: AbortSignal): Promise<void> {
  let backoff = 1000;
  while (!signal.aborted) {
    if (Date.now() < unsupportedUntil) { await sleep(5000, signal); continue; }
    const idle = new AbortController();
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const kick = () => { clearTimeout(idleTimer); idleTimer = setTimeout(() => idle.abort(), 45_000); };
    const stop = () => idle.abort();
    signal.addEventListener('abort', stop, { once: true });
    try {
      const headers: Record<string, string> = { Accept: 'text/event-stream' };
      const t = token();
      if (t) headers['X-Decensor-Token'] = t;
      if (lastId) headers['Last-Event-ID'] = lastId;
      kick();
      const res = await fetch(apiUrl('api/events'), { headers, signal: idle.signal });
      if (res.status === 404 || res.status === 405 || res.status === 501) {
        unsupportedUntil = Date.now() + 5 * 60_000;
        setConnected(false);
        continue;
      }
      if (!res.ok || !res.body) throw new Error(`events ${res.status}`);
      setConnected(true);
      backoff = 1000;
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        kick();
        buf += value.replace(/\r\n/g, '\n');
        let i: number;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          dispatch(buf.slice(0, i));
          buf = buf.slice(i + 2);
        }
      }
    } catch (e) {
      if (signal.aborted) return;
      if (!isAbort(e)) { /* network error: fall through to the backoff */ }
    } finally {
      clearTimeout(idleTimer);
      signal.removeEventListener('abort', stop);
    }
    setConnected(false);
    await sleep(backoff, signal);
    backoff = Math.min(backoff * 2, 30_000);
  }
}

function open(): void {
  if (MOCK || stream) return;
  stream = new AbortController();
  void run(stream.signal);
}
function close(): void {
  stream?.abort();
  stream = null;
  setConnected(false);
}

export interface LiveOpts<T> {
  /** one plain request for the same data the stream pushes */
  poll: (signal: AbortSignal) => Promise<T>;
  /** poll cadence while there is no stream */
  everyMs: number;
  onData: (data: T) => void;
  onError?: (e: unknown) => void;
}

/** Keep `onData` fed with fresh data. Returns `stop()`. Safe to call before the stream exists. */
export function live<T>(topic: string, o: LiveOpts<T>): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let inflight: AbortController | null = null;

  const every = () => (sseConnected() ? Math.max(o.everyMs * 10, 30_000) : o.everyMs);
  const schedule = () => { clearTimeout(timer); if (!stopped) timer = setTimeout(tick, every()); };
  async function tick(): Promise<void> {
    clearTimeout(timer);
    if (stopped) return;
    if (document.hidden) { schedule(); return; }
    inflight?.abort();
    inflight = new AbortController();
    try {
      const d = await o.poll(inflight.signal);
      if (!stopped) o.onData(d);
    } catch (e) {
      if (!isAbort(e)) o.onError?.(e);
    }
    schedule();
  }
  const handler: Handler = (d) => { if (!stopped) o.onData(d as T); };
  const visible = () => { if (!document.hidden) void tick(); };

  let set = subs.get(topic);
  if (!set) subs.set(topic, (set = new Set()));
  set.add(handler);
  users++;
  open();
  document.addEventListener('visibilitychange', visible);
  const off = onConnection(schedule);
  void tick();

  return () => {
    stopped = true;
    clearTimeout(timer);
    inflight?.abort();
    set.delete(handler);
    document.removeEventListener('visibilitychange', visible);
    off();
    if (--users <= 0) { users = 0; close(); }
  };
}
