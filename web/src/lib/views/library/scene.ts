/* Small facts about a scene that the library shows or filters on. */
import type { Scene } from '../../api/types';

/** A scene counts as done when Stash tagged it as restored or upscaled. */
export const isDone = (s: Scene): boolean => s.tags.some((t) => /^(Decensored|Upscaled)/i.test(t.name));

export const sceneHeight = (s: Scene): number => s.files[0]?.height ?? 0;

/** Keep the start and the tail (the extension) of a long file name: "harbour_walk_part…final.mp4" */
export function midTruncate(name: string, max: number): string {
  if (name.length <= max) return name;
  const tail = Math.min(14, Math.floor(max / 3));
  return `${name.slice(0, max - tail - 1)}…${name.slice(-tail)}`;
}

/** The scene's own title, else its file name (middle-truncated), else "Untitled". */
export function displayTitle(s: Scene): string {
  const t = s.title?.trim();
  if (t) return t;
  const base = s.files[0]?.path.split(/[\\/]/).pop();
  return base ? midTruncate(base, 44) : 'Untitled';
}
