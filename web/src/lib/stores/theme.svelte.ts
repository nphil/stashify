/* The colour theme. A person either pins one theme, or "follows the system": one dark and one light
   theme are paired and the device's light/dark setting picks between them. The inline boot script in
   index.html applied the saved choice before first paint; this store takes over from there. */
import { DEFAULT_DARK, DEFAULT_LIGHT, THEMES, type ThemeMeta } from '../themes/themes.generated';
import { loadJson, saveJson } from '../util/persist';

export interface ThemePref { mode: 'auto' | 'fixed'; id: string; dark: string; light: string }

const KEY = 'stashify.theme';
export const themeById: ReadonlyMap<string, ThemeMeta> = new Map(THEMES.map((t) => [t.id, t]));

function ok(id: unknown, scheme: 'dark' | 'light' | null = null): id is string {
  const t = typeof id === 'string' ? themeById.get(id) : undefined;
  return !!t && (!scheme || t.scheme === scheme);
}
function read(): ThemePref {
  const s = loadJson<Partial<ThemePref>>(KEY, {});
  const dark = ok(s.dark, 'dark') ? s.dark : DEFAULT_DARK;
  const light = ok(s.light, 'light') ? s.light : DEFAULT_LIGHT;
  return { mode: s.mode === 'fixed' ? 'fixed' : 'auto', id: ok(s.id) ? s.id : dark, dark, light };
}
/** `?theme=dark|light|<id>` shows a theme without saving it (links, screenshots). */
function fromUrl(): string | null {
  const v = new URLSearchParams(location.search).get('theme');
  if (v === 'dark') return DEFAULT_DARK;
  if (v === 'light') return DEFAULT_LIGHT;
  return ok(v) ? v : null;
}

const reduced = (): boolean => matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Swap the palette. A short cross-fade where the browser can do it cheaply (View Transitions), instant otherwise. */
function apply(t: ThemeMeta, animate: boolean): void {
  const d = document.documentElement;
  if (d.dataset.theme === t.id) return;
  const run = (): void => {
    d.dataset.theme = t.id;
    d.dataset.scheme = t.scheme;
    document.querySelector('meta[name=theme-color]')?.setAttribute('content', t.canvas);
  };
  if (!animate || reduced() || d.hasAttribute('data-shot') || typeof document.startViewTransition !== 'function') run();
  else document.startViewTransition(run);
}

class ThemeStore {
  pref = $state<ThemePref>(read());
  systemLight = $state(matchMedia('(prefers-color-scheme: light)').matches);
  /** set by `?theme=`; cleared as soon as the person picks something */
  override = $state<string | null>(fromUrl());

  /** the theme on screen right now */
  active: ThemeMeta = $derived.by(() => {
    const id = this.override ?? (this.pref.mode === 'fixed' ? this.pref.id : this.systemLight ? this.pref.light : this.pref.dark);
    return themeById.get(id) ?? THEMES[0];
  });
  get auto(): boolean { return this.pref.mode === 'auto' && !this.override; }

  constructor() {
    matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => { this.systemLight = e.matches; });
    let first = true;
    $effect.root(() => {
      $effect(() => { apply(this.active, !first); first = false; });
      $effect(() => { saveJson(KEY, this.pref); });
    });
  }

  /** Pick a theme. When following the system it sets that scheme's half of the pair. */
  choose(id: string): void {
    const t = themeById.get(id);
    if (!t) return;
    this.override = null;
    this.pref = { ...this.pref, mode: this.pref.mode, id: this.pref.mode === 'fixed' ? id : this.pref.id, [t.scheme]: id };
  }
  /** Follow the system (on), or pin whatever is showing now (off). */
  setAuto(on: boolean): void {
    const shown = this.active.id;
    this.override = null;
    this.pref = on ? { ...this.pref, mode: 'auto' } : { ...this.pref, mode: 'fixed', id: shown };
  }
}

export const theme = new ThemeStore();
