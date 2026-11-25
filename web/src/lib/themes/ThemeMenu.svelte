<script lang="ts">
  import Icon from '../ui/Icon.svelte';
  import Switch from '../ui/Switch.svelte';
  import { focusLayer, lightDismiss } from '../ui/actions';
  import { theme, themeById } from '../stores/theme.svelte';
  import { ui } from '../stores/ui.svelte';
  import { THEMES, type ThemeMeta } from './themes.generated';

  /* Header button + popover (bottom sheet on phones): a searchable gallery of palettes, grouped Dark / Light,
     each tile a live swatch painted with that palette's own colours. A radio group per scheme.
     Open state lives in `ui.menu` so the bottom-bar item on phones can open the same sheet. */
  const open = $derived(ui.menu === 'theme');
  let q = $state('');
  let pop = $state<HTMLElement>();
  const uid = $props.id();

  const hit = (t: ThemeMeta): boolean => {
    const s = q.trim().toLowerCase();
    return !s || `${t.name} ${t.family}`.toLowerCase().includes(s);
  };
  const dark = $derived(THEMES.filter((t) => t.scheme === 'dark' && hit(t)));
  const light = $derived(THEMES.filter((t) => t.scheme === 'light' && hit(t)));
  const isOn = (t: ThemeMeta): boolean =>
    theme.auto ? (t.scheme === 'dark' ? theme.pref.dark : theme.pref.light) === t.id : theme.active.id === t.id;
  /** the one tile of a group that can be reached with Tab */
  const stop = (list: ThemeMeta[], t: ThemeMeta): boolean => (list.some(isOn) ? isOn(t) : t === list[0]);

  /** the trigger that is on screen: the header button, or the bottom-bar item on phones */
  const visibleTrigger = (): HTMLElement | undefined =>
    [...document.querySelectorAll<HTMLElement>('[data-theme-trigger]')].find((el) => el.offsetParent !== null);
  function close(refocus = true): void {
    ui.menu = null;
    q = '';
    if (refocus) visibleTrigger()?.focus({ preventScroll: true });
  }
  function popKey(e: KeyboardEvent): void {
    if (e.key === 'Escape') { e.stopPropagation(); if (q) q = ''; else close(); }
  }

  /* Arrow keys move through a group (and into the next one) and pick as they go, like any radio group. */
  function gridKey(e: KeyboardEvent, list: ThemeMeta[]): void {
    const keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
    if (!keys.includes(e.key)) return;
    const grid = e.currentTarget as HTMLElement;
    const cols = getComputedStyle(grid).gridTemplateColumns.split(' ').length || 1;
    const cur = list.findIndex((t) => t.id === (document.activeElement as HTMLElement | null)?.dataset.id);
    let n = cur;
    if (e.key === 'ArrowLeft') n = cur - 1;
    else if (e.key === 'ArrowRight') n = cur + 1;
    else if (e.key === 'ArrowUp') n = cur - cols;
    else if (e.key === 'ArrowDown') n = cur + cols;
    else if (e.key === 'Home') n = 0;
    else n = list.length - 1;
    if (n < 0 || n >= list.length) {
      /* edge of the grid: stay put on a row move, hop to the other group on a column move */
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); }
      return;
    }
    e.preventDefault();
    theme.choose(list[n].id);
    const id = list[n].id;
    queueMicrotask(() => grid.querySelector<HTMLElement>(`[data-id="${id}"]`)?.focus({ preventScroll: false }));
  }

  const darkName = $derived(themeById.get(theme.pref.dark)?.name ?? '');
  const lightName = $derived(themeById.get(theme.pref.light)?.name ?? '');
</script>

<div class="thm">
  <button type="button" class="iconbtn" data-theme-trigger aria-label="Theme" aria-haspopup="dialog" aria-expanded={open}
    aria-controls={`${uid}-pop`} onclick={() => (open ? close() : ((q = ''), (ui.menu = 'theme')))}><Icon name="swatch" /></button>

  {#if open}
    <button type="button" class="thm-scrim" tabindex="-1" aria-hidden="true" onclick={() => close()}></button>
    <div class="thm-pop pop enter" id={`${uid}-pop`} role="dialog" tabindex="-1" aria-label="Theme" bind:this={pop} onkeydown={popKey} use:focusLayer={{ initial: 'input' }} use:lightDismiss={() => close(false)}>
      <label class="field">
        <Icon name="search" />
        <input type="search" placeholder="Search themes" aria-label="Search themes" autocomplete="off" bind:value={q} />
      </label>
      <div class="thm-auto">
        <div class="col">
          <b class="lbl">Follow system</b>
          <span class="cap muted">{theme.auto ? `${darkName} at night, ${lightName} by day` : 'Pick one theme, or let the device decide'}</span>
        </div>
        <Switch checked={theme.pref.mode === 'auto' && !theme.override} label="Follow system" onchange={(v) => theme.setAuto(v)} />
      </div>
      <div class="thm-scroll" aria-live="polite">
        {#each [['Dark', dark], ['Light', light]] as const as [name, list] (name)}
          {#if list.length}
            <section class="thm-group" aria-label={`${name} themes`}>
              <h3 class="h">{name}{#if theme.auto}<span class="cap muted"> · used when the device is {name.toLowerCase()}</span>{/if}</h3>
              <!-- svelte-ignore a11y_interactive_supports_focus -->
              <div class="thm-grid" role="radiogroup" aria-label={`${name} themes`} onkeydown={(e) => gridKey(e, list)}>
                {#each list as t (t.id)}
                  <button type="button" class="thm-tile" role="radio" aria-checked={isOn(t)} tabindex={stop(list, t) ? 0 : -1}
                    data-id={t.id} aria-label={t.name} onclick={() => theme.choose(t.id)}>
                    <span class="thm-prev" data-theme={t.id} aria-hidden="true">
                      <i class="pv-card"></i><i class="pv-ink"></i><i class="pv-ink2"></i><i class="pv-acc"></i>
                      <span class="pv-dots"><i style="background:var(--pal-positive)"></i><i style="background:var(--pal-warning)"></i><i style="background:var(--pal-danger)"></i><i style="background:var(--pal-info)"></i></span>
                    </span>
                    <span class="thm-name">{t.name}</span>
                    {#if isOn(t)}<span class="thm-chk" aria-hidden="true"><Icon name="check" small /></span>{/if}
                  </button>
                {/each}
              </div>
            </section>
          {/if}
        {/each}
        {#if !dark.length && !light.length}
          <p class="muted thm-empty">No theme matches “{q}”.</p>
        {/if}
      </div>
    </div>
  {/if}
</div>
