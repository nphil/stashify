/* Mock library: GET api/scenes (search, sort, tag, paging) and GET api/tags.
   The twelve mockup scenes come first, then neutral generated clips up to `?scenes=<n>` (default 1,248, as in the mockup). */
import type { Scene, ScenesPage, Tag } from '../types';
import { BASE_SCENES } from './scenes';
import { route } from './router';

const GB = 1e9;
const PLACES = ['Harbour', 'Meadow', 'Quarry', 'Terrace', 'Lagoon', 'Orchard', 'Viaduct', 'Foundry', 'Canyon', 'Boulevard', 'Greenhouse', 'Lighthouse'] as const;
const STUDIOS = ['Blue Harbor', 'Northlight', 'Atlas Films', 'Fernwood', 'Paperboat', 'Lumen Works'] as const;
const TAGS = [
  'Outdoors', 'Interior', 'Travel', 'Interview', 'Night', 'Daylight', 'Archive', 'Rehearsal', 'Documentary', 'Landscape', 'Portrait',
  'Handheld', 'Tripod', 'Montage', 'Timelapse', 'Aerial', 'Street', 'Water', 'Festival', 'Weekend', 'Vintage', 'Extended',
] as const;
const HEIGHTS = [480, 720, 720, 1080, 1080, 1080, 1080, 2160] as const;
const WIDTH: Record<number, number> = { 480: 854, 720: 1280, 1080: 1920, 2160: 3840 };
const BITRATE: Record<number, number> = { 480: 1.2e6, 720: 2.5e6, 1080: 5e6, 2160: 16e6 };
const FIRST_GENERATED_DAY = Date.UTC(2026, 10, 12);

interface Entry { scene: Scene; added: number; modified: number }

const pad4 = (n: number): string => String(n).padStart(4, '0');
const slug = (s: string): string => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

function tagsFor(i: number): { name: string }[] {
  const names = new Set<string>([TAGS[(i * 7) % TAGS.length]]);
  if (i % 3 === 0) names.add(TAGS[(i * 5 + 3) % TAGS.length]);
  if (i % 5 === 0) names.add(TAGS[(i * 11 + 1) % TAGS.length]);
  return [...names].map((name) => ({ name }));
}

function generated(i: number): Scene {
  const place = PLACES[i % PLACES.length];
  const h = HEIGHTS[(i * 5 + 2) % HEIGHTS.length];
  const dur = 600 + ((i * 937) % 4800);
  const done = i % 7 === 3 ? 'Decensored' : i % 11 === 5 ? 'Upscaled' : null;
  const name = `clip_${pad4(i + 1)}_${slug(place)}`;
  /* every 29th clip has no title in Stash: the file name stands in for it */
  const titled = i % 29 !== 4;
  const path = titled ? `/library/${name}.mp4` : `/library/incoming/${name}_v2_final_export_graded.mp4`;
  return {
    id: String(i + 1),
    title: titled ? `Clip ${pad4(i + 1)} · ${place}` : null,
    date: new Date(FIRST_GENERATED_DAY - (i - 11) * 86_400_000).toISOString().slice(0, 10),
    files: [{ path, width: WIDTH[h], height: h, duration: dur, size: Math.round((dur * BITRATE[h]) / 8) }],
    studio: { name: STUDIOS[(i * 3) % STUDIOS.length] },
    tags: [...(done ? [{ name: done }] : []), ...tagsFor(i)],
    tn: (i % 6) + 1,
  };
}

const built = new Map<number, Entry[]>();
function library(): Entry[] {
  const q = Number(new URLSearchParams(location.search).get('scenes'));
  const n = Math.max(3, Number.isFinite(q) && q > 0 ? Math.floor(q) : 1248);
  const hit = built.get(n);
  if (hit) return hit;
  const all: Scene[] = BASE_SCENES.map((s, i) => ({ ...s, tags: [...s.tags, ...tagsFor(i)] }));
  for (let i = BASE_SCENES.length; i < n; i++) all.push(generated(i));
  const list = all.slice(0, n).map((scene, i) => ({ scene, added: (i * 37) % n, modified: (i * 53 + 7) % n }));
  built.set(n, list);
  return list;
}

function tagList(): Tag[] {
  const counts = new Map<string, number>();
  for (const e of library()) for (const t of e.scene.tags) counts.set(t.name, (counts.get(t.name) ?? 0) + 1);
  return [...counts].map(([name, scene_count]) => ({ id: slug(name), name, scene_count })).sort((a, b) => b.scene_count - a.scene_count || a.name.localeCompare(b.name));
}

/** small seeded shuffle so a random order is the same on every page of one listing */
function shuffled<T>(list: readonly T[], seed: number): T[] {
  const out = [...list];
  let s = seed >>> 0;
  const next = (): number => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(next() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

route('GET', 'api/tags', () => tagList());

route('GET', 'api/scenes', ({ query }): ScenesPage => {
  const q = (query.get('q') ?? '').trim().toLowerCase();
  const tag = query.get('tag') ?? '';
  const sort = query.get('sort') ?? 'date';
  const desc = (query.get('dir') ?? 'desc') !== 'asc';
  const page = Math.max(1, Number(query.get('page')) || 1);
  const per = Math.min(60, Math.max(1, Number(query.get('per_page')) || 60));

  const tagName = tag ? tagList().find((t) => t.id === tag)?.name : undefined;
  let list = library().map((e, i) => ({ ...e, i }));
  if (q) list = list.filter(({ scene: s }) => `${s.title ?? ''} ${s.studio?.name ?? ''} ${s.files[0]?.path ?? ''}`.toLowerCase().includes(q));
  if (tag) list = list.filter(({ scene: s }) => tagName !== undefined && s.tags.some((t) => t.name === tagName));

  if (sort === 'random') {
    list = shuffled(list, Number(query.get('seed')) || 1);
  } else {
    const key = (e: (typeof list)[number]): string | number => {
      if (sort === 'title') return (e.scene.title ?? e.scene.files[0]?.path ?? '').toLowerCase();
      if (sort === 'created_at') return e.added;
      if (sort === 'file_mod_time') return e.modified;
      return e.scene.date ?? '';
    };
    list.sort((a, b) => {
      const x = key(a);
      const y = key(b);
      const c = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
      return (desc ? -c : c) || a.i - b.i;
    });
  }
  return { count: list.length, scenes: list.slice((page - 1) * per, page * per).map((e) => e.scene) };
});
