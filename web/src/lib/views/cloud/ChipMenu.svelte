<script module lang="ts">
  export interface MenuItem { value: string; label: string; hint?: string; checked: boolean; disabled?: boolean; group?: string }
</script>

<script lang="ts">
  /* A filter chip that opens a small menu of choices. Which menu is open lives in `ui.menu`, so Escape, the scrim and the mock states agree. */
  import { ui } from '../../stores/ui.svelte';
  import { focusLayer, lightDismiss } from '../../ui/actions';
  import Icon from '../../ui/Icon.svelte';
  import type { IconName } from '../../ui/icons';

  let { id, icon, label, items, on = false, end = false, onpick }: {
    id: string;
    icon: IconName;
    label: string;
    items: readonly MenuItem[];
    /** a choice other than the default is active */
    on?: boolean;
    /** open towards the left edge (menus at the right end of the bar) */
    end?: boolean;
    /** called with the chosen value; a menu whose only live choice is already active needs none */
    onpick?: (value: string) => void;
  } = $props();

  const open = $derived(ui.menu === id);
  let wrap: HTMLDivElement;

  function pick(it: MenuItem): void {
    if (it.disabled) return;
    onpick?.(it.value);
    ui.menu = null;
  }

  /* arrows move through the choices; Tab closes the menu and carries on */
  function keys(e: KeyboardEvent & { currentTarget: HTMLElement }): void {
    if (e.key === 'Tab') { ui.menu = null; return; }
    const all = [...e.currentTarget.querySelectorAll<HTMLElement>('.opt:not([aria-disabled="true"])')];
    if (all.length === 0) return;
    const i = all.findIndex((el) => el === document.activeElement);
    let n = -1;
    if (e.key === 'ArrowDown') n = (i + 1) % all.length;
    else if (e.key === 'ArrowUp') n = i < 0 ? all.length - 1 : (i - 1 + all.length) % all.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = all.length - 1;
    if (n < 0) return;
    e.preventDefault();
    all[n].focus();
  }
</script>

<div class="menuwrap" bind:this={wrap}>
  <button type="button" class="chip" data-on={on || undefined} aria-haspopup="menu" aria-expanded={open} onclick={() => (ui.menu = open ? null : id)}>
    <Icon name={icon} small />{label}<Icon name="chevD" small />
  </button>
  {#if open}
    <div class={['menu', 'pop', 'enter', end && 'r']} role="menu" aria-label={label} tabindex="-1" use:focusLayer={{ trap: false }} use:lightDismiss={() => (ui.menu = null)} onkeydown={keys}>
      {#each items as it (it.value)}
        {#if it.group}<div class="hd" role="presentation">{it.group}</div>{/if}
        <button type="button" class="opt" role="menuitemradio" aria-checked={it.checked} aria-disabled={it.disabled || undefined} tabindex="-1" onclick={() => pick(it)}>
          <span class="om"><span class="on">{it.label}</span>{#if it.hint}<span class="oh">{it.hint}</span>{/if}</span>
          {#if it.checked}<Icon name="check" />{/if}
        </button>
      {/each}
    </div>
  {/if}
</div>
