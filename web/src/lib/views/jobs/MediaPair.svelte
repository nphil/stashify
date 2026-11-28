<script lang="ts">
  import { jobMedia } from '../../api/jobs';
  import type { Job } from '../../api/types';

  let { job }: { job: Job } = $props();
  const newest = $derived(job.segments?.reduce<number | undefined>(
    (last, seg) => last === undefined || seg.n > last ? seg.n : last, undefined));
  type Media = { key: string; frame: string } | { key: string; moment: number };
  let shown = $state.raw<Media>();
  let failedFrame = $state<string>();
  let failedMoment = $state<number>();

  const still = $derived(job.preview_id && job.preview_id !== failedFrame
    ? { key: `frame:${job.preview_id}`, frame: job.preview_id } : undefined);
  const motion = $derived(newest !== undefined && newest !== failedMoment
    ? { key: `moment:${newest}`, moment: newest } : undefined);
  const next = $derived(job.stage === 'upscale' ? still ?? motion : motion ?? still);
  // Keep the last matched pair visible until both sides of its replacement are ready.
  const versions = $derived([shown, next?.key !== shown?.key ? next : undefined]
    .filter((entry): entry is Media => entry !== undefined));

  function loaded(media: Media, event: Event) {
    if (media.key !== next?.key) return;
    const parent = (event.currentTarget as Element).parentElement;
    const before = parent?.firstElementChild;
    const after = before?.nextElementSibling;
    const ready = (element: Element | null | undefined) =>
      (element instanceof HTMLImageElement && element.complete && element.naturalWidth > 0)
      || (element instanceof HTMLVideoElement && element.readyState >= 2);
    if (ready(before) && ready(after)) shown = media;
  }

  function failed(media: Media) {
    if ('frame' in media) failedFrame = media.frame;
    else failedMoment = media.moment;
    if (shown?.key === media.key) shown = undefined;
  }

  function sync(event: Event) {
    const after = event.currentTarget as HTMLVideoElement;
    const before = after.previousElementSibling;
    if (!(before instanceof HTMLVideoElement) || before.readyState < 1) return;
    if (Math.abs(before.currentTime - after.currentTime) > 0.2) before.currentTime = after.currentTime;
    if (after.paused) before.pause();
    else if (before.paused) before.play().catch(() => {});
  }
</script>

{#each versions as version (version.key)}
  <div class="jmedia" hidden={shown?.key !== version.key}>
    {#if 'frame' in version}
      <img class="jv" src={jobMedia.before(job.id, version.frame)} alt="Original frame" onload={(event) => loaded(version, event)} onerror={() => failed(version)} />
      <img class="jv" src={jobMedia.after(job.id, version.frame)} alt="Processed frame" onload={(event) => loaded(version, event)} onerror={() => failed(version)} />
    {:else}
      <video class="jv" src={jobMedia.segBefore(job.id, version.moment)} aria-label="Original moment" muted playsinline loop preload="auto" onloadeddata={(event) => loaded(version, event)} onerror={() => failed(version)}></video>
      <video class="jv" src={jobMedia.segAfter(job.id, version.moment)} aria-label="Processed moment" muted playsinline autoplay loop preload="auto" onloadeddata={(event) => loaded(version, event)} onerror={() => failed(version)} ontimeupdate={sync} onplay={sync} onpause={sync}></video>
    {/if}
    <div class="jc"><span>Before</span><span>{'frame' in version ? 'After · processed frame' : `After · moment ${version.moment + 1}`}</span></div>
  </div>
{/each}
