<script lang="ts">
  import { MOCK } from '../../api/client';
  import { jobMedia } from '../../api/jobs';
  import { videoUrl } from '../../api/library';
  import type { Job, JobSegment } from '../../api/types';
  import Thumb from '../../ui/Thumb.svelte';
  import { BLOCKS } from './jobInfo';
  import Clip from './Clip.svelte';

  /* The two small pictures of a running card: the original next to the restored result. While segments
     finish it shows the newest one; otherwise the live feed next to the original. Neutral placeholders
     stay underneath until real video arrives. */
  let { job, fresh }: { job: Job; fresh: boolean } = $props();

  const newest = $derived<JobSegment | null>(job.segments?.length ? job.segments.reduce((a, b) => (b.n > a.n ? b : a)) : null);
  const armed = $derived(!!job.preview && !fresh && (job.progress > 0 || (job.frame ?? 0) > 0));
  const beforeSrc = $derived(newest ? jobMedia.segBefore(job.id, newest.n) : videoUrl(job.scene_id));
  const afterSrc = $derived(newest ? jobMedia.segAfter(job.id, newest.n) : jobMedia.live(job.id));
  /** segment pair or live feed: a change of source starts new players */
  const pairKey = $derived(newest ? `s${newest.n}` : 'live');
  const caption = $derived(newest ? `After · moment ${newest.n + 1} of ${job.segments?.length ?? 1}` : 'After · live');

  let before = $state<HTMLVideoElement>();
  let after = $state<HTMLVideoElement>();

  /* The original follows the restored clip: same play state, pulled back when it drifts. */
  function sync(): void {
    if (!before || !after) return;
    if (after.paused !== before.paused) {
      if (after.paused) before.pause();
      else before.play().catch(() => { /* autoplay refused: the poster stays, nothing to do */ });
    }
    if (before.readyState >= 1 && Math.abs(before.currentTime - after.currentTime) > (newest ? 0.2 : 1)) before.currentTime = after.currentTime;
  }
</script>

<div class={['jmedia', fresh && 'one']}>
  <div class="jv">
    <Thumb id={job.scene_id} />
    {#if MOCK}<div class="patch blocks">{#each BLOCKS as l, i (i)}<i style:--l={l}></i>{/each}</div>{/if}
    {#if armed}{#key pairKey}<Clip src={beforeSrc} bind:video={before} loop={!!newest} />{/key}{/if}
  </div>
  {#if !fresh}
    <div class="jv">
      <Thumb id={job.scene_id} />
      {#if MOCK}<div class="patch clean"></div>{/if}
      {#if armed}{#key pairKey}<Clip src={afterSrc} bind:video={after} loop={!!newest} onsync={sync} />{/key}{/if}
    </div>
  {/if}
  <div class="jc">{#if fresh}<span>Just started</span>{:else}<span>Before</span><span>{caption}</span>{/if}</div>
</div>
