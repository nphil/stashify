<script lang="ts">
  import { jobs } from '../../stores/jobs.svelte';
  import { nav } from '../../stores/nav.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Dialog from '../../ui/Dialog.svelte';
  import Icon from '../../ui/Icon.svelte';
  import PageHead from '../../ui/PageHead.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import { jobTitle, reviewFacts } from '../jobs/jobInfo';
  import CompareStage from './CompareStage.svelte';
  import { review } from './review.svelte';

  const job = $derived(review.current);
  const title = $derived(job ? jobTitle(job) : '');
  const facts = $derived(job ? reviewFacts(job) : []);
  const sub = $derived(review.waiting.length ? `${review.waiting.length} waiting · drag the handle to compare` : 'Nothing waiting');

  /* The "Replaced." banner belongs to this visit only. */
  $effect(() => () => { review.decided = null; });
</script>

<PageHead title="Review" {sub} />

{#if !jobs.loaded}
  <div class="a-rev" role="status" aria-label="Loading">
    <div class="col" style="gap:var(--lu-space-3);min-width:0"><span class="skel rsk-stage"></span><Skeleton small width="60%" /></div>
    <div class="col" style="gap:var(--lu-space-3)"><Skeleton rows={4} /></div>
  </div>
{:else if jobs.error && jobs.all.length === 0}
  <div class="empty">
    <span class="gt bad"><Icon name="alert" /></span>
    <p><b>Could not load jobs.</b> {jobs.error}</p>
    <button type="button" class="btn" onclick={() => jobs.refresh()}><Icon name="refresh" />Retry</button>
  </div>
{:else}
  {#if review.decided}
    <div class={['banner', review.decided.kind === 'replaced' ? 'pos' : 'inf', 'rdecided']} role="status">
      <Icon name={review.decided.kind === 'replaced' ? 'checkCircle' : 'info'} />
      <span>{#if review.decided.kind === 'replaced'}<b>Replaced.</b> {review.decided.title} now uses the cleaned file.{:else}<b>Discarded.</b> The original was not changed.{/if}</span>
    </div>
  {/if}

  {#if job}
    {#if review.waiting.length > 1}
      <div class="row wrap rsw" role="group" aria-label="Previews waiting">
        {#each review.waiting as w (w.id)}
          <button type="button" class="chip" aria-pressed={w.id === job.id} data-on={w.id === job.id ? '' : undefined} onclick={() => review.pick(w.id)}>{jobTitle(w)}</button>
        {/each}
      </div>
    {/if}
    <div class="a-rev">
      <div class="col" style="gap:var(--lu-space-3);min-width:0">
        {#key job.id}<CompareStage {job} />{/key}
      </div>
      <aside class="col" style="gap:var(--lu-space-4)">
        <h2 class="h">Details</h2>
        <dl class="facts">
          {#each facts as f (f.k)}
            <dt>{f.k}</dt><dd>{f.v}{#if f.sub}<small>{f.sub}</small>{/if}</dd>
          {/each}
        </dl>
      </aside>
    </div>
  {:else}
    <div class="empty">
      <span class="gt inf"><Icon name="eye" /></span>
      <p>Nothing is waiting for your decision. Finished runs show up here to compare before you replace anything.</p>
      <button type="button" class="btn" onclick={() => nav.go('library')}><Icon name="grid" />Back to the library</button>
    </div>
  {/if}
{/if}

{#if ui.overlay === 'replace' && job}
  <Dialog title="Replace the original?" onclose={() => (ui.overlay = null)}>
    <p>Stashify will overwrite <b>{title}</b> with the new file, in place.</p>
    <ul class="plain">
      <li><Icon name="alert" small />There is no backup of the original.</li>
      <li><Icon name="checkCircle" small />Tags, rating, markers and play history stay.</li>
    </ul>
    {#snippet actions()}
      <!-- svelte-ignore a11y_autofocus: the safe choice takes focus when the dialog opens -->
      <button type="button" class="btn quiet" autofocus onclick={() => (ui.overlay = null)}>Cancel</button>
      <button type="button" class="btn t-danger" aria-disabled={review.busy} onclick={() => review.decide('replace')}><Icon name="swap" />Replace original</button>
    {/snippet}
  </Dialog>
{/if}
