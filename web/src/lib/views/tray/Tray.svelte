<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { WHERE_CLOUD } from '../../api/types';
  import { OPS } from '../../choices';
  import { startRun } from '../../run';
  import Icon from '../../ui/Icon.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import { focusLayer, trayHeight } from '../../ui/actions';
  import { cloud } from '../../stores/cloud.svelte';
  import { jobs } from '../../stores/jobs.svelte';
  import { nav } from '../../stores/nav.svelte';
  import { options } from '../../stores/options.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { toast } from '../../stores/toast.svelte';
  import { ui, type Overlay } from '../../stores/ui.svelte';
  import { fmtDur, fmtMin, plural, usd } from '../../util/format';
  import OpList from './OpList.svelte';
  import QualityList from './QualityList.svelte';
  import WhereList from './WhereList.svelte';
  import { plan, qualityLabel } from './plan.svelte';

  type TokenId = 'op' | 'quality' | 'where' | 'settings' | 'run';
  const TRAY_LAYERS: readonly Overlay[] = ['op', 'quality', 'where', 'settings'];

  /* phones (< 600 px) get one compact bar that opens a settings sheet */
  const narrow = matchMedia('(max-width: 599.98px)');
  let phone = $state(narrow.matches);
  $effect(() => {
    const sync = (): void => { phone = narrow.matches; };
    narrow.addEventListener('change', sync);
    return () => narrow.removeEventListener('change', sync);
  });

  const els: Partial<Record<TokenId, HTMLButtonElement | null>> = {};
  let busy = $state(false);

  const scenes = $derived(`${plural(selection.count, 'scene')} · ${fmtDur(selection.info.dur)}`);
  const cloudNow = $derived(options.cloud);
  const open = $derived(ui.overlay);
  const sheetOpen = $derived(open !== null && TRAY_LAYERS.includes(open));
  const chip = $derived(`${OPS[options.op].name} · ${qualityLabel(options.op)} · ${cloudNow ? 'Cloud' : (plan.runner?.name ?? 'No runner')}`);
  const runLabel = $derived(plan.canCloud && cloudNow && plan.quote ? `Run · ~${usd(plan.quote.total)}` : 'Run');

  /* a cloud-capable job keeps its price up to date; this only asks, the cloud store does the work */
  $effect(() => {
    if (!selection.count || !plan.canCloud) return;
    void plan.jobKey;
    untrack(() => cloud.ensure());
  });

  /* keep the layers consistent with what is on screen: no tray, or a different screen size, closes or swaps them */
  $effect(() => {
    const o = ui.overlay;
    if (!selection.count && o !== null && TRAY_LAYERS.includes(o)) ui.overlay = null;
    else if (phone && (o === 'op' || o === 'quality' || o === 'where')) ui.overlay = 'settings';
    else if (!phone && o === 'settings') ui.overlay = null;
  });
  $effect(() => () => {
    const o = ui.overlay;
    if (o !== null && TRAY_LAYERS.includes(o)) ui.overlay = null;
  });

  /* when a layer closes and focus was lost with it (Safari does not focus a clicked button), put it back on the control that opened it */
  let was: Overlay = null;
  $effect(() => {
    const now = ui.overlay;
    const prev = was;
    was = now;
    if (now !== null || prev === null) return;
    const key: TokenId | null = prev === 'picker' ? (phone ? 'settings' : 'where') : prev === 'confirm' ? 'run' : prev === 'replace' ? null : prev;
    const el = key ? els[key] : null;
    if (!el) return;
    void tick().then(() => {
      if (!document.activeElement || document.activeElement === document.body) el.focus({ preventScroll: true });
    });
  });

  async function run(): Promise<void> {
    if (plan.blocker || busy) return;
    if (options.cloud) { ui.overlay = 'confirm'; return; }
    busy = true;
    try {
      const r = await startRun();
      if (!r.ok) toast.show(r.message);
    } finally {
      busy = false;
    }
  }

  const runningNow = $derived(jobs.running.slice(0, 2));
</script>

<section class={['tray', !selection.count && 'now']} aria-label={selection.count ? 'Run settings' : 'Running now'} use:trayHeight>
  {#if selection.count}
    {#if phone}
      <div class="trow">
        <div class="twrap grow">
          <button type="button" class="tok full" aria-haspopup="dialog" aria-expanded={open === 'settings'} bind:this={els.settings}
            onclick={() => ui.toggle('settings')}>
            <Icon name="sliders" /><span class="tl">{chip}</span><Icon name="chevD" small />
          </button>
          {#if open === 'settings'}
            <div class="tpop pop sheet enter" role="dialog" aria-label="Settings" use:focusLayer>
              <div class="fgrp"><span class="h">What</span><OpList /></div>
              <div class="fgrp"><span class="h">Quality</span><QualityList /></div>
              <div class="fgrp"><span class="h">Where</span><WhereList /></div>
              <div class="sheetfoot"><button type="button" class="btn" onclick={() => ui.back()}>Done</button></div>
            </div>
          {/if}
        </div>
      </div>
      <div class="trow">
        <div class="col grow" style="gap: 2px">
          <span class="row" style="gap: var(--lu-space-1)">
            <span class="cap muted">{scenes}</span>
            <button type="button" class="iconbtn sm" aria-label="Clear selection" onclick={() => selection.clear()}><Icon name="x" small /></button>
          </span>
          <span class="cap" id="tray-sum">{@render summary(true)}</span>
        </div>
        {@render runButton()}
      </div>
    {:else}
      <div class="trow">
        <span class="tok sel">
          <span class="tl">{scenes}</span>
          <button type="button" class="iconbtn sm" aria-label="Clear selection" onclick={() => selection.clear()}><Icon name="x" small /></button>
        </span>
        <div class="twrap">
          <button type="button" class="tok" aria-haspopup="dialog" aria-expanded={open === 'op'} bind:this={els.op} onclick={() => ui.toggle('op')}>
            <Icon name={OPS[options.op].icon} /><span class="tl">{OPS[options.op].name}</span><Icon name="chevD" small />
          </button>
          {#if open === 'op'}
            <div class="tpop pop enter" role="dialog" aria-label="What to do" use:focusLayer><OpList /></div>
          {/if}
        </div>
        <div class="twrap">
          <button type="button" class="tok" aria-haspopup="dialog" aria-expanded={open === 'quality'} bind:this={els.quality} onclick={() => ui.toggle('quality')}>
            <Icon name="sliders" /><span class="tl">{qualityLabel(options.op)}</span><Icon name="chevD" small />
          </button>
          {#if open === 'quality'}
            <div class="tpop pop enter" role="dialog" aria-label="Quality" use:focusLayer><QualityList /></div>
          {/if}
        </div>
        <div class="twrap">
          <button type="button" class="tok" aria-haspopup="dialog" aria-expanded={open === 'where'} bind:this={els.where} onclick={() => ui.toggle('where')}>
            <Icon name={plan.whereIcon} /><span class="tl">{plan.whereLabel}</span><Icon name="chevD" small />
          </button>
          {#if open === 'where'}
            <div class="tpop pop end enter" role="dialog" aria-label="Where to run" use:focusLayer><WhereList /></div>
          {/if}
        </div>
        {@render runButton()}
      </div>
      <div class="tsum cap">
        <span id="tray-sum">{@render summary(false)}</span>
        <span class="grow"></span>
        {@render nudge()}
      </div>
    {/if}
  {:else}
    <div class="trow">
      <div class="col grow">
        <b>{jobs.activeCount ? 'Pick scenes to start another job' : 'Pick scenes to start a job'}</b>
        <span class="cap muted">Tap a scene to select it. The tray appears here.</span>
      </div>
      <button type="button" class="btn" onclick={() => nav.go('jobs')}>
        <Icon name="bolt" />{jobs.running.length ? `${jobs.running.length} running` : jobs.waiting.length ? `${jobs.waiting.length} waiting` : 'Jobs'}<Icon name="chevR" small />
      </button>
    </div>
    {#if runningNow.length || jobs.reviewCount}
      <div class="tsum cap">
        {#each runningNow as j (j.id)}
          <span class="st run"><i class="dot"></i><span class="ell tjob">{j.scene_title ?? `Scene ${j.scene_id}`}</span><span class="nw">· {Math.round(j.progress * 100)}%{j.cloud ? ` · ${usd(j.cloud_usd ?? 0)} so far` : j.runner ? ` · ${j.runner}` : ''}</span></span>
        {/each}
        {#if jobs.running.length > runningNow.length}<span class="muted nw">+{jobs.running.length - runningNow.length} more</span>{/if}
        <span class="grow"></span>
        {#if jobs.reviewCount}
          <button type="button" class="linkbtn" onclick={() => nav.go('review')}><Icon name="eye" small />{jobs.reviewCount} to review<Icon name="chevR" small /></button>
        {/if}
      </div>
    {/if}
  {/if}
</section>

{#snippet runButton()}
  <button type="button" class="btn primary run" aria-disabled={!!plan.blocker || busy || undefined} aria-describedby="tray-sum" bind:this={els.run} onclick={run}>
    {#if cloudNow}<Icon name="coin" />{/if}{runLabel}
  </button>
{/snippet}

{#snippet summary(short: boolean)}
  {#if cloudNow && plan.waiting}
    <Skeleton small width="22ch" />
  {:else if plan.why}
    <span class="warn row" style="gap: 6px"><Icon name="alert" small />{plan.why}{cloudNow ? '' : '. Pick another quality or runner.'}</span>
  {:else if cloudNow && plan.quote && plan.pick}
    <span>{short ? 'About ' : 'Takes about '}<b class="nw">{fmtMin(plan.quote.minutes)}</b> <span class="dotsep">·</span>
      <span class="nw">{short ? '~' : 'costs about '}<b>{usd(plan.quote.total)}</b>
        <span class="tip" data-id={plan.pick.offer.id}>
          <button type="button" class="iconbtn" aria-label="How this price is worked out" aria-expanded={ui.receipt === plan.pick.offer.id}
            onclick={() => { const id = plan.pick?.offer.id ?? null; ui.receipt = ui.receipt === id ? null : id; }}><Icon name="info" small /></button>
        </span></span></span>
  {:else if !cloudNow && plan.home.ok}
    <span>{short ? 'About ' : 'Takes about '}<b class="nw">{fmtMin(plan.home.min)}</b> <span class="dotsep">·</span> <span class="nw"><b>Free</b></span></span>
  {/if}
{/snippet}

{#snippet nudge()}
  {#if plan.canCloud}
    {#if cloudNow}
      {#if plan.alt.ok}
        <button type="button" class="linkbtn" onclick={() => (options.where = null)}>
          <Icon name="monitor" small />Run it at home: {fmtMin(plan.alt.min)} · Free<Icon name="chevR" small />
        </button>
      {/if}
    {:else if plan.waiting}
      <Skeleton small width="16ch" />
    {:else if plan.quote}
      <button type="button" class="linkbtn" onclick={() => (options.where = WHERE_CLOUD)}>
        <Icon name="cloud" small />Cloud: {fmtMin(plan.quote.minutes)} · ~{usd(plan.quote.total)}<Icon name="chevR" small />
      </button>
    {/if}
  {/if}
{/snippet}
