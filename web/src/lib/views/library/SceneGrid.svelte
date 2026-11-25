<script lang="ts">
  import { onMount, tick } from 'svelte';
  import type { Scene } from '../../api/types';
  import Skeleton from '../../ui/Skeleton.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { model } from './model.svelte';
  import SceneTile from './SceneTile.svelte';

  /* Only the rows near the viewport are in the DOM; spacer padding stands in for the rest. The column count
     follows the width, the row height is measured once per width from a real tile (every tile is the same
     height: one line of title, one of detail). Scroll work is plain variables; state changes only when the
     window of rows moves. */
  const MIN_TILE = 280;
  const OVERSCAN = 3;
  const NEAR_END = 2;
  const SKELETON_ROWS = 3;

  let root: HTMLDivElement;
  let scroller: HTMLElement | undefined;
  let offset = 0; // top of the grid inside the scroller
  let viewH = 0;
  let inset = 0; // bottom padding of the scroller: the tray covers it
  let gap = 16;
  let frame = 0;
  let prevFirst = -1;
  let prevLast = -2;
  let anchor: number | null = null;

  let cols = $state(3);
  let tileW = $state(280);
  /** one row, tile + gap, in px */
  let stride = $state(260);
  let first = $state(0);
  let last = $state(-1);
  let tabIdx = $state(model.tabIdx);

  const items = $derived(model.visible);
  const rows = $derived(Math.ceil(items.length / cols));
  const base = $derived(first * cols);
  const slice = $derived(items.slice(base, (last + 1) * cols));
  const bottom = $derived(Math.max(0, rows - 1 - last) * stride);
  /* Tab lands on the remembered tile when it is drawn, otherwise on the first row in view */
  const tabbable = $derived.by(() => {
    const lo = base;
    const hi = Math.min(items.length, (last + 1) * cols) - 1;
    if (tabIdx >= lo && tabIdx <= hi) return tabIdx;
    return Math.min(hi, (first > 0 ? first + OVERSCAN : 0) * cols);
  });
  const showSkeleton = $derived((model.phase === 'loading' && model.scenes.length === 0) || (items.length === 0 && model.hasMore && model.more !== 'error' && model.phase === 'ready'));
  const failed = $derived(items.length === 0 && (model.phase === 'error' || model.more === 'error'));

  function measure(): void {
    if (!scroller) return;
    const cs = getComputedStyle(root);
    gap = parseFloat(cs.rowGap) || 16;
    const w = root.clientWidth;
    const phone = document.documentElement.dataset.luProfile === 'phone';
    const c = phone ? 2 : Math.max(1, Math.floor((w + gap) / (MIN_TILE + gap)));
    const tw = (w - gap * (c - 1)) / c;
    if (c !== cols) cols = c;
    if (Math.abs(tw - tileW) > 0.5) {
      tileW = tw;
      stride = (tw * 9) / 16 + 56 + gap; // until a real tile is measured
    }
    offset = root.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    viewH = scroller.clientHeight;
    inset = parseFloat(getComputedStyle(scroller).paddingBottom) || 0;
    update();
  }

  function update(): void {
    if (!scroller) return;
    const n = Math.ceil(items.length / cols);
    const y = scroller.scrollTop - offset;
    let f = Math.floor(y / stride) - OVERSCAN;
    let l = Math.ceil((y + viewH) / stride) + OVERSCAN;
    f = Math.min(Math.max(0, f), Math.max(0, n - 1));
    l = Math.min(n - 1, Math.max(l, f));
    if (f !== prevFirst || l !== prevLast) {
      prevFirst = f;
      prevLast = l;
      first = f;
      last = l;
    }
  }

  onMount(() => {
    const el = root.closest<HTMLElement>('.a-main') ?? root.parentElement;
    if (!el) return;
    scroller = el;
    let alive = true;
    const onscroll = (): void => {
      model.scrollTop = el.scrollTop;
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; update(); });
    };
    el.addEventListener('scroll', onscroll, { passive: true });
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    ro.observe(root);
    if (root.parentElement) ro.observe(root.parentElement);
    model.onReset = () => {
      el.scrollTop = 0;
      model.scrollTop = 0;
      anchor = null;
      tabIdx = 0;
    };
    measure();
    /* put the person back where they were; after the shell's own scroll-to-top for a page change */
    void tick().then(() => requestAnimationFrame(() => {
      if (!alive) return;
      el.scrollTop = model.scrollTop;
      update();
    }));
    return () => {
      alive = false;
      el.removeEventListener('scroll', onscroll);
      ro.disconnect();
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      model.onReset = null;
      model.tabIdx = tabIdx;
    };
  });

  /* the window follows the list and the geometry */
  $effect(() => {
    void items.length;
    void cols;
    void stride;
    update();
  });

  /* one real tile gives the true row height for this width */
  $effect(() => {
    void tileW;
    void cols;
    if (items.length === 0) return;
    const t = root.querySelector<HTMLElement>('.grid > .tile');
    if (!t) return;
    const s = t.offsetHeight + gap;
    if (Math.abs(s - stride) > 0.5) stride = s;
  });

  /* near the end of what is loaded: fetch the next page */
  $effect(() => {
    if (model.phase !== 'ready' || !model.hasMore) return;
    if (last >= rows - 1 - NEAR_END) model.loadMore();
  });

  function pick(s: Scene, i: number, shift: boolean): void {
    tabIdx = i;
    if (shift && anchor !== null && anchor < items.length) {
      selection.add(items.slice(Math.min(anchor, i), Math.max(anchor, i) + 1));
    } else {
      selection.toggle(s);
      anchor = i;
    }
  }

  async function focusIndex(n: number): Promise<void> {
    if (!scroller) return;
    tabIdx = n;
    const top = offset + Math.floor(n / cols) * stride;
    const visibleH = viewH - inset;
    if (top < scroller.scrollTop) scroller.scrollTop = top - 8;
    else if (top + stride > scroller.scrollTop + visibleH) scroller.scrollTop = top + stride - visibleH + 8;
    update();
    await tick();
    root.querySelector<HTMLElement>(`.grid > [data-i="${n}"]`)?.focus({ preventScroll: true });
  }

  function onkeydown(e: KeyboardEvent): void {
    const el = e.target;
    if (!(el instanceof HTMLElement) || el.dataset.i === undefined) return;
    const i = Number(el.dataset.i);
    const len = items.length;
    const rowStart = i - (i % cols);
    const page = Math.max(1, Math.floor((viewH - inset) / stride)) * cols;
    let n: number;
    switch (e.key) {
      case 'ArrowRight': n = i + 1; break;
      case 'ArrowLeft': n = i - 1; break;
      case 'ArrowDown': n = i + cols >= len ? (Math.floor(i / cols) < rows - 1 ? len - 1 : i) : i + cols; break;
      case 'ArrowUp': n = i - cols; break;
      case 'PageDown': n = Math.min(len - 1, i + page); break;
      case 'PageUp': n = Math.max(0, i - page); break;
      case 'Home': n = e.ctrlKey ? 0 : rowStart; break;
      case 'End': n = e.ctrlKey ? len - 1 : Math.min(len - 1, rowStart + cols - 1); break;
      default: return;
    }
    if (n < 0 || n >= len) return;
    e.preventDefault();
    if (e.shiftKey && !e.ctrlKey) {
      anchor ??= i;
      selection.add(items.slice(Math.min(anchor, n), Math.max(anchor, n) + 1));
    }
    if (n !== i) void focusIndex(n);
  }

  function onfocusin(e: FocusEvent): void {
    const t = e.target;
    if (t instanceof HTMLElement && t.dataset.i !== undefined) tabIdx = Number(t.dataset.i);
  }

  function retry(): void {
    if (model.phase === 'error') model.reload();
    else model.retryMore();
  }
</script>

<div class="lib-win" bind:this={root}>
  {#if failed}
    <div class="lib-state" role="alert">
      <b>Couldn't load scenes</b>
      <span>{model.error}</span>
      <button type="button" class="btn" onclick={retry}>Retry</button>
    </div>
  {:else if showSkeleton}
    <div class="grid" style:--cols={cols} aria-hidden="true">
      {#each { length: cols * SKELETON_ROWS } as _, i (i)}
        <div class="sk-tile"><span class="skel sk-pic"></span><Skeleton small width="70%" /><Skeleton small width="40%" /></div>
      {/each}
    </div>
  {:else if items.length === 0}
    <div class="lib-state">
      <b>No scenes match</b>
      {#if model.filtered}
        <span>Try a different search or loosen the filters.</span>
        <button type="button" class="btn" onclick={() => model.clearFilters()}>Clear filters</button>
      {:else}
        <span>The library is empty.</span>
      {/if}
    </div>
  {:else}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class={['grid', model.phase === 'loading' && 'busy']} role="group" aria-label="Scenes" aria-busy={model.phase === 'loading'}
      style:--cols={cols} style:padding-top="{first * stride}px" style:padding-bottom="{bottom}px" {onkeydown} {onfocusin}>
      {#each slice as s, k (s.id)}
        <SceneTile scene={s} index={base + k} selected={selection.has(s.id)} tabbable={base + k === tabbable} onpick={pick} />
      {/each}
    </div>
    {#if model.more === 'loading'}
      <div class="grid" style:--cols={cols} aria-hidden="true">
        {#each { length: cols } as _, i (i)}
          <div class="sk-tile"><span class="skel sk-pic"></span><Skeleton small width="70%" /><Skeleton small width="40%" /></div>
        {/each}
      </div>
    {:else if model.more === 'error'}
      <div class="lib-more" role="alert">
        <span>Couldn't load more scenes</span>
        <button type="button" class="btn" onclick={retry}>Retry</button>
      </div>
    {/if}
  {/if}
</div>
