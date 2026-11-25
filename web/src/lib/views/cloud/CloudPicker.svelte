<script lang="ts">
  /* The cloud picker: four machines chosen for this job, the month's budget, and a way into the whole list. */
  import { budget } from '../../stores/budget.svelte';
  import { cloud } from '../../stores/cloud.svelte';
  import { nav } from '../../stores/nav.svelte';
  import { options } from '../../stores/options.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Icon from '../../ui/Icon.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import Status from '../../ui/Status.svelte';
  import Switch from '../../ui/Switch.svelte';
  import { fmtMin, plural, usd } from '../../util/format';
  import { roving } from '../tray/roving';
  import BudgetBar from './BudgetBar.svelte';
  import CostTip from './CostTip.svelte';
  import { agoText, clock, liveStatus } from './clock.svelte';
  import { ROLE_ICON, TIER_ICON, TIER_LABEL, city, fmtN, gpuName, jobName, vramTotal, whyNot } from './offerText';

  $effect(() => { cloud.ensure(); });
  $effect(() => clock.use());

  const tiles = $derived(cloud.recs.flatMap((rec) => {
    const offer = cloud.offerById(rec.offerId);
    return offer ? [{ rec, offer }] : [];
  }));
  const pick = $derived(cloud.picked);
  const pickedId = $derived(pick?.offer.id ?? null);
  /** the tile Tab lands on: the chosen one, else the first */
  const stopId = $derived(tiles.some((t) => t.offer.id === pickedId) ? pickedId : (tiles[0]?.offer.id ?? null));
  const live = $derived(liveStatus(cloud.state === 'error' ? 'error' : cloud.state === 'idle' || cloud.state === 'loading' ? 'busy' : 'ready', cloud.pickerAt, clock.now));
  const via = $derived(cloud.pickerProviders.filter((p) => p.ok).map((p) => p.name).join(' · '));
  const limit = $derived(budget.caps?.maxPrice ?? 0);

  function settings(): void {
    ui.overlay = null;
    ui.browse = false;
    nav.go('cloud');
  }
</script>

<div class="picker">
  <div class="rb-h">
    <div class="rt">
      <h2 class="ttl">Pick a cloud GPU</h2>
      <div class="rb-sub">
        <span class="live"><Status state={live.state} word={live.word} />{#if cloud.pickerAt > 0}{agoText(cloud.pickerAt, clock.now)}{/if}</span>
        {#if via}<span>via {via}</span>{/if}
        {#if selection.count > 0}<span>{plural(selection.count, 'scene')} · {jobName(options.op, options.q)}</span>{/if}
      </div>
    </div>
    <button type="button" class="iconbtn" aria-label="Refresh offers" onclick={() => cloud.refresh()}><Icon name="refresh" /></button>
  </div>

  <div class="setrow">
    <div class="sl"><b>Data centres only</b><span>Always-on hosts with the best uptime. Usually a little dearer.</span></div>
    <Switch checked={cloud.dcOnly} label="Data centres only" onchange={(v) => { void cloud.setDcOnly(v); }} />
  </div>

  {#if selection.count === 0}
    <div class="empty"><p>Pick a scene first, then choose a machine for it.</p></div>
  {:else if cloud.state === 'error'}
    <div class="banner bad" role="alert">
      <Icon name="alert" /><span class="grow">{cloud.error ?? 'Could not load machines'}</span>
      {#if cloud.errorCode === 'no_key'}<button type="button" class="btn quiet" onclick={settings}>Open cloud settings</button>{/if}
      <button type="button" class="btn quiet" onclick={() => cloud.refresh()}>Try again</button>
    </div>
  {:else if cloud.state === 'empty'}
    <div class="empty">
      <span class="gt warn"><Icon name="coin" /></span>
      <h3 class="h">No machine fits under your limits</h3>
      <p>Nothing that can run this job costs less than {usd(limit)}/h{cloud.dcOnly ? ' in data centres' : ''}. Raise the limit, include community hosts, or run it at home.</p>
      <div class="row">
        {#if cloud.dcOnly}<button type="button" class="btn" onclick={() => { void cloud.setDcOnly(false); }}>Include community hosts</button>{/if}
        <button type="button" class="btn quiet" onclick={settings}>Change limits</button>
      </div>
    </div>
  {:else if cloud.state === 'ready'}
    {#if tiles.length > 0}
      <div class="recs" role="radiogroup" aria-label="Recommended machines" data-n={tiles.length} use:roving>
        {#each tiles as t (t.offer.id)}
          {@const o = t.offer}
          {@const bad = whyNot(o)}
          {@const est = o.estimate}
          <div class="recw">
            <button type="button" class="rec" role="radio" aria-checked={pickedId === o.id} aria-disabled={bad !== null || undefined}
              tabindex={stopId === o.id ? 0 : -1} onclick={() => { if (bad === null) cloud.choose(o); }}>
              <span class="rh"><Icon name={ROLE_ICON[t.rec.role]} />{t.rec.tags.join(' · ')}<span class="radio" aria-hidden="true"><Icon name="check" /></span></span>
              <span class="rg">{gpuName(o)}</span>
              <span class="rl"><Icon name={TIER_ICON[o.tier]} small />{vramTotal(o)} GB · {TIER_LABEL[o.tier]} · {o.provider}</span>
              <span class="rl"><Icon name="shieldCheck" small />{o.reliability}% reliable · {usd(o.pricePerHour)}/h</span>
              {#if bad}
                <span class="rwhy">{bad}</span>
              {:else if est}
                <span class="rn"><span class="rt">{fmtMin(est.minutes)}</span><span class="rp">~{usd(est.total)}</span></span>
              {/if}
            </button>
            {#if est && !bad}<CostTip id={o.id} />{/if}
          </div>
        {/each}
      </div>
    {:else}
      <p class="cap muted">No recommendations right now. Browse all machines to pick one yourself.</p>
    {/if}
  {:else}
    <div class="recs" data-n="4" aria-hidden="true"><Skeleton rows={4} /></div>
  {/if}

  <button type="button" class="adv" onclick={() => (ui.browse = true)}>
    <Icon name="filter" />Advanced<span class="cap">{cloud.pickerAll > 0 ? `Browse all ${fmtN(cloud.pickerAll)} offers` : 'Browse all machines'}, filters and sorting</span><Icon name="chevR" />
  </button>

  <div class="rb-f">
    <div class="sel">
      {#if pick}
        {@const o = pick.offer}
        <span class="gt sm"><Icon name={TIER_ICON[o.tier]} /></span>
        <div class="col grow">
          <b class="ell">{gpuName(o)} · {city(o)}</b>
          <span class="cap muted ell">{TIER_LABEL[o.tier]} · {o.provider} · {fmtMin(pick.quote.minutes)}</span>
        </div>
        <div class="col end">
          <span class="rp2">~{usd(pick.quote.total)}<CostTip id={o.id} /></span>
          <span class="cap muted">estimated total</span>
        </div>
      {:else}
        <span class="gt sm mute"><Icon name="cloud" /></span>
        <div class="col grow"><b>No machine chosen</b><span class="cap muted">Pick one above to see what it costs.</span></div>
      {/if}
    </div>
    <div class="bud"><BudgetBar extra={pick?.quote.total ?? 0} /></div>
  </div>
</div>
