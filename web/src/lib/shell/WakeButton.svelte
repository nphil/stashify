<script lang="ts">
  /* The power glyph that wakes a sleeping runner (Wake-on-LAN). `glyph` = the small round button in a header pill,
     `btn` = the labelled button on a Runners card. Wake progress lives in the runners store, so both stay in step. */
  import Icon from '../ui/Icon.svelte';
  import { runners } from '../stores/runners.svelte';
  import type { Runner } from '../api/types';

  let { runner, variant = 'glyph' }: { runner: Runner; variant?: 'glyph' | 'btn' } = $props();

  const w = $derived(runners.wakes[runner.url]);
  const left = $derived(runners.wakeLeft(runner.url));
  const waking = $derived(!!w && !w.failed);
  const failed = $derived(!!w?.failed);
  const noMac = $derived(!runner.can_wake);
  const word = $derived(waking ? (left ? `Waking… ${left} s` : 'Waking…') : failed ? 'Didn’t wake · Retry' : 'Wake up');
  const tip = $derived(noMac ? 'Add the PC’s network address (MAC) in Runners to enable waking'
    : waking ? `Waking ${runner.name}…` : failed ? (w?.error ?? `${runner.name} didn’t wake. Try again`) : `Wake ${runner.name}`);
  const aria = $derived(noMac ? `${tip}` : waking ? `Waking ${runner.name}${left ? `, about ${left} seconds` : ''}` : failed ? `${runner.name} didn’t wake. Retry` : `Wake ${runner.name}`);

  function press(e: Event): void {
    e.stopPropagation();
    if (noMac || waking) return;
    void runners.wake(runner);
  }
</script>

{#if variant === 'glyph'}
  <button type="button" class={['wk', waking && 'waking', failed && 'failed']} aria-disabled={noMac || waking || undefined} aria-label={aria} title={tip} onclick={press}>
    <Icon name={failed ? 'alert' : 'power'} small />
  </button>
{:else}
  <button type="button" class="btn" aria-disabled={noMac || waking || undefined} aria-label={aria} title={tip} onclick={press}>
    <Icon name={failed ? 'alert' : 'power'} />{word}
  </button>
{/if}
