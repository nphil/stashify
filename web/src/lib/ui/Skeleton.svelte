<script lang="ts">
  /* Placeholder shape while data loads. Never a spinner. */
  let { rows = 1, small = false, width }: { rows?: number; small?: boolean; width?: string } = $props();
</script>

{#each { length: rows } as _, i (i)}
  <span class={['skel', small && 's']} style:width aria-hidden="true"></span>
{/each}
