<script lang="ts">
  /* Four bars that fill with reliability: a glyph, so the number is never alone. */
  let { rel }: { rel: number } = $props();
  const lit = $derived(rel >= 99 ? 4 : rel >= 97.5 ? 3 : rel >= 95.5 ? 2 : 1);
  const tone = $derived(rel >= 99 ? undefined : rel >= 97 ? 'mid' : 'low');
</script>

<span class={['rel', tone]} aria-hidden="true">{#each [1, 2, 3, 4] as n (n)}<i class={n <= lit ? 'on' : undefined}></i>{/each}</span>
