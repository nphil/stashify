<script lang="ts">
  /* The all-machines browser: how fresh and how many, a bar of menus and chips, the filters, the windowed list and a footer with the chosen
     machine. With a job (opened from the picker) every machine is priced for it; on the Rent page it is a plain market view. */
  import { REGIONS } from '../../api/cloudAdapter';
  import { WHERE_CLOUD, type Offer } from '../../api/types';
  import { OPS } from '../../choices';
  import { budget } from '../../stores/budget.svelte';
  import { cloud } from '../../stores/cloud.svelte';
  import { options } from '../../stores/options.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Icon from '../../ui/Icon.svelte';
  import Status from '../../ui/Status.svelte';
  import { fmtMin, plural, usd } from '../../util/format';
  import BudgetBar from './BudgetBar.svelte';
  import ChipMenu, { type MenuItem } from './ChipMenu.svelte';
  import CostTip from './CostTip.svelte';
  import FilterRail from './FilterRail.svelte';
  import OfferList from './OfferList.svelte';
  import { agoText, clock, liveStatus } from './clock.svelte';
  import { TIER_ICON, TIER_LABEL, city, feeText, fmtN, gpuName, jobName, whyNot } from './offerText';

  /** `job`: opened from the picker, prices machines for the selected scenes. Without it the page is a plain market view. */
  let { job }: { job: boolean } = $props();

  const FALLBACK_REGIONS = REGIONS.slice(0, 3).map((code) => ({ code, count: 0 }));

  $effect(() => {
    cloud.ensureBrowse(job);
    return () => cloud.releaseBrowse();
  });
  $effect(() => clock.use());

  const withJob = $derived(job && selection.count > 0 && OPS[options.op].cloud && !cloud.unpriced);
  const f = $derived(cloud.filters);

  /* the machine picked here is the run's machine */
  const selectedId = $derived(options.offerId ?? cloud.picked?.offer.id ?? null);
  const sel = $derived(selectedId === null ? null : cloud.offerById(selectedId));
  const quote = $derived(withJob ? (sel?.estimate ?? null) : null);
  const bad = $derived(sel === null ? null : whyNot(sel));
  const can = $derived(sel !== null && bad === null && withJob && quote !== null);

  const live = $derived(liveStatus(cloud.browseState === 'error' ? 'error' : cloud.browseState === 'ready' && !cloud.refreshing ? 'ready' : 'busy', cloud.updatedAt, clock.now));

  const rental: MenuItem[] = $derived([
    { value: 'ondemand', label: 'On-demand', hint: 'Fixed price until the run ends', checked: true },
    { value: 'interruptible', label: 'Interruptible', hint: withJob ? 'Can stop your job halfway' : 'Not offered yet', checked: false, disabled: true },
    { value: 'reserved', label: 'Reserved', hint: 'Not offered yet', checked: false, disabled: true },
  ]);
  const models: MenuItem[] = $derived([
    { value: 'any', label: 'Any GPU', checked: f.model === 'any' },
    ...cloud.modelFacets.map((g) => ({ value: g.name, label: g.name, hint: plural(g.count, 'offer'), checked: f.model === g.name })),
  ]);
  const counts: MenuItem[] = $derived([
    { value: 'any', label: 'Any count', checked: f.count === 'any' },
    ...cloud.countFacets.map((c) => ({ value: String(c.value), label: plural(c.value, 'GPU'), hint: plural(c.count, 'offer'), checked: f.count === c.value })),
  ]);
  const regions: MenuItem[] = $derived([
    { value: 'any', label: 'Any region', checked: f.region === 'any' },
    ...(cloud.facets.regions.length > 0 ? cloud.facets.regions : FALLBACK_REGIONS).map((r) => ({ value: r.code, label: r.code, hint: r.count > 0 ? plural(r.count, 'offer') : undefined, checked: f.region === r.code })),
  ]);
  /** a "per job" sort means nothing without a job: it shows as Auto there */
  const sortId = $derived(withJob || !cloud.sort.startsWith('est_') ? cloud.sort : 'auto');
  const sortLabel = $derived(cloud.sorts.find((s) => s.id === sortId)?.label ?? 'Auto');
  const sorts: MenuItem[] = $derived(cloud.sorts
    .filter((s) => withJob || !s.needsJob)
    .map((s) => ({
      value: s.id,
      label: s.label,
      hint: s.id === 'auto' ? (withJob ? 'Best fit for this job' : 'Performance per $/h') : undefined,
      checked: sortId === s.id,
      disabled: !s.available,
      group: s.id === 'perf_per_dollar' && withJob ? 'Hardware and price' : undefined,
    })));

  function select(o: Offer): void {
    cloud.choose(o);
  }
  function back(): void {
    ui.browse = false;
    ui.menu = null;
    ui.filters = false;
  }
  /** take the machine for the run: the tray goes back to cloud with it */
  function use(): void {
    if (!can) return;
    options.where = WHERE_CLOUD;
    ui.overlay = null;
    back();
  }
</script>

<section class="rb" data-filters={ui.filters || undefined} data-nojob={!withJob || undefined} aria-label="GPU rental browser">
  <div class="rb-h">
    <button type="button" class="iconbtn" aria-label="Back to recommendations" onclick={back}><Icon name="chevL" /></button>
    <div class="rt">
      {#if job}<h2 class="ttl">Rent a GPU</h2>{/if}
      <div class="rb-sub">
        <span class="live"><Status state={live.state} word={live.word} />{#if cloud.updatedAt > 0}{agoText(cloud.updatedAt, clock.now)}{/if}</span>
        <span>{withJob ? `Estimates for ${plural(selection.count, 'scene')} · ${jobName(options.op, options.q)}` : 'Compare GPU rentals across providers'}</span>
        {#if cloud.browseState === 'ready'}
          <span><b>{fmtN(cloud.rowsTotal)}</b> of {fmtN(cloud.rowsAll)} offers{#if withJob && cloud.blockedCount > 0} · {cloud.blockedCount} can’t run this job{/if}</span>
        {/if}
      </div>
    </div>
    {#if job}<button type="button" class="iconbtn" aria-label="Refresh offers" onclick={() => cloud.refresh()}><Icon name="refresh" /></button>{/if}
  </div>

  <div class="rb-top">
    <ChipMenu id="rental" icon="clock" label="On-demand" items={rental} />
    <ChipMenu id="model" icon="cpu" label={f.model === 'any' ? 'Any GPU' : f.model} on={f.model !== 'any'} items={models} onpick={(v) => cloud.setFilter('model', v)} />
    <ChipMenu id="count" icon="layers" label={f.count === 'any' ? 'Any count' : plural(f.count, 'GPU')} on={f.count !== 'any'} items={counts}
      onpick={(v) => cloud.setFilter('count', v === 'any' ? 'any' : Number(v))} />
    <ChipMenu id="region" icon="pin" label={f.region === 'any' ? 'Any region' : f.region} on={f.region !== 'any'} items={regions} onpick={(v) => cloud.setFilter('region', v)} />
    {#if cloud.providers.length > 1}
      {#each cloud.providers as p (p.id ?? p.name)}
        {@const pid = p.id ?? p.name}
        {@const shown = f.providers === null || f.providers.includes(pid)}
        <button type="button" class="chip" data-on={shown || undefined} aria-pressed={shown} onclick={() => cloud.toggleProvider(pid)}>{p.name}</button>
      {/each}
    {/if}
    {#if withJob}
      <button type="button" class="chip" data-on={!f.cantRun || undefined} aria-pressed={!f.cantRun} onclick={() => cloud.setFilter('cantRun', !f.cantRun)}>
        <Icon name="eye" small />Hide can’t-run{cloud.blockedCount > 0 ? ` · ${cloud.blockedCount}` : ''}
      </button>
    {/if}
    <span class="sp"></span>
    <ChipMenu id="sort" icon="sort" end label={`Sort: ${sortLabel}`} items={sorts} onpick={(v) => (cloud.sort = v)} />
    <button type="button" class="chip fchip" data-on={cloud.activeCount > 0 || undefined} aria-expanded={ui.filters} onclick={() => (ui.filters = !ui.filters)}>
      <Icon name="filter" small />Filters{cloud.activeCount > 0 ? ` · ${cloud.activeCount}` : ''}
    </button>
  </div>

  {#if job && cloud.unpriced}
    <div class="banner inf" role="status"><Icon name="info" /><span>This server prices one scene at a time, so estimates are hidden. Pick a single scene to see prices.</span></div>
  {/if}

  <div class="rb-main">
    <FilterRail />
    <OfferList job={withJob} {selectedId} onselect={select} />
  </div>

  <div class="rb-f">
    <div class="sel">
      {#if sel}
        <span class="gt sm"><Icon name={TIER_ICON[sel.tier]} /></span>
        <div class="col grow">
          <b class="ell">{gpuName(sel)} · {city(sel)}</b>
          <span class="cap muted ell">{TIER_LABEL[sel.tier]} · {sel.provider} · {sel.reliability}% reliable</span>
        </div>
        <div class="col end">
          {#if quote}
            <span class="rp2">~{usd(quote.total)}<CostTip id={sel.id} /></span>
            <span class="cap muted">{fmtMin(quote.minutes)}</span>
          {:else}
            <span class="rp2">{usd(sel.pricePerHour)}/h</span>
            <span class="cap muted">{feeText(sel) ? `+ ${feeText(sel)}` : 'no transfer fee'}</span>
          {/if}
        </div>
      {:else}
        <span class="gt sm mute"><Icon name="cloud" /></span>
        <div class="col grow"><b>Choose a machine</b><span class="cap muted">Tap a row to select it.</span></div>
      {/if}
    </div>
    <div class="bud">
      {#if withJob}
        <BudgetBar extra={quote?.total ?? 0} />
      {:else}
        {@const credit = budget.caps?.credit ?? null}
        <div class="budget"><div class="bl">{#if credit !== null}<span>Credit <b>{usd(credit)}</b></span>{/if}<span>Billed by the hour, per second</span></div></div>
      {/if}
    </div>
    <button type="button" class="btn primary" aria-disabled={!can || undefined} onclick={use}><Icon name="check" />Use this GPU</button>
  </div>
</section>
