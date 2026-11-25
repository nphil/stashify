<script lang="ts">
  import { focusLayer, lightDismiss } from '../../ui/actions';
  import Icon from '../../ui/Icon.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Choice from './Choice.svelte';
  import { model } from './model.svelte';

  const POPULAR = 18;
  const SEARCH = 40;
  const ID = 'lib:tag';

  let tq = $state('');
  let menu: HTMLDivElement | undefined = $state();

  const open = $derived(ui.menu === ID);
  const searching = $derived(tq.trim() !== '');
  const shown = $derived.by(() => {
    const q = tq.trim().toLowerCase();
    return q ? model.tags.filter((t) => t.name.toLowerCase().includes(q)).slice(0, SEARCH) : model.tags.slice(0, POPULAR);
  });
  /* the chosen tag stays in the list even when it is not among the busiest */
  const options = $derived.by(() => {
    const cur = model.prefs.tag;
    const rows = shown.map((t) => ({ value: t.id, label: t.name, count: t.scene_count }));
    if (cur && !rows.some((r) => r.value === cur.id)) rows.unshift({ value: cur.id, label: cur.name, count: model.tags.find((t) => t.id === cur.id)?.scene_count ?? 0 });
    return [{ value: '', label: 'All tags', count: undefined }, ...rows];
  });

  function toggle(): void {
    tq = '';
    ui.menu = open ? null : ID;
  }
  function pick(id: string): void {
    const t = id ? model.tags.find((x) => x.id === id) ?? model.prefs.tag : null;
    model.setTag(t ? { id: t.id, name: t.name } : null);
    ui.menu = null;
  }
  function key(e: KeyboardEvent): void {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      menu?.querySelector<HTMLElement>('[role="menuitemradio"]')?.focus();
    } else if (e.key === 'Enter' && searching && shown[0]) {
      e.preventDefault();
      pick(shown[0].id);
    }
  }
</script>

<div class="lib-menuwrap">
  <button type="button" class="chip" data-on={model.prefs.tag ? '' : undefined} aria-haspopup="menu" aria-expanded={open} onclick={toggle}>
    <Icon name="tag" />{model.prefs.tag?.name ?? 'All tags'}<Icon name="chevD" small />
  </button>
  {#if open}
    <div class="lib-menu pop enter" role="group" aria-label="Filter by tag" bind:this={menu} use:focusLayer={{ initial: 'input' }} use:lightDismiss={() => (ui.menu = null)}>
      <label class="field"><Icon name="search" /><input type="search" placeholder="Search tags" aria-label="Search tags" autocomplete="off" bind:value={tq} onkeydown={key} /></label>
      {#if !searching}<div class="hd">Most used</div>{/if}
      {#if searching && !shown.length}
        <div class="none">No matching tags</div>
      {:else}
        <Choice {options} value={model.prefs.tag?.id ?? ''} label="Tags" onpick={pick} />
      {/if}
    </div>
  {/if}
</div>
