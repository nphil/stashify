<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { MOCK } from './lib/api/client';
  import { applyMockState } from './lib/mockState.svelte';
  import AppBar from './lib/shell/AppBar.svelte';
  import BottomNav from './lib/shell/BottomNav.svelte';
  import MockBar from './lib/shell/MockBar.svelte';
  import Toast from './lib/shell/Toast.svelte';
  import { budget } from './lib/stores/budget.svelte';
  import { jobs } from './lib/stores/jobs.svelte';
  import { nav, type View } from './lib/stores/nav.svelte';
  import { runners } from './lib/stores/runners.svelte';
  import { ui } from './lib/stores/ui.svelte';
  import CloudHost from './lib/views/cloud/CloudHost.svelte';
  import CloudSettings from './lib/views/cloudsettings/CloudSettings.svelte';
  import EdgeBoard from './lib/views/edge/EdgeBoard.svelte';
  import Jobs from './lib/views/jobs/Jobs.svelte';
  import Library from './lib/views/library/Library.svelte';
  import Review from './lib/views/review/Review.svelte';
  import ReviewDock from './lib/views/review/ReviewDock.svelte';
  import Runners from './lib/views/runners/Runners.svelte';
  import Tray from './lib/views/tray/Tray.svelte';

  const TITLES: Record<View, string> = {
    library: 'Library', jobs: 'Jobs', review: 'Review', runners: 'Runners', cloud: 'Cloud settings', states: 'States',
  };
  /* layers that dim the page behind them (the dialogs dim themselves) */
  const DIMMED = new Set(['picker', 'op', 'quality', 'where', 'settings']);
  const scrim = $derived(ui.overlay !== null && DIMMED.has(ui.overlay));

  let main: HTMLElement;

  onMount(() => {
    const stops = [jobs.start(), runners.start(), budget.start()];
    if (MOCK) {
      const s = new URLSearchParams(location.search).get('s');
      if (s || !location.hash) void applyMockState(s ?? 'compose');
    }
    return () => { for (const stop of stops) stop(); };
  });

  /* a new page: tell screen readers by moving focus to its heading, start at the top, set the tab title */
  $effect(() => {
    document.title = nav.view === 'library' ? 'Stashify' : `${TITLES[nav.view]} · Stashify`;
    if (!nav.stamp) return;
    main.scrollTop = 0;
    void tick().then(() => document.querySelector<HTMLElement>('main h1')?.focus({ preventScroll: true }));
  });

  function onkey(e: KeyboardEvent): void {
    if (e.key === 'Escape' && !e.defaultPrevented && ui.back()) e.preventDefault();
  }
</script>

<svelte:window onkeydown={onkey} />

{#if MOCK}<MockBar />{/if}

<div class="a-shell" data-lu-tier="t1">
  <button type="button" class="sr skip" onclick={() => main.focus()}>Skip to content</button>
  <AppBar />

  <main class="a-main" id="main" tabindex="-1" bind:this={main}>
    <div class="wrap">
      {#if nav.view === 'library'}<Library />
      {:else if nav.view === 'jobs'}<Jobs />
      {:else if nav.view === 'review'}<Review />
      {:else if nav.view === 'runners'}<Runners />
      {:else if nav.view === 'cloud'}<CloudSettings />
      {:else}<EdgeBoard />{/if}
    </div>
  </main>

  {#if scrim}
    <button type="button" class="a-scrim" class:dim={ui.overlay === 'picker'} tabindex="-1" aria-hidden="true" onclick={() => ui.back()}></button>
  {/if}

  {#if nav.view === 'library'}<Tray />{:else if nav.view === 'review'}<ReviewDock />{/if}
  <CloudHost />
  <BottomNav />
  <Toast />
</div>
