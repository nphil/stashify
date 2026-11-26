<script lang="ts">
  import { jobs } from '../../stores/jobs.svelte';
  import type { Job } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import Status from '../../ui/Status.svelte';
  import { gbf } from '../../util/format';
  import { CLOUD_STEPS, costLine, dash, fmtN, gbOf, jobTitle, opIcon, runWord, stepIndex, whatLine, whereLine } from './jobInfo';
  import LogView from './LogView.svelte';
  import MediaPair from './MediaPair.svelte';
  import StatusBlock from './StatusBlock.svelte';

  /* One running, starting or settling job. The card is keyed by job id and only its numbers change,
     so a poll never rebuilds it (the open log and the playing clips stay put). */
  let { job, fresh }: { job: Job; fresh: boolean } = $props();

  let acting = $state(false);
  const title = $derived(jobTitle(job));
  const word = $derived(runWord(job, fresh));
  const live = $derived(job.state === 'running');
  const gpu = $derived(job.gpu_stats);
  const step = $derived(stepIndex(job));
  const cost = $derived(costLine(job));
  const showStats = $derived(live && !fresh);
  const canCancel = $derived(job.state === 'queued' || job.state === 'running');

  async function act(action: 'cancel' | 'pause' | 'resume'): Promise<void> {
    if (acting) return;
    acting = true;
    try { await jobs.act(job.id, action); } finally { acting = false; }
  }
</script>

<article class="job" aria-label={title}>
  <MediaPair {job} />
  <div class="jbody">
    <div class="jhead">
      <div class="grow">
        <h2 class="ttl">{title}</h2>
        <div class="jwhere">
          <span><Icon name={opIcon(job)} />{whatLine(job)}</span>
          <span><Icon name={job.cloud ? 'cloud' : 'monitor'} />{whereLine(job)}</span>
          {#if job.cloud && job.cloud_tier}<span><Icon name="building" />{job.cloud_tier === 'dc' ? 'Data centre' : 'Community'}</span>{/if}
        </div>
      </div>
      <Status state={word.state} word={word.word} />
    </div>
    {#if job.cloud}
      <ol class="phases" aria-label="Steps">
        {#each CLOUD_STEPS as s, i (s.id)}
          <li class="ph" aria-current={i === step ? 'step' : undefined}>{#if i < step}<Icon name="check" small />{:else if i === step}<i class="dot"></i>{/if}{s.label}</li>
        {/each}
      </ol>
    {/if}
    <StatusBlock {job} {title} />
    {#if job.cloud}
      <div class="jcost">
        <div class="col"><b class="num">{cost.spent}</b><span class="cap muted">so far{cost.of ? `, of about ${cost.of}` : ''}</span></div>
        {#if cost.cap}<div class="col"><b class="num">{cost.cap}</b><span class="cap muted">stops by itself</span></div>{/if}
        {#if cost.rented}<div class="col"><b class="num">{cost.rented}</b><span class="cap muted">machine running</span></div>{/if}
      </div>
    {/if}
    {#if showStats}
      <dl class="jstats">
        <div><dt>Frame</dt><dd class="num">{job.frame != null && job.total_frames ? `${fmtN(job.frame)} / ${fmtN(job.total_frames)}` : dash}</dd></div>
        <div><dt>GPU</dt><dd class="num">{gpu?.util != null ? `${Math.round(gpu.util)}%` : dash}</dd></div>
        <div><dt>Video memory</dt><dd class="num">{gpu?.mem_used != null && gpu.mem_total != null ? `${gbOf(gpu.mem_used)} / ${gbOf(gpu.mem_total)} GB` : dash}</dd></div>
        {#if !job.cloud}
          <div><dt>Temperature</dt><dd class="num">{gpu?.temp != null ? `${Math.round(gpu.temp)} °C` : dash}</dd></div>
          <div><dt>Power</dt><dd class="num">{gpu?.power != null ? `${Math.round(gpu.power)} W` : dash}</dd></div>
        {:else}
          <div><dt>Sent</dt><dd class="num">{job.sent_bytes != null ? gbf(job.sent_bytes / 1024 ** 3) : dash}</dd></div>
        {/if}
      </dl>
    {/if}
    {#if canCancel}
      <div class="jfoot">
        {#if live && !job.cloud}
          <button type="button" class="btn" aria-disabled={acting} onclick={() => act(job.paused ? 'resume' : 'pause')}>
            <Icon name={job.paused ? 'play' : 'pause'} />{job.paused ? 'Resume' : 'Pause'}
          </button>
        {/if}
        <button type="button" class="btn quiet t-danger" aria-disabled={acting} onclick={() => act('cancel')}>
          <Icon name="stop" />{job.cloud ? 'Stop & destroy machine' : 'Cancel'}
        </button>
        {#if job.cloud}<span class="cap muted">Can’t pause: the GPU is billed while it runs.</span>{/if}
      </div>
    {/if}
  </div>
  {#if live && !fresh}
    <div class="jlogw"><LogView id={job.id} active startOpen={false} /></div>
  {/if}
</article>
