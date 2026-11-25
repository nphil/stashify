/* Mock mode only: jump to one of the mockup's named screens (`?s=quality`, or the bar along the top).
   It only sets stores, so the real components do all the drawing. */
import { listScenes } from './api/library';
import { WHERE_CLOUD } from './api/types';
import { nav, type View } from './stores/nav.svelte';
import { options } from './stores/options.svelte';
import { selection } from './stores/selection.svelte';
import { toast } from './stores/toast.svelte';
import { ui } from './stores/ui.svelte';

export const MOCK_STATES = [
  ['compose', 'Compose'], ['quality', 'Quality'], ['where', 'Where'], ['cloud', 'Cloud'], ['breakdown', 'Cost breakdown'],
  ['browse', 'Browse all offers'], ['details', 'Row details'], ['sortmenu', 'Sort menu'], ['filters', 'Filters'],
  ['confirm', 'Paid confirm'], ['jobs', 'Jobs'], ['review', 'Review'], ['replace', 'Replace confirm'], ['runners', 'Runners'], ['cloudset', 'Cloud settings'], ['edge', 'States board'],
] as const;

export const mockState = $state({ current: '' });

/** screens that are a whole page, with nothing selected */
const PAGES: Record<string, View> = { jobs: 'jobs', review: 'review', replace: 'review', runners: 'runners', cloudset: 'cloud', edge: 'states' };
/** the mockup's fixed first offer: the best-value pick in the default job */
const FIRST_OFFER = 'v1';

export async function applyMockState(name: string): Promise<void> {
  ui.reset();
  toast.hide();
  mockState.current = name;
  options.reset({ q: 'high' });
  selection.clear();
  const view = PAGES[name] ?? 'library';
  nav.replace(view);
  if (view === 'library') {
    const page = await listScenes({ q: '', page: 1, per_page: 60 });
    selection.add(page.scenes.filter((s) => ['1', '2', '3'].includes(s.id)));
  }
  const cloud = (): void => { options.where = WHERE_CLOUD; ui.overlay = 'picker'; };
  switch (name) {
    case 'quality': ui.overlay = 'quality'; ui.advanced = true; break;
    case 'where': ui.overlay = 'where'; break;
    case 'cloud': cloud(); break;
    case 'breakdown': cloud(); ui.receipt = FIRST_OFFER; break;
    case 'browse': cloud(); ui.browse = true; break;
    case 'details': cloud(); ui.browse = true; ui.expanded.add(FIRST_OFFER); break;
    case 'sortmenu': cloud(); ui.browse = true; ui.menu = 'sort'; break;
    case 'filters': cloud(); ui.browse = true; ui.filters = true; break;
    case 'confirm': options.where = WHERE_CLOUD; ui.overlay = 'confirm'; break;
    case 'replace': ui.overlay = 'replace'; break;
  }
}
