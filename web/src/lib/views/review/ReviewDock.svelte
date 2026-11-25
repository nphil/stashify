<script lang="ts">
  import { ui } from '../../stores/ui.svelte';
  import Icon from '../../ui/Icon.svelte';
  import { trayHeight } from '../../ui/actions';
  import { jobTitle, reviewLine } from '../jobs/jobInfo';
  import { review } from './review.svelte';

  const job = $derived(review.current);
</script>

{#if job}
  <section class="tray dock" aria-label="Decision" use:trayHeight>
    <div class="trow">
      <span class="gt sm"><Icon name="film" /></span>
      <div class="col grow"><b class="ell">{jobTitle(job)}</b><span class="cap muted ell">{reviewLine(job)}</span></div>
      <button type="button" class="btn quiet" aria-disabled={review.busy} onclick={() => review.decide('discard')}><Icon name="x" />Discard</button>
      <button type="button" class="btn quiet t-danger" aria-disabled={review.busy} onclick={() => (ui.overlay = 'replace')}><Icon name="swap" />Replace original</button>
    </div>
  </section>
{/if}
