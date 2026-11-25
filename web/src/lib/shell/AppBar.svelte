<script lang="ts">
  import Icon from '../ui/Icon.svelte';
  import type { IconName } from '../ui/icons';
  import ThemeMenu from '../themes/ThemeMenu.svelte';
  import { nav, type View } from '../stores/nav.svelte';
  import { jobs } from '../stores/jobs.svelte';
  import { runners } from '../stores/runners.svelte';
  import WakeButton from './WakeButton.svelte';
  import { budget } from '../stores/budget.svelte';
  import type { Runner } from '../api/types';
  import { usd } from '../util/format';

  interface Tab { view: View; label: string; n: number }
  const tabs: Tab[] = $derived([
    { view: 'library', label: 'Library', n: 0 },
    { view: 'jobs', label: 'Jobs', n: jobs.activeCount },
    { view: 'review', label: 'Review', n: jobs.reviewCount },
  ]);

  /* Two runner pills at most (machines that are up come first); the rest sit behind a "+N" pill. */
  const ranked = $derived([...runners.list].sort((a, b) => Number(b.online) - Number(a.online)));
  const shown = $derived(ranked.slice(0, 2));
  const more = $derived(Math.max(0, ranked.length - shown.length));

  const iconOf = (r: Runner): IconName => (r.kind === 'nas' || r.kind === 'server' ? 'server' : 'cpu');
  const load = (r: Runner): number => Math.max(0, Math.min(100, Math.round(runners.meter(r.url)?.gpu?.util ?? 0)));
  function label(r: Runner): string {
    if (!r.online) return `${r.name}: ${r.asleep ? 'asleep' : 'offline'}`;
    return `${r.name} ${load(r)}%, ${r.busy ? 'busy' : 'ready'}`;
  }
  const cloudLabel = $derived(
    !budget.connected && budget.settings ? 'Cloud: not set up' : budget.left === null ? 'Cloud' : `Cloud: ${usd(budget.left)} left to spend`,
  );
</script>

<header class="appbar">
  <span class="brand"><Icon name="mark" /><span class="bt">Stashify</span></span>
  <nav class="apptabs" aria-label="Sections">
    {#each tabs as t (t.view)}
      <button type="button" class="tab" aria-current={nav.view === t.view ? 'page' : undefined} onclick={() => nav.go(t.view)}>{t.label}{#if t.n}<span class="count quiet">{t.n}</span>{/if}</button>
    {/each}
  </nav>
  <div class="pills">
    {#each shown as r (r.url)}
      {#if r.online}
        <button type="button" class="hp" aria-label={label(r)} onclick={() => nav.go('runners')}>
          <Icon name={iconOf(r)} /><span class="nm">{r.name}</span>
          <span class="mini"><i style:width="{load(r)}%"></i></span><span class="num">{load(r)}%</span>
        </button>
      {:else}
        <!-- not up: the pill holds its own wake button, so it is a container, not a button (buttons cannot nest) -->
        <span class="hp hpx" role="group" aria-label={label(r)}>
          <button type="button" class="hpn" aria-label={`${r.name}: open Runners`} onclick={() => nav.go('runners')}><Icon name={iconOf(r)} /><span class="nm">{r.name}</span></button>
          <WakeButton runner={r} />
          <span class="wd">{runners.wakes[r.url] && !runners.wakes[r.url].failed ? `Waking… ${runners.wakeLeft(r.url) ?? ''}${runners.wakeLeft(r.url) ? ' s' : ''}` : runners.wakes[r.url]?.failed ? 'Didn’t wake' : r.asleep ? 'Asleep' : 'Offline'}</span>
        </span>
      {/if}
    {/each}
    {#if more}
      <button type="button" class="hp" aria-label={`${more} more runner${more > 1 ? 's' : ''}`} onclick={() => nav.go('runners')}><span class="num">+{more}</span></button>
    {/if}
    <button type="button" class="hp" aria-label={cloudLabel} onclick={() => nav.go('cloud')}>
      <Icon name="cloud" /><span class="nm">Cloud</span>
      {#if budget.settings && !budget.connected}
        <span class="wd">Set up</span>
      {:else if budget.left !== null}
        <span class="num">{usd(budget.left)}{' '}<span class="wd">left</span></span>
      {/if}
    </button>
  </div>
  <ThemeMenu />
</header>
