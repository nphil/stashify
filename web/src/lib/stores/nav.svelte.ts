/* Which page is showing. Hash routes (#/jobs) work under any sub-path with a plain static server. */

export type View = 'library' | 'jobs' | 'review' | 'runners' | 'cloud' | 'states';
const VIEWS: readonly View[] = ['library', 'jobs', 'review', 'runners', 'cloud', 'states'];

function fromHash(): View {
  const h = location.hash.replace(/^#\/?/, '');
  return (VIEWS as readonly string[]).includes(h) ? (h as View) : 'library';
}

class Nav {
  view = $state<View>(fromHash());
  /** bumps on every page change so the shell can move focus to the new page heading */
  stamp = $state(0);

  constructor() {
    addEventListener('popstate', () => this.#sync());
    addEventListener('hashchange', () => this.#sync());
  }
  #sync(): void {
    const v = fromHash();
    if (v !== this.view) { this.view = v; this.stamp++; }
  }
  go(v: View): void {
    if (v === this.view) return;
    history.pushState(null, '', `#/${v}`);
    this.view = v;
    this.stamp++;
  }
  /** set without adding a history entry (mock `?s=` states) */
  replace(v: View): void {
    history.replaceState(null, '', `#/${v}`);
    this.view = v;
  }
}

export const nav = new Nav();
