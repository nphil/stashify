<script lang="ts">
  /* A muted video that stays invisible until its first frame arrives, so a missing file leaves the
     placeholder underneath instead of a broken player. */
  let { src, video = $bindable(), loop = false, autoplay = true, onsync, onmeta }: {
    src: string;
    video?: HTMLVideoElement;
    loop?: boolean;
    autoplay?: boolean;
    /** fires on play, pause, seek and time updates so a partner clip can follow */
    onsync?: () => void;
    /** fires once the length is known */
    onmeta?: () => void;
  } = $props();

  let ok = $state(false);
</script>

<video bind:this={video} {src} class={{ in: ok }} muted playsinline {autoplay} {loop} preload="auto" aria-hidden="true" tabindex="-1"
  onloadeddata={() => (ok = true)} onloadedmetadata={onmeta} onplay={onsync} onpause={onsync} onseeked={onsync} ontimeupdate={onsync}></video>
