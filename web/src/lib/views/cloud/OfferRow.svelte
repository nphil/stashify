<script lang="ts">
  /* One machine in the list. A button laid under the whole row picks it; the cost tip and the details button sit above that
     button as siblings, never inside it, so nothing interactive is nested. */
  import type { Offer } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import { fmtMin, usd } from '../../util/format';
  import CostTip from './CostTip.svelte';
  import RelBars from './RelBars.svelte';
  import { TIER_ICON, TIER_LABEL, city, feeText, fmtN, gpuName, vramTotal, whyNot } from './offerText';

  let { offer, job, on, stop, index, size, open, measure, onpick, onfocus, ontoggle }: {
    offer: Offer;
    /** priced for a job: the "This job" column is shown */
    job: boolean;
    on: boolean;
    /** the row Tab lands on */
    stop: boolean;
    index: number;
    size: number;
    open: boolean;
    measure: (node: HTMLElement) => { destroy: () => void };
    onpick: (o: Offer) => void;
    onfocus: (id: string) => void;
    ontoggle: (id: string) => void;
  } = $props();

  const bad = $derived(whyNot(offer));
  const fee = $derived(feeText(offer));
  const est = $derived(offer.estimate);
  const name = $derived(`${gpuName(offer)}, ${offer.provider}, ${city(offer)}, ${usd(offer.pricePerHour)} per hour${job && est && !bad ? `, about ${usd(est.total)}` : ''}${bad ? `, ${bad}` : ''}`);
  const groups = $derived(open ? detailGroups(offer, fee) : []);

  function detailGroups(o: Offer, fee: string | null): { title: string; items: [string, string][] }[] {
    const has = (n: number, text: string): string => (n > 0 ? text : '');
    const group = (title: string, all: [string, string][]) => ({ title, items: all.filter(([, v]) => v !== '') });
    const bw = o.memGbs ?? 0;
    return [
      group('GPU', [['Model', gpuName(o)], ['Performance', has(o.perf, fmtN(o.perf))], ['Compute', has(o.tflops, `${o.tflops.toFixed(1)} TFLOPS`)], ['Video memory', `${vramTotal(o)} GB`], ['Bandwidth', has(bw, `${fmtN(bw)} GB/s`)], ['Max CUDA', has(o.cuda, o.cuda.toFixed(1))], ['Driver', has(o.driver, String(o.driver))]]),
      group('Machine', [['CPU', o.cpu], ['Cores', has(o.cores, String(o.cores))], ['Memory', has(o.ramGb, `${o.ramGb} GB`)], ['Board', o.board], ['PCIe', o.pcie]]),
      group('Disk and network', [['Disk', o.disk], ['Disk speed', has(o.diskMbs, `${fmtN(o.diskMbs)} MB/s`)], ['Free space', has(o.diskGb, `${fmtN(o.diskGb)} GB`)], ['Download', has(o.netDown, `${fmtN(o.netDown)} Mbps`)], ['Upload', has(o.netUp, `${fmtN(o.netUp)} Mbps`)], ['Open ports', has(o.ports, String(o.ports))]]),
      group('Terms', [['Location', o.location], ['Host type', TIER_LABEL[o.tier]], ['Verified', o.verified ? 'Yes' : 'No'], ['Reliability', `${o.reliability}%`], ['Max rental', has(o.days, `${o.days} days`)], ['Provider', o.provider], ['Price', `${usd(o.pricePerHour)}/h · ${fee ?? 'no transfer fee'}`]]),
    ];
  }
</script>

<div class="mrow" data-id={offer.id} data-on={on || undefined} data-off={bad !== null || undefined} data-x={open || undefined} use:measure>
  <button type="button" class="mpick" role="radio" aria-checked={on} aria-disabled={bad !== null || undefined} aria-label={name}
    aria-posinset={index + 1} aria-setsize={size} tabindex={stop ? 0 : -1}
    onfocus={() => onfocus(offer.id)} onclick={() => { if (bad === null) onpick(offer); }}></button>
  <span class="radio" aria-hidden="true"><Icon name="check" /></span>

  <div class="cl c-gpu">
    <span class="gn">{gpuName(offer)}</span>
    <span class="l2">Perf {fmtN(offer.perf)} · {offer.tflops.toFixed(1)} TFLOPS</span>
    <span class="l3"><span class="ic"><Icon name="server" small />{offer.provider}</span><span class="sep">·</span><span class="ic"><Icon name={TIER_ICON[offer.tier]} small />{TIER_LABEL[offer.tier]}</span></span>
  </div>
  <div class="cl c-mem">
    <span class="l1">{vramTotal(offer)} GB VRAM</span>
    {#if offer.memGbs}<span class="l2">{fmtN(offer.memGbs)} GB/s</span>{/if}
    {#if offer.cuda > 0}<span class="l3">CUDA {offer.cuda.toFixed(1)}</span>{/if}
  </div>
  <div class="cl c-host">
    <span class="l1">{offer.cpu}{offer.cores > 0 ? ` · ${offer.cores}c` : ''}</span>
    <span class="l2">{offer.ramGb} GB RAM</span>
    <span class="l3">{fmtN(offer.diskGb)} GB free{offer.diskMbs > 0 ? ` · ${(offer.diskMbs / 1000).toFixed(1)} GB/s` : ''}</span>
  </div>
  <div class="cl c-net">
    <span class="l1"><span class="ic"><Icon name="pin" small />{offer.location}</span></span>
    <span class="l2">↓ {fmtN(offer.netDown)} ↑ {fmtN(offer.netUp)} Mbps</span>
    <span class="l3">
      <span class="ic"><RelBars rel={offer.reliability} />{offer.reliability}%</span><span class="sep">·</span>
      {#if offer.verified}<span class="ic vf"><Icon name="shieldCheck" small />Verified</span>{:else}<span class="ic uv"><Icon name="shield" small />Unverified</span>{/if}
      {#if offer.days > 0}<span class="sep">·</span>{offer.days} d max{/if}
    </span>
  </div>
  <div class="cl c-price">
    <span class="l1">{usd(offer.pricePerHour)}/h</span>
    <span class="l2">{fee ? `+ ${fee}` : 'No transfer fee'}</span>
    {#if !job && bad}<span class="l3 why"><Icon name="alert" small />{bad}</span>{:else if offer.pricePerHour > 0}<span class="l3">Value {fmtN(offer.perf / offer.pricePerHour)}</span>{/if}
  </div>
  {#if job}
    <div class="c-est">
      {#if bad}
        <span class="why"><Icon name="alert" small /><span>{bad}</span></span>
      {:else if est}
        <span class="tot">{usd(est.total)}<CostTip id={offer.id} /></span><span class="tm">{fmtMin(est.minutes)}</span><span class="sm">with transfer</span>
      {/if}
    </div>
  {/if}
  <button type="button" class="iconbtn sm c-x" aria-label="Details" aria-expanded={open} tabindex={stop ? 0 : -1} onclick={() => ontoggle(offer.id)}><Icon name={open ? 'chevU' : 'chevD'} small /></button>

  <div class="c-sum">
    <span><b>{vramTotal(offer)} GB</b> · {Math.round(offer.tflops)} TFLOPS</span>
    <span>{city(offer)}</span>
    <span class="s-price">{usd(offer.pricePerHour)}/h{fee ? ` + ${fee}` : ''}</span>
    <span class="ic"><RelBars rel={offer.reliability} />{offer.reliability}%</span>
    {#if !offer.verified}<span class="uv ic"><Icon name="shield" small />Unverified</span>{/if}
    <button type="button" class="linkbtn" aria-expanded={open} tabindex={stop ? 0 : -1} onclick={() => ontoggle(offer.id)}>Details<Icon name={open ? 'chevU' : 'chevD'} small /></button>
  </div>

  {#if open}
    <div class="c-det">
      {#each groups as g (g.title)}
        <div><h4 class="h">{g.title}</h4><dl>{#each g.items as [k, v] (k)}<div><dt>{k}</dt><dd>{v}</dd></div>{/each}</dl></div>
      {/each}
    </div>
  {/if}
</div>
