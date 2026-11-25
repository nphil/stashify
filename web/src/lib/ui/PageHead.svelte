<script lang="ts">
  import type { Snippet } from 'svelte';

  /* `quiet`: the heading stays in the page for screen readers and focus, but is not drawn (the library starts with its search bar). */
  let { title, sub, right, quiet = false }: { title: string; sub?: string; right?: Snippet; quiet?: boolean } = $props();
</script>

{#if quiet}
  <h1 class="sr" tabindex="-1">{title}</h1>
{:else}
  <div class="pagehd">
    <div class="col">
      <h1 class="ttl" tabindex="-1">{title}</h1>
      {#if sub}<span class="cap muted">{sub}</span>{/if}
    </div>
    {#if right}{@render right()}{/if}
  </div>
{/if}
