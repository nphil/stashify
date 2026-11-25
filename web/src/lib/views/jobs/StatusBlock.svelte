<script lang="ts">
  import type { Job } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import { fmtDur } from '../../util/format';
  import { agoWord, dash, idleSeconds, isIndeterminate, leftWord, pct, phaseTitle, STALL_S, stepDots, stepLine } from './jobInfo';

  /* What a job is doing, in one place that changes in place: the phase in plain words, where it is in its
     steps, one bar, the time left, and how long since it last gave a sign of life. Every row keeps its
     height, numbers are tabular, and only the bar's transform moves, so nothing jumps while it updates. */
  let { job, title }: { job: Job; title: string } = $props();

  let now = $state(Date.now());
  $effect(() => {
    const t = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(t);
  });

  const percent = $derived(pct(job));
  const indet = $derived(isIndeterminate(job));
  const dots = $derived(stepDots(job));
  const steps = $derived(stepLine(job));
  const idle = $derived(idleSeconds(job, now));
  const stalled = $derived(idle !== null && idle >= STALL_S);
  const eta = $derived(job.eta != null && !job.paused && job.state === 'running' ? job.eta : null);
  /* the replace phase has no speed or time left: show only how long it has been going */
  const timed = $derived(job.state !== 'replacing');
  const speed = $derived(job.fps != null && !job.paused ? `${Math.round(job.fps * 10) / 10} fps` : dash);
</script>

<div class={['jstat', stalled && 'is-stalled']}>
  <div class="jst-head">
    <h3 class="jst-phase">{job.paused ? 'Paused' : job.state === 'queued' ? 'Waiting its turn' : phaseTitle(job)}</h3>
    <b class="jst-pct num" title={job.estimate ? 'An estimate: it firms up as the job runs.' : undefined}>{indet && percent === 0 ? dash : `${job.estimate ? '\u2248\u2009' : ''}${percent}%`}</b>
  </div>
  <div class="jst-sub cap muted">
    {#if dots}
      <ol class="jdots" aria-label={dots.label}>
        {#each { length: dots.n } as _, i (i)}
          <li class={[i + 1 < dots.at && 'done', i + 1 === dots.at && 'now']} aria-current={i + 1 === dots.at ? 'step' : undefined}></li>
        {/each}
      </ol>
    {/if}
    <span class="ell">{steps || '\u00a0'}</span>
  </div>
  <div class={['bar thick pbar', indet && 'indeterminate', (job.paused || stalled) && 'is-paused']} role="progressbar" aria-valuemin="0" aria-valuemax="100"
    aria-valuenow={indet ? undefined : percent} aria-valuetext={job.estimate ? `${percent}%, an estimate` : undefined} aria-label={`${title} progress`}><i style:--p={percent / 100}></i></div>
  <dl class="jmeta">
    {#if timed}
      <div><dt>Time left{#if eta != null && job.estimate}{' (estimate)'}{/if}</dt><dd class="num">{eta != null ? leftWord(eta) : dash}</dd></div>
      <div><dt>Speed</dt><dd class="num">{speed}</dd></div>
    {/if}
    <div><dt>Running</dt><dd class="num">{job.elapsed != null ? fmtDur(job.elapsed) : dash}</dd></div>
  </dl>
  <p class={['jlive cap', stalled ? 'warn' : 'muted']} role={stalled ? 'alert' : undefined}>
    {#if stalled && idle !== null}
      <Icon name="alert" small /><span>No update for {agoWord(idle)}. It may be stuck: check the PC, or cancel and try again.</span>
    {:else if idle !== null}
      <span class="num">Last update {idle < 3 ? 'just now' : `${agoWord(idle)} ago`}</span>
    {:else}
      <span>&nbsp;</span>
    {/if}
  </p>
</div>
