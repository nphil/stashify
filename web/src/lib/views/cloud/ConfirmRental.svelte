<script lang="ts">
  /* The paid confirmation. It asks the server for this machine's bill again (prices move), shows what the rental can do to the
     month's budget, and rents nothing until the button is pressed. */
  import { untrack } from 'svelte';
  import { isAbort } from '../../api/client';
  import { fetchQuote } from '../../api/cloud';
  import type { QuoteReply } from '../../api/cloudAdapter';
  import { startRun } from '../../run';
  import { budget } from '../../stores/budget.svelte';
  import { cloud } from '../../stores/cloud.svelte';
  import { options } from '../../stores/options.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Dialog from '../../ui/Dialog.svelte';
  import Icon from '../../ui/Icon.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import { fmtDur, plural, usd } from '../../util/format';
  import BillRows from './BillRows.svelte';
  import { TIER_ICON, TIER_LABEL, gpuName, jobName } from './offerText';

  $effect(() => { cloud.ensure(); });

  const pick = $derived(cloud.picked);
  const offer = $derived(pick?.offer ?? null);
  const caps = $derived(budget.caps);

  /** the server's bill for this machine right now (null until it answers) */
  let fresh = $state.raw<QuoteReply | null>(null);
  let checking = $state(false);
  let tries = $state(0);
  let changed = $state(false);
  let failure = $state<string | null>(null);
  let running = $state(false);

  const quote = $derived(fresh?.quote ?? pick?.quote ?? null);
  const refusal = $derived(fresh?.refusal ?? null);
  const total = $derived(quote?.total ?? 0);
  const can = $derived(offer !== null && quote !== null && !checking && refusal === null && !running);

  /* ask again when the dialog opens, when another machine is picked, and after the price changed under the person */
  $effect(() => {
    const id = offer?.id;
    void tries;
    if (id === undefined) return;
    const ctl = new AbortController();
    untrack(() => {
      const p = cloud.picked;
      if (!p) return;
      checking = true;
      fresh = null;
      fetchQuote({ scenes: selection.ids, op: options.op, quality: options.q, size: options.size, offerId: p.offer.id, providerId: p.offer.providerId, sceneGb: selection.info.gb }, ctl.signal).then(
        (r) => {
          if (ctl.signal.aborted) return;
          fresh = r;
          checking = false;
        },
        (e: unknown) => {
          if (isAbort(e) || ctl.signal.aborted) return;
          checking = false;
          failure = e instanceof Error ? e.message : 'Could not check the price';
        },
      );
    });
    return () => ctl.abort();
  });

  async function run(): Promise<void> {
    const o = offer;
    const q = quote;
    if (!can || o === null || q === null) return;
    running = true;
    failure = null;
    changed = false;
    try {
      const res = await startRun({ offerId: o.id, providerId: o.providerId, confirmUsd: q.max, minutes: q.minutes });
      if (res.ok) return;
      if (res.code === 'price_changed') {
        changed = true;
        tries += 1;
      } else {
        failure = res.message;
      }
    } catch (e) {
      failure = e instanceof Error ? e.message : 'Could not start the run';
    } finally {
      running = false;
    }
  }
</script>

<Dialog title="Rent a GPU and run?" onclose={() => (ui.overlay = null)}>
  <div class="col confirm">
    <p class="cap muted">{jobName(options.op, options.q)} · {plural(selection.count, 'scene')}{selection.info.dur > 0 ? ` · ${fmtDur(selection.info.dur)}` : ''}</p>
    {#if offer && quote}
      <div class="mach">
        <span class="gt sm"><Icon name={TIER_ICON[offer.tier]} /></span>
        <div class="col grow">
          <b class="ell">{gpuName(offer)} · {offer.location}</b>
          <span class="cap muted ell">{TIER_LABEL[offer.tier]} · {offer.provider} · {offer.reliability}% reliable · {usd(offer.pricePerHour)}/h</span>
        </div>
      </div>
      <BillRows {offer} {quote} />
      {#if caps}
        <dl class="facts">
          <dt>Stops by itself at</dt><dd>{usd(caps.jobCap)}<small>per rental, nothing partial is kept</small></dd>
          <dt>This month</dt><dd>{usd(caps.monthToDate)} → {usd(caps.monthToDate + total)}<small>of {usd(caps.monthCap)} · {usd(Math.max(0, caps.monthCap - caps.monthToDate - total))} left after</small></dd>
          {#if caps.credit !== null}<dt>Credit</dt><dd>{usd(caps.credit)} → {usd(Math.max(0, caps.credit - total))}<small>on {offer.provider}</small></dd>{/if}
        </dl>
      {/if}
      <p class="cap muted">An estimate, not a fixed price: it could reach about {usd(quote.max)} if the machine runs slower. Your video is uploaded to a third-party machine and deleted after the job. The machine is destroyed when the run ends.</p>
      {#if checking}<p class="cap muted" role="status">Checking the price with the provider…</p>{/if}
    {:else}
      <Skeleton rows={4} />
    {/if}
    {#if refusal}<div class="banner bad" role="alert"><Icon name="alert" /><span>{refusal.message}</span></div>{/if}
    {#if changed}<div class="banner" role="status"><Icon name="alert" /><span>The price changed. Check the new total, then confirm again.</span></div>{/if}
    {#if failure}<div class="banner bad" role="alert"><Icon name="alert" /><span>{failure}</span></div>{/if}
  </div>
  {#snippet actions()}
    <!-- svelte-ignore a11y_autofocus: the safe choice takes focus when the dialog opens -->
    <button type="button" class="btn quiet" autofocus onclick={() => (ui.overlay = null)}>Cancel</button>
    <button type="button" class="btn primary" aria-disabled={!can || undefined} onclick={run}><Icon name="coin" />{running ? 'Starting…' : quote ? `Rent GPU & run · ~${usd(total)}` : 'Rent GPU & run'}</button>
  {/snippet}
</Dialog>
