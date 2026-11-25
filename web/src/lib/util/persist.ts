/* localStorage that never throws (private mode, quota, blocked storage). */

export function loadJson<T>(key: string, fallback: T): T {
  try {
    const s = localStorage.getItem(key);
    if (!s) return fallback;
    const v: unknown = JSON.parse(s);
    const plain = fallback !== null && typeof fallback === 'object' && !Array.isArray(fallback);
    return (plain && v && typeof v === 'object' ? { ...fallback, ...v } : v) as T;
  } catch {
    return fallback;
  }
}

export function saveJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage unavailable: the choice just won't survive a reload */ }
}
