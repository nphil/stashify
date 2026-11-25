<script lang="ts" generics="T extends string | number">
  import Icon from '../../ui/Icon.svelte';

  interface Opt { value: T; label: string; count?: number }
  let { options, value, label, onpick }: {
    options: readonly Opt[];
    /** the option that is on now (none checked when it matches nothing) */
    value: T;
    label: string;
    onpick: (v: T) => void;
  } = $props();

  let root: HTMLDivElement;

  /* a one-choice menu: arrows move between rows, Enter/Space picks; one row is the tab stop */
  const stop = $derived(options.some((o) => o.value === value) ? value : options[0]?.value);

  function key(e: KeyboardEvent): void {
    const rows = [...root.querySelectorAll<HTMLElement>('[role="menuitemradio"]')];
    const i = rows.findIndex((r) => r === document.activeElement);
    let n = -1;
    if (e.key === 'ArrowDown') n = Math.min(rows.length - 1, i + 1);
    else if (e.key === 'ArrowUp') n = Math.max(0, i - 1);
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = rows.length - 1;
    if (n < 0) return;
    e.preventDefault();
    rows[n]?.focus();
  }
</script>

<div class="opt-list" role="menu" tabindex="-1" aria-label={label} bind:this={root} onkeydown={key}>
  {#each options as o (o.value)}
    <button type="button" class="opt" role="menuitemradio" aria-checked={o.value === value} tabindex={o.value === stop ? 0 : -1} onclick={() => onpick(o.value)}>
      <span class="nm">{o.label}</span>
      {#if o.count !== undefined}<span class="n">{o.count.toLocaleString()}</span>{/if}
      {#if o.value === value}<Icon name="check" small />{/if}
    </button>
  {/each}
</div>
