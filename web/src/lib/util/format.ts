/* Small display helpers shared by every view. */

export const pad2 = (n: number): string => String(n).padStart(2, '0');

/** 3725 -> "1:02:05", 95 -> "1:35" */
export function fmtDur(s: number): string {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const x = s % 60;
  return h ? `${h}:${pad2(m)}:${pad2(x)}` : `${m}:${pad2(x)}`;
}

/** 7 -> "7 min", 75 -> "1 h 15 min", 120 -> "2 h" */
export function fmtMin(min: number): string {
  min = Math.max(1, Math.round(min));
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m ? `${h} h ${pad2(m)} min` : `${h} h`;
}

export const usd = (n: number, digits = 2): string => '$' + n.toFixed(digits);

export const gbf = (n: number): string => (n >= 10 ? n.toFixed(0) : n.toFixed(1)) + ' GB';

export const resName = (h: number): string => (h >= 2160 ? '4K' : `${h}p`);

/** Round to cents. */
export const r2 = (x: number): number => Math.round(x * 100) / 100;

/** Plain count + noun: plural(2, 'scene') -> "2 scenes" */
export const plural = (n: number, one: string, many = one + 's'): string => `${n} ${n === 1 ? one : many}`;

/** Seconds ago / date for history rows. `now` is injectable so mock time can be fixed. */
export function fmtWhen(ts: number | string, now = Date.now()): string {
  const d = new Date(typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const same = new Date(now).toDateString() === d.toDateString();
  const time = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (same) return `Today ${time}`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
