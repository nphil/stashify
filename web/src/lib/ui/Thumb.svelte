<script lang="ts">
  import { thumbUrl } from '../api/library';
  import { MOCK } from '../api/client';

  /* A scene picture. A neutral gradient shows first (and is all there is in mock mode); the real
     image fades in over it, loaded lazily and decoded off the main thread. */
  let { id, tn, eager = false }: { id: string; tn?: number; eager?: boolean } = $props();

  function pick(s: string): number {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return (h % 6) + 1;
  }
  const n = $derived(tn ?? pick(id));
  let loaded = $state(false);
</script>

<span class={['thumb', 'tn', `tn-${n}`]}>
  {#if !MOCK}
    <img src={thumbUrl(id)} alt="" loading={eager ? 'eager' : 'lazy'} decoding="async" class:in={loaded} onload={() => (loaded = true)} onerror={() => (loaded = false)} />
  {/if}
</span>
