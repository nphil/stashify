<script lang="ts">
  /* This month's spend, what this run adds, and the owner's limits: the budget line under the picker and the browser. */
  import { budget } from '../../stores/budget.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import { usd } from '../../util/format';

  let { extra = 0 }: { extra?: number } = $props();
  const caps = $derived(budget.caps);
  const spentPct = $derived(caps && caps.monthCap > 0 ? Math.min(100, (caps.monthToDate / caps.monthCap) * 100) : 0);
  const runPct = $derived(caps && caps.monthCap > 0 ? Math.max(0, Math.min(100 - spentPct, (extra / caps.monthCap) * 100)) : 0);
</script>

{#if caps}
  <div class="budget">
    <div class="bar thick" role="img" aria-label={`${usd(caps.monthToDate)} spent this month, ${usd(extra)} for this run, ${usd(Math.max(0, caps.monthCap - caps.monthToDate - extra))} left of ${usd(caps.monthCap)}`}>
      <i class="spent" style:width="{spentPct}%"></i>{#if extra > 0}<i class="run" style:width="{runPct}%"></i>{/if}
    </div>
    <div class="bl">
      <span>This month <b>{usd(caps.monthToDate)}</b> of {usd(caps.monthCap)}</span>
      {#if extra > 0}<span>This run <b>+{usd(extra)}</b></span>{/if}
      {#if caps.credit !== null}<span>Credit <b>{usd(caps.credit)}</b></span>{/if}
      <span>Stop per rental <b>{usd(caps.jobCap)}</b></span>
    </div>
  </div>
{:else}
  <Skeleton rows={2} small />
{/if}
