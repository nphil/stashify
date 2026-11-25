/* The scenes picked in the library. Keeps the whole scene so the tray can add up time and size
   without asking the server again. */
import { SvelteMap } from 'svelte/reactivity';
import type { Scene, SceneInfo } from '../api/types';
import { sceneInfo } from '../choices';

class Selection {
  #m = new SvelteMap<string, Scene>();

  get count(): number { return this.#m.size; }
  /** in the order they were picked */
  get list(): Scene[] { return [...this.#m.values()]; }
  get ids(): string[] { return [...this.#m.keys()]; }
  /** n, dur (s), eff (s, weighted by resolution), gb */
  info: SceneInfo = $derived.by(() => sceneInfo(this.list));

  has(id: string): boolean { return this.#m.has(id); }
  toggle(s: Scene): void {
    if (this.#m.has(s.id)) this.#m.delete(s.id);
    else this.#m.set(s.id, s);
  }
  add(scenes: readonly Scene[]): void { for (const s of scenes) this.#m.set(s.id, s); }
  remove(id: string): void { this.#m.delete(id); }
  clear(): void { this.#m.clear(); }
}

export const selection = new Selection();
