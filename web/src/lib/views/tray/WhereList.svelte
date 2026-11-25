<script lang="ts">
  import { WHERE_CLOUD, type Runner } from '../../api/types';
  import { OPS } from '../../choices';
  import Icon from '../../ui/Icon.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import Status, { type StatusState } from '../../ui/Status.svelte';
  import { cloud } from '../../stores/cloud.svelte';
  import { nav } from '../../stores/nav.svelte';
  import { options } from '../../stores/options.svelte';
  import { runners } from '../../stores/runners.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { ui } from '../../stores/ui.svelte';
  import { fmtMin, usd } from '../../util/format';
  import { homeFor, plan, runnerIcon } from './plan.svelte';
  import { roving } from './roving';

  /** live state of a runner as glyph + word */
  function stateOf(r: Runner): { state: StatusState; word: string } {
    if (r.paused) return { state: 'warn', word: 'Paused' };
    if (!r.online) {
      if (!r.asleep) return { state: 'offline', word: 'Offline' };
      return { state: 'asleep', word: r.can_wake ? `Asleep · wakes in ~${Math.max(1, Math.round((r.wake_eta_s ?? 40) / 10) * 10)} s` : 'Asleep' };
    }
    if (r.busy) return { state: 'busy', word: r.queue_eta_s ? `Busy · free in ~${fmtMin(r.queue_eta_s / 60)}` : 'Busy' };
    return { state: 'ok', word: 'Ready' };
  }

  const rows = $derived(
    runners.list.map((r) => {
      const est = homeFor(selection.info, options.op, options.q, r);
      return { r, est, st: stateOf(r), checked: !options.cloud && plan.runner?.url === r.url, disabled: !est.ok };
    }),
  );
  const cloudOff = $derived(!plan.canCloud);
  /* one Tab stop: the chosen row, or the first one that can be chosen */
  const stop = $derived(
    rows.find((x) => x.checked && !x.disabled)?.r.url ?? (options.cloud && !cloudOff ? WHERE_CLOUD : (rows.find((x) => !x.disabled)?.r.url ?? (cloudOff ? undefined : WHERE_CLOUD))),
  );
  const offer = $derived(plan.pick?.offer);
</script>

<div class="opt-list" role="radiogroup" aria-label="Where to run" use:roving>
  {#if !runners.loaded}
    <Skeleton rows={2} />
  {/if}
  {#each rows as x (x.r.url)}
    <button type="button" class="opt" role="radio" aria-checked={x.checked} aria-disabled={x.disabled || undefined} tabindex={x.r.url === stop ? 0 : -1}
      onclick={() => { if (!x.disabled) options.where = x.r.url; }}>
      <span class="gt sm"><Icon name={runnerIcon(x.r)} /></span>
      <span class="om"><span class="on">{x.r.name}</span><span class="oh">{#if x.r.gpu}{x.r.gpu} · {/if}<Status state={x.st.state} word={x.st.word} /></span></span>
      <span class="oe">{#if x.disabled}<b class="warn">{x.est.why}</b>{:else}<b>{fmtMin(x.est.min)}</b><span>Free</span>{/if}</span>
      <span class="radio"><Icon name="check" /></span>
    </button>
  {/each}

  <button type="button" class="opt" role="radio" aria-checked={options.cloud} aria-disabled={cloudOff || undefined} tabindex={stop === WHERE_CLOUD ? 0 : -1}
    onclick={() => { if (cloudOff) return; if (options.cloud) ui.overlay = 'picker'; else options.where = WHERE_CLOUD; }}>
    <span class="gt sm"><Icon name="cloud" /></span>
    <span class="om">
      <span class="on">Cloud GPU</span>
      <span class="oh">
        {#if cloudOff}Rent a card by the hour · <b class="warn">Not for {OPS[options.op].name}</b>
        {:else if cloud.blocked}<Status state="warn" word={cloud.blocked} />
        {:else if plan.waiting}<Skeleton small width="18ch" />
        {:else if offer}{offer.gpu}{offer.count > 1 ? ` × ${offer.count}` : ''} · {offer.location.split(',')[0]} · {usd(offer.pricePerHour)}/h
        {:else}Rent a card by the hour{/if}
      </span>
    </span>
    <span class="oe">
      {#if !cloudOff && plan.waiting}<Skeleton small width="5ch" />
      {:else if !cloudOff && plan.quote}<b>~{usd(plan.quote.total)}</b><span>{fmtMin(plan.quote.minutes)}</span>{/if}
    </span>
    <span class="radio"><Icon name="check" /></span>
  </button>
</div>

<div class="optx">
  {#if !cloudOff}
    <button type="button" class="linkbtn" onclick={() => (ui.overlay = 'picker')}><Icon name="cloud" small />{offer ? 'Change machine' : 'See machines'}<Icon name="chevR" small /></button>
  {/if}
  {#if runners.loaded && !runners.list.length}
    <button type="button" class="linkbtn" onclick={() => nav.go('runners')}><Icon name="server" small />Set up a runner<Icon name="chevR" small /></button>
  {/if}
</div>
