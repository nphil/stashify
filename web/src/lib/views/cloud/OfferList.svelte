<script lang="ts">
  /* The offer table. Only the rows in (and just around) the view exist in the page; two spacers stand in for the rest, so
     500 machines scroll like 20. Row heights are measured as rows appear, because details and wrapping make them differ.
     The scroll position lives in plain variables; state is written only when the set of drawn rows changes. */
  import { tick, untrack } from 'svelte';
  import type { Offer } from '../../api/types';
  import { cloud } from '../../stores/cloud.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Icon from '../../ui/Icon.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import OfferRow from './OfferRow.svelte';

  let { job, selectedId, onselect }: { job: boolean; selectedId: string | null; onselect: (o: Offer) => void } = $props();

  const OVER = 6; // rows kept drawn above and below the view
  const NEAR = 20; // ask for the next page when the drawn rows get this close to the end
  const items = $derived(cloud.visible);

  let scroller: HTMLDivElement;
  let head: HTMLDivElement;
  let tops = $state<HTMLDivElement>();
  let bottoms = $state<HTMLDivElement>();
  let start = $state(0);
  let end = $state(0);
  let stopId = $state<string | null>(null);
  const windowed = $derived(items.slice(start, end));
  /** the one row Tab lands on: the row last focused, else the chosen one, else the first drawn */
  const tabId = $derived.by(() => {
    for (const id of [stopId, selectedId]) if (id !== null && windowed.some((o) => o.id === id)) return id;
    return windowed[0]?.id ?? null;
  });

  const heights = new Map<string, number>();
  const watched = new Set<HTMLElement>();
  let offsets: number[] = [0];
  let index = new Map<string, number>();
  let dirty = true;
  let guess = 96;
  let base = 0;
  let viewport = 0;
  let width = 0;
  let frame = 0;
  let epoch = cloud.rowsEpoch;

  /** where every row starts, from the heights measured so far (a guess for the rest) */
  function rebuild(): void {
    if (!dirty) return;
    dirty = false;
    const n = items.length;
    offsets = new Array<number>(n + 1);
    index = new Map();
    let y = 0;
    for (let i = 0; i < n; i++) {
      const id = items[i].id;
      offsets[i] = y;
      index.set(id, i);
      y += heights.get(id) ?? guess;
    }
    offsets[n] = y;
  }
  const at = (i: number): number => offsets[Math.min(i, offsets.length - 1)];

  /** the first row whose bottom edge is below `y` */
  function firstBelow(y: number): number {
    let lo = 0;
    let hi = items.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (offsets[mid + 1] > y) hi = mid;
      else lo = mid + 1;
    }
    return lo;
  }

  function spacers(): void {
    if (!tops || !bottoms) return;
    tops.style.height = `${at(start)}px`;
    bottoms.style.height = `${Math.max(0, at(items.length) - at(end))}px`;
  }

  function place(): void {
    frame = 0;
    if (!scroller) return;
    rebuild();
    const n = items.length;
    const view = viewport || scroller.clientHeight || 600;
    const y = Math.max(0, scroller.scrollTop - base);
    const s = Math.max(0, firstBelow(y) - OVER);
    const e = Math.min(n, firstBelow(y + view) + OVER + 1);
    if (s !== start) start = s;
    if (e !== end) end = e;
    spacers();
    if (e >= n - NEAR) cloud.loadMore();
  }
  const schedule = (): void => { if (frame === 0) frame = requestAnimationFrame(place); };

  /* every drawn row reports its height; rows scrolled out above the view that change size push the scroll position by the same amount */
  const rowsSeen = new ResizeObserver((entries) => {
    rebuild();
    const above = firstBelow(Math.max(0, scroller.scrollTop - base));
    let shift = 0;
    for (const en of entries) {
      if (!(en.target instanceof HTMLElement)) continue;
      const id = en.target.dataset.id;
      if (id === undefined) continue;
      const h = en.borderBoxSize.length > 0 ? en.borderBoxSize[0].blockSize : en.contentRect.height;
      const was = heights.get(id);
      if (was !== undefined && Math.abs(h - was) < 0.5) continue;
      heights.set(id, h);
      if (!ui.expanded.has(id)) guess = h;
      const i = index.get(id);
      if (i !== undefined && i < above) shift += h - (was ?? guess);
      dirty = true;
    }
    if (!dirty) return;
    rebuild();
    spacers();
    if (shift !== 0) scroller.scrollTop += shift;
  });

  /* a different width re-flows the rows: forget the measurements and let the drawn rows report again */
  const box = new ResizeObserver(() => {
    viewport = scroller.clientHeight;
    base = head.offsetHeight;
    if (scroller.clientWidth !== width) {
      width = scroller.clientWidth;
      heights.clear();
      dirty = true;
      for (const el of watched) {
        rowsSeen.unobserve(el);
        rowsSeen.observe(el);
      }
    }
    schedule();
  });

  function measure(node: HTMLElement): { destroy: () => void } {
    watched.add(node);
    rowsSeen.observe(node);
    return { destroy: () => { watched.delete(node); rowsSeen.unobserve(node); } };
  }

  $effect(() => {
    box.observe(scroller);
    return () => {
      box.disconnect();
      rowsSeen.disconnect();
      cancelAnimationFrame(frame);
    };
  });
  /* a new list: sorted, filtered, refreshed or one page longer */
  $effect(() => {
    void items;
    untrack(() => { dirty = true; place(); });
  });
  /* a different question: start at the top */
  $effect(() => {
    const now = cloud.rowsEpoch;
    if (now === epoch) return;
    epoch = now;
    untrack(() => { scroller.scrollTop = 0; });
  });
  /* the spacers exist only once rows do */
  $effect(() => {
    void [windowed, tops, bottoms];
    untrack(spacers);
  });

  /* Arrows move between rows (even the ones that cannot be chosen, so their reason can be read); Space or Enter chooses. */
  function go(n: number): void {
    const id = items[n].id;
    stopId = id;
    rebuild();
    const top = at(n);
    const bottom = at(n + 1) + base;
    if (top < scroller.scrollTop) scroller.scrollTop = top;
    else if (bottom > scroller.scrollTop + viewport) scroller.scrollTop = bottom - viewport;
    place();
    void tick().then(() => scroller.querySelector<HTMLElement>(`.mrow[data-id="${CSS.escape(id)}"] .mpick`)?.focus({ preventScroll: true }));
  }
  function keys(e: KeyboardEvent): void {
    if (items.length === 0 || !(e.target instanceof HTMLElement) || !e.target.classList.contains('mpick')) return;
    const id = e.target.closest<HTMLElement>('.mrow')?.dataset.id;
    const i = id === undefined ? undefined : index.get(id);
    if (i === undefined) return;
    const page = Math.max(1, Math.floor(viewport / guess) - 1);
    let n = i;
    if (e.key === 'ArrowDown') n = i + 1;
    else if (e.key === 'ArrowUp') n = i - 1;
    else if (e.key === 'PageDown') n = i + page;
    else if (e.key === 'PageUp') n = i - page;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = items.length - 1;
    else return;
    e.preventDefault();
    go(Math.max(0, Math.min(items.length - 1, n)));
  }

  const toggle = (id: string): void => {
    if (ui.expanded.has(id)) ui.expanded.delete(id);
    else ui.expanded.add(id);
  };
</script>

<!-- svelte-ignore a11y_interactive_supports_focus: focus lives on the roving radio buttons inside -->
<div class="rb-list" role="radiogroup" aria-label="GPU offers" aria-busy={cloud.refreshing || undefined} data-busy={cloud.refreshing || undefined}
  bind:this={scroller} onscroll={schedule} onkeydown={keys}>
  <div class="mhead" aria-hidden="true" bind:this={head}>
    <span></span><span>GPU</span><span>Memory</span><span>Machine</span><span>Location and network</span><span>Price</span>{#if job}<span class="r">This job</span>{/if}<span></span>
  </div>
  {#if cloud.browseState === 'error' && items.length === 0}
    <div class="banner bad" role="alert">
      <Icon name="alert" /><span class="grow">{cloud.browseError ?? 'Could not load offers'}</span>
      <button type="button" class="btn quiet" onclick={() => cloud.refresh()}>Try again</button>
    </div>
  {:else if cloud.browseState !== 'ready' && items.length === 0}
    <div class="rb-skels" aria-hidden="true"><Skeleton rows={8} /></div>
  {:else if items.length === 0}
    <div class="empty">
      <span class="gt mute"><Icon name="filter" /></span>
      <h3 class="h">No offers match these filters</h3>
      <p>Loosen a filter or reset them all.</p>
      <button type="button" class="btn quiet" onclick={() => cloud.resetFilters()}>Reset filters</button>
    </div>
  {:else}
    <div class="pad" bind:this={tops} aria-hidden="true"></div>
    {#each windowed as o, k (o.id)}
      <OfferRow offer={o} {job} on={selectedId === o.id} stop={tabId === o.id} index={start + k} size={items.length} open={ui.expanded.has(o.id)}
        {measure} onpick={onselect} onfocus={(id) => (stopId = id)} ontoggle={toggle} />
    {/each}
    <div class="pad" bind:this={bottoms} aria-hidden="true"></div>
    {#if cloud.moreFailed}
      <div class="rb-more">Couldn’t load more <button type="button" class="linkbtn" onclick={() => cloud.loadMore(true)}>Retry</button></div>
    {:else if cloud.paging}
      <div class="rb-more" aria-hidden="true"><Skeleton small width="40%" /></div>
    {/if}
  {/if}
</div>
