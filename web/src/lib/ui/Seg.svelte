<script lang="ts" generics="T extends string | number">
  import type { Snippet } from 'svelte';

  interface Opt { value: T; label: string; disabled?: boolean }
  let { options, value = $bindable(), label, full = false, onchange, item }: {
    options: readonly Opt[];
    value: T;
    /** what the group chooses, read by screen readers */
    label: string;
    full?: boolean;
    onchange?: (v: T) => void;
    /** optional custom content of one button */
    item?: Snippet<[Opt]>;
  } = $props();

  let root: HTMLDivElement;

  function pick(v: T): void {
    if (v === value) return;
    value = v;
    onchange?.(v);
  }
  /* Radio-group keys: arrows move and select, Home/End jump. Only the checked button is a tab stop. */
  function key(e: KeyboardEvent): void {
    const enabled = options.filter((o) => !o.disabled);
    const i = enabled.findIndex((o) => o.value === value);
    let n = -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') n = (i + 1) % enabled.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') n = (i - 1 + enabled.length) % enabled.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = enabled.length - 1;
    if (n < 0) return;
    e.preventDefault();
    pick(enabled[n].value);
    queueMicrotask(() => root.querySelector<HTMLElement>('[aria-checked="true"]')?.focus());
  }
</script>

<!-- svelte-ignore a11y_interactive_supports_focus: focus lives on the roving radio buttons inside -->
<div class={['seg', full && 'full']} role="radiogroup" aria-label={label} bind:this={root} onkeydown={key}>
  {#each options as o (o.value)}
    <button type="button" role="radio" aria-checked={o.value === value} aria-disabled={o.disabled || undefined}
      tabindex={o.value === value ? 0 : -1} onclick={() => !o.disabled && pick(o.value)}>
      {#if item}{@render item(o)}{:else}{o.label}{/if}
    </button>
  {/each}
</div>
