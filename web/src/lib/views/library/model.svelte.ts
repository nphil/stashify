/* The library's state, kept for the whole session: what was searched and sorted, every page loaded so far,
   and where the person had scrolled. Coming back to the page shows it exactly as it was. */
import { swr } from '../../api/cache';
import { isAbort } from '../../api/client';
import { listScenes, listTags } from '../../api/library';
import type { Scene, ScenesQuery, Tag } from '../../api/types';
import { loadJson, saveJson } from '../../util/persist';
import { isDone, sceneHeight } from './scene';

export const PER_PAGE = 60;
const STALE_MS = 30_000;
const KEY = 'stashify.library.view';

export type SortId = NonNullable<ScenesQuery['sort']>;
export type Dir = NonNullable<ScenesQuery['dir']>;
export interface TagRef { id: string; name: string }
export interface Prefs { sort: SortId; dir: Dir; minRes: number; tag: TagRef | null; hideDone: boolean }
type Phase = 'loading' | 'ready' | 'error';
type More = 'idle' | 'loading' | 'error';

export const DEFAULT_DIR: Record<SortId, Dir> = { date: 'desc', created_at: 'desc', title: 'asc', file_mod_time: 'desc', random: 'desc' };
const DEFAULTS: Prefs = { sort: 'date', dir: 'desc', minRes: 0, tag: null, hideDone: false };

const messageOf = (e: unknown): string => (e instanceof Error && e.message ? e.message : 'Could not load scenes');

class LibraryModel {
  /** what is typed in the search field (instant); the request follows 250 ms later */
  q = $state('');
  prefs = $state<Prefs>(loadJson<Prefs>(KEY, DEFAULTS));
  scenes = $state.raw<Scene[]>([]);
  /** scenes the server says match the current search and tag */
  count = $state(0);
  /** every scene in the library (known after an unfiltered load) */
  total = $state(0);
  phase = $state<Phase>('loading');
  more = $state<More>('idle');
  error = $state('');
  /** the last page had nothing new: stop asking */
  done = $state(false);
  /** busiest first */
  tags = $state.raw<Tag[]>([]);

  /** kept across page visits (plain values: they change every frame) */
  scrollTop = 0;
  tabIdx = 0;
  /** the grid sets this: jump back to the top when the list is replaced */
  onReset: (() => void) | null = null;

  #page = 0;
  #seed = Math.floor(Math.random() * 2 ** 31);
  #loaded = false;
  #loadedAt = 0;
  #ids = new Set<string>();
  #timer: ReturnType<typeof setTimeout> | undefined;
  #ctl: AbortController | null = null;
  #moreCtl: AbortController | null = null;
  #headCtl: AbortController | null = null;

  /** the loaded scenes that pass the size and "not cleaned yet" filters (those two run in the browser) */
  visible = $derived.by(() => {
    const { minRes, hideDone } = this.prefs;
    if (!minRes && !hideDone) return this.scenes;
    return this.scenes.filter((s) => (!minRes || sceneHeight(s) >= minRes) && (!hideDone || !isDone(s)));
  });
  hasMore = $derived(!this.done && this.scenes.length < this.count);
  filtered = $derived(this.q.trim() !== '' || this.prefs.tag !== null || this.prefs.minRes > 0 || this.prefs.hideDone);
  countLine = $derived.by(() => {
    if (this.phase === 'error' && !this.scenes.length) return 'Could not load scenes';
    if (this.phase === 'loading' && !this.scenes.length) return 'Loading scenes';
    const base = `${this.count.toLocaleString()} ${this.count === 1 ? 'scene' : 'scenes'}`;
    const local = this.prefs.minRes > 0 || this.prefs.hideDone;
    return local ? `${base} · ${this.visible.length.toLocaleString()} shown` : base;
  });

  #query(page: number): ScenesQuery {
    const { sort, dir, tag } = this.prefs;
    return {
      q: this.q.trim() || undefined, page, per_page: PER_PAGE, sort, dir, tag: tag?.id,
      seed: sort === 'random' ? this.#seed : undefined,
    };
  }

  /** Call when the page opens. Returns the stop function for when it closes. */
  start(): () => void {
    const stopTags = swr<Tag[]>('library:tags', (signal) => listTags(signal), (v) => {
      this.tags = [...v].sort((a, b) => b.scene_count - a.scene_count);
    }, { ttlMs: 60_000 });
    if (!this.#loaded) this.reload();
    else if (Date.now() - this.#loadedAt > STALE_MS) this.#refreshHead();
    return () => {
      stopTags();
      this.#headCtl?.abort();
      this.#moreCtl?.abort();
      if (this.more === 'loading') this.more = 'idle';
      if (this.#timer !== undefined) { clearTimeout(this.#timer); this.#timer = undefined; this.#loaded = false; }
      if (this.phase === 'loading') { this.#ctl?.abort(); this.#loaded = false; }
    };
  }

  /** Throw away the loaded pages and ask for page 1 again. */
  reload(): void {
    clearTimeout(this.#timer);
    this.#timer = undefined;
    this.#ctl?.abort();
    this.#moreCtl?.abort();
    this.#headCtl?.abort();
    const ctl = (this.#ctl = new AbortController());
    this.#loaded = false;
    this.phase = 'loading';
    this.more = 'idle';
    this.error = '';
    this.onReset?.();
    listScenes(this.#query(1), ctl.signal).then(
      (r) => {
        if (ctl.signal.aborted) return;
        this.#ids = new Set(r.scenes.map((s) => s.id));
        this.scenes = r.scenes;
        this.count = r.count;
        if (this.q.trim() === '' && this.prefs.tag === null) this.total = r.count;
        this.#page = 1;
        this.done = r.scenes.length === 0;
        this.#loaded = true;
        this.#loadedAt = Date.now();
        this.phase = 'ready';
      },
      (e: unknown) => {
        if (ctl.signal.aborted || isAbort(e)) return;
        this.error = messageOf(e);
        this.phase = 'error';
      },
    );
  }

  /** The next page, when the person is near the end. Safe to call any time. */
  loadMore(): void {
    if (this.phase !== 'ready' || this.more !== 'idle' || !this.hasMore) return;
    const ctl = (this.#moreCtl = new AbortController());
    const page = this.#page + 1;
    this.more = 'loading';
    listScenes(this.#query(page), ctl.signal).then(
      (r) => {
        if (ctl.signal.aborted) return;
        const fresh = r.scenes.filter((s) => !this.#ids.has(s.id));
        for (const s of fresh) this.#ids.add(s.id);
        if (fresh.length) this.scenes = [...this.scenes, ...fresh];
        this.#page = page;
        this.count = r.count;
        if (r.scenes.length === 0) this.done = true;
        this.more = 'idle';
      },
      (e: unknown) => {
        if (ctl.signal.aborted || isAbort(e)) return;
        this.error = messageOf(e);
        this.more = 'error';
      },
    );
  }

  retryMore(): void {
    if (this.more !== 'error') return;
    this.more = 'idle';
    this.loadMore();
  }

  /** Coming back after a while: refresh page 1 in place so new tags (done badges) show up, without moving anything. */
  #refreshHead(): void {
    if (this.prefs.sort === 'random') return;
    const ctl = (this.#headCtl = new AbortController());
    listScenes(this.#query(1), ctl.signal).then(
      (r) => {
        if (ctl.signal.aborted) return;
        const fresh = new Map(r.scenes.map((s) => [s.id, s]));
        this.scenes = this.scenes.map((s) => fresh.get(s.id) ?? s);
        this.count = r.count;
        this.#loadedAt = Date.now();
      },
      () => { /* a quiet refresh: the pages already shown stay */ },
    );
  }

  /** Typing: the field updates at once, the request waits for a pause. */
  setQ(v: string): void {
    this.q = v;
    this.#ctl?.abort();
    this.#moreCtl?.abort();
    clearTimeout(this.#timer);
    this.#timer = setTimeout(() => this.reload(), 250);
  }

  /** Enter in the search field: don't wait for the pause. */
  flushQ(): void {
    if (this.#timer !== undefined) this.reload();
  }

  setSort(sort: SortId): void {
    if (sort === 'random') this.#seed = Math.floor(Math.random() * 2 ** 31);
    this.#set({ sort, dir: DEFAULT_DIR[sort] }, true);
  }
  setDir(dir: Dir): void { this.#set({ dir }, true); }
  setTag(tag: TagRef | null): void { this.#set({ tag }, true); }
  setMinRes(minRes: number): void { this.#set({ minRes }, false); }
  toggleHideDone(): void { this.#set({ hideDone: !this.prefs.hideDone }, false); }

  /** Back to an unfiltered, newest-first library. */
  clearFilters(): void {
    this.q = '';
    this.#set({ ...DEFAULTS, sort: this.prefs.sort, dir: this.prefs.dir }, true);
  }

  #set(patch: Partial<Prefs>, server: boolean): void {
    this.prefs = { ...this.prefs, ...patch };
    saveJson(KEY, this.prefs);
    if (server) this.reload();
  }
}

export const model = new LibraryModel();
