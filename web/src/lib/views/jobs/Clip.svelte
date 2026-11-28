<script lang="ts">
  /* Keep the available poster visible until the browser can display a video frame. */
  let { src, label, video = $bindable(), loop = false, onsync, onerror }: {
    src: string;
    label: string;
    video?: HTMLVideoElement;
    loop?: boolean;
    onsync: (video: HTMLVideoElement, event: string) => void;
    onerror: (video: HTMLVideoElement) => void;
  } = $props();

  let ok = $state(false);

  function sync(e: Event & { currentTarget: HTMLVideoElement }): void {
    if (e.currentTarget.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) ok = true;
    onsync(e.currentTarget, e.type);
  }
</script>

<video bind:this={video} {src} class={{ in: ok }} muted playsinline {loop} preload="auto" aria-label={label} tabindex="-1"
  onloadedmetadata={sync} ondurationchange={sync} onloadeddata={sync} oncanplay={sync} onplaying={sync}
  onpause={sync} onended={sync} onseeked={sync} ontimeupdate={sync}
  onerror={() => { ok = false; if (video) onerror(video); }}></video>
