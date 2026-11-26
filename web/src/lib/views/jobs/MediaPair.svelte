<script lang="ts">
  import { jobMedia } from '../../api/jobs';
  import type { Job } from '../../api/types';

  let { job }: { job: Job } = $props();
  const newest = $derived(job.segments?.reduce<number | undefined>(
    (last, seg) => last === undefined || seg.n > last ? seg.n : last, undefined));
  const pair = $derived(job.preview && job.preview_id ? job.preview_id : null);
  const key = $derived(pair ? `frame:${pair}` : newest !== undefined ? `moment:${newest}` : null);
  let beforeReady = $state<string | null>(null);
  let afterReady = $state<string | null>(null);
  let before = $state<HTMLVideoElement>();
  let after = $state<HTMLVideoElement>();

  function sync() {
    if (!after || !before || before.readyState < 1) return;
    if (Math.abs(before.currentTime - after.currentTime) > 0.2) before.currentTime = after.currentTime;
    if (after.paused) before.pause();
    else if (before.paused) before.play().catch(() => {});
  }
</script>

{#if key}
  {#each [key] as version (version)}
    <div class="jmedia" hidden={beforeReady !== key || afterReady !== key}>
      {#if pair}
        <img class="jv" src={jobMedia.before(job.id, pair)} alt="Original frame" onload={() => beforeReady = version} onerror={() => beforeReady = null} />
        <img class="jv" src={jobMedia.after(job.id, pair)} alt="Processed frame" onload={() => afterReady = version} onerror={() => afterReady = null} />
      {:else if newest !== undefined}
        <video class="jv" bind:this={before} src={jobMedia.segBefore(job.id, newest)} aria-label="Original moment" muted playsinline loop preload="auto" onloadeddata={() => beforeReady = version} onerror={() => beforeReady = null}></video>
        <video class="jv" bind:this={after} src={jobMedia.segAfter(job.id, newest)} aria-label="Processed moment" muted playsinline autoplay loop preload="auto" onloadeddata={() => afterReady = version} onerror={() => afterReady = null} ontimeupdate={sync} onplay={sync} onpause={sync}></video>
      {/if}
      <div class="jc"><span>Before</span><span>{pair ? 'After · processed frame' : `After · moment ${(newest ?? 0) + 1}`}</span></div>
    </div>
  {/each}
{/if}
