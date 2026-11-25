/* The twelve scenes of the approved mockup, shared by every mock area (library, jobs, cloud quotes).
   Neutral titles and gradient thumbnails only. Scene ids are the strings '1'..'12', in this order. */
import type { Scene } from '../types';

const GB = 1e9;
interface Row { id: number; title: string; studio: string; dur: number; h: number; gb: number; tn: number; done?: boolean }

const ROWS: readonly Row[] = [
  { id: 1, title: 'Harbour at dusk', studio: 'Blue Harbor', dur: 2482, h: 1080, gb: 1.9, tn: 1 },
  { id: 2, title: 'Cabin weekend, part 2', studio: 'Northlight', dur: 2285, h: 1080, gb: 1.7, tn: 2 },
  { id: 3, title: 'Studio session 14', studio: 'Atlas Films', dur: 1963, h: 1080, gb: 1.5, tn: 3 },
  { id: 4, title: 'City walk after rain', studio: 'Northlight', dur: 1638, h: 720, gb: 0.9, tn: 4 },
  { id: 5, title: 'Garden party', studio: 'Blue Harbor', dur: 3160, h: 2160, gb: 6.4, tn: 5 },
  { id: 6, title: 'Lake house, day one', studio: 'Atlas Films', dur: 1449, h: 1080, gb: 1.2, tn: 6 },
  { id: 7, title: 'Rooftop evening', studio: 'Northlight', dur: 2815, h: 1080, gb: 2.2, tn: 1, done: true },
  { id: 8, title: 'Market morning', studio: 'Blue Harbor', dur: 1111, h: 720, gb: 0.6, tn: 2 },
  { id: 9, title: 'Summer rental', studio: 'Atlas Films', dur: 3912, h: 2160, gb: 8.1, tn: 3 },
  { id: 10, title: 'Backstage, take 3', studio: 'Northlight', dur: 1787, h: 1080, gb: 1.4, tn: 4, done: true },
  { id: 11, title: 'Old town stroll', studio: 'Blue Harbor', dur: 2120, h: 1080, gb: 1.6, tn: 5 },
  { id: 12, title: 'Winter cabin', studio: 'Atlas Films', dur: 2588, h: 1080, gb: 2.0, tn: 6 },
];

const slug = (s: string): string => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
const WIDTH: Record<number, number> = { 720: 1280, 1080: 1920, 2160: 3840 };

export function makeScene(r: Row): Scene {
  return {
    id: String(r.id),
    title: r.title,
    date: `2026-11-${String(Math.max(1, 25 - r.id)).padStart(2, '0')}`,
    files: [{ path: `/library/${slug(r.title)}.mp4`, width: WIDTH[r.h] ?? 1920, height: r.h, duration: r.dur, size: Math.round(r.gb * GB) }],
    studio: { name: r.studio },
    tags: r.done ? [{ name: 'Decensored' }] : [],
    tn: r.tn,
  };
}

export const BASE_SCENES: readonly Scene[] = ROWS.map(makeScene);

/** the mock library's title for a scene id (jobs and reviews name their scene with it) */
export function sceneTitle(id: string): string | undefined {
  return BASE_SCENES.find((s) => s.id === id)?.title ?? undefined;
}
