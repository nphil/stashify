<script lang="ts">
  import { jobs } from '../../stores/jobs.svelte';
  import { nav } from '../../stores/nav.svelte';
  import Icon from '../../ui/Icon.svelte';
  import PageHead from '../../ui/PageHead.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import { fmtMin } from '../../util/format';
  import { FINISHED_WORD, isFresh, jobTitle, whatLine, whereLine } from './jobInfo';
  import JobCard from './JobCard.svelte';
  import LogView from './LogView.svelte';

  /* The cards update in place: each is keyed by job id, and a poll only changes the numbers inside. */
  let now = $state(Date.now());
  $effect(() => {
    const t = setInterval(() => (now = Date.now()), 5000);
    return () => clearInterval(t);
  });

  /** running jobs, plus a queued one that was just started (it gets the "new run" card) */
  const cards = $derived(jobs.active.filter((j) => j.state !== 'queued' || isFresh(j, now)));
  /** the queue reads in the order it will run: oldest first */
  const queue = $derived(jobs.waiting.filter((j) => !isFresh(j, now)).reverse());
  const sub = $derived.by(() => {
    const run = jobs.running.length + jobs.waiting.filter((j) => isFresh(j, now)).length;
    const wait = queue.length;
    if (!run && !wait) return 'Nothing running';
    return [run ? `${run} running` : null, wait ? `${wait} waiting` : null].filter(Boolean).join(' · ');
  });
  const none = $derived(jobs.visible.length === 0);
</script>

<PageHead title="Jobs" {sub}>
  {#snippet right()}
    {#if jobs.finished.length}
      <button type="button" class="btn quiet" onclick={() => jobs.clearFinished()}><Icon name="trash" />Clear finished</button>
    {/if}
  {/snippet}
</PageHead>

{#if !jobs.loaded}
  <div class="jobs" role="status" aria-label="Loading jobs">
    {#each [0, 1] as i (i)}
      <div class="job jsk" aria-hidden="true">
        <span class="skel jsk-media"></span>
        <div class="jbody"><Skeleton small width="55%" /><Skeleton small width="35%" /><Skeleton small width="100%" /><Skeleton rows={2} /></div>
      </div>
    {/each}
  </div>
{:else if jobs.error && jobs.all.length === 0}
  <div class="empty">
    <span class="gt bad"><Icon name="alert" /></span>
    <p><b>Could not load jobs.</b> {jobs.error}</p>
    <button type="button" class="btn" onclick={() => jobs.refresh()}><Icon name="refresh" />Retry</button>
  </div>
{:else if none}
  <div class="empty">
    <span class="gt inf"><Icon name="bolt" /></span>
    <p>No jobs yet. Pick scenes in the Library.</p>
    <button type="button" class="btn" onclick={() => nav.go('library')}><Icon name="grid" />Open the library</button>
  </div>
{:else}
  <div class="jobs">
    {#if jobs.error}
      <div class="banner bad" role="alert"><Icon name="alert" /><span class="grow">Showing the last jobs we got. {jobs.error}</span><button type="button" class="btn quiet" onclick={() => jobs.refresh()}>Retry</button></div>
    {/if}

    {#each cards as job (job.id)}
      <JobCard {job} fresh={isFresh(job, now)} />
    {/each}

    {#if queue.length}
      <section aria-labelledby="jq-h">
        <h2 class="h jsec" id="jq-h">Up next</h2>
        <ul class="qlist">
          {#each queue as job (job.id)}
            <li class="qrow">
              <span class="gt sm mute"><Icon name="clock" /></span>
              <div class="col grow"><b class="ell">{jobTitle(job)}</b><span class="cap muted ell">{whatLine(job)} · {whereLine(job)}</span></div>
              {#if job.eta != null}<span class="cap muted nowrap num">about {fmtMin(job.eta / 60)}</span>{/if}
              <button type="button" class="iconbtn sm" aria-label={`Remove ${jobTitle(job)} from the queue`} onclick={() => jobs.act(job.id, 'cancel')}><Icon name="x" small /></button>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    {#if jobs.finished.length}
      <section aria-labelledby="jf-h">
        <h2 class="h jsec" id="jf-h">Finished</h2>
        <ul class="qlist">
          {#each jobs.finished as job (job.id)}
            {@const w = FINISHED_WORD[job.state]}
            <li class="frow">
              <div class="qrow">
                <span class={['gt sm', w?.tone === 'ok' && 'pos', w?.tone === 'bad' && 'bad', w?.tone === 'off' && 'mute']}><Icon name={w?.icon ?? 'check'} /></span>
                <div class="col grow">
                  <b class="ell">{jobTitle(job)}</b>
                  <span class="cap muted ell">{w?.word ?? job.state}{job.state === 'error' ? ` · ${job.error || job.message || 'Failed'}` : w?.sub ? ` · ${w.sub}` : ''}</span>
                </div>
                <button type="button" class="iconbtn sm" aria-label={`Dismiss ${jobTitle(job)}`} onclick={() => jobs.dismiss(job.id)}><Icon name="x" small /></button>
              </div>
              {#if job.state === 'error'}<div class="jlogw"><LogView id={job.id} active={false} startOpen /></div>{/if}
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  </div>
{/if}
