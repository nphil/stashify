<script lang="ts">
  import { focusLayer } from '../../ui/actions';
  import Icon from '../../ui/Icon.svelte';
  import Seg from '../../ui/Seg.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Choice from './Choice.svelte';
  import { model, type Dir, type SortId } from './model.svelte';
  import TagPicker from './TagPicker.svelte';

  const SORT = 'lib:sort';
  const SIZE = 'lib:size';

  interface SortDef { value: SortId; label: string; chip: Record<Dir, string>; dir: Record<Dir, string> }
  const SORTS: readonly SortDef[] = [
    { value: 'date', label: 'Date', chip: { desc: 'Newest', asc: 'Oldest' }, dir: { desc: 'Newest first', asc: 'Oldest first' } },
    { value: 'created_at', label: 'Date added', chip: { desc: 'Recently added', asc: 'First added' }, dir: { desc: 'Newest first', asc: 'Oldest first' } },
    { value: 'title', label: 'Title', chip: { desc: 'Title Z–A', asc: 'Title A–Z' }, dir: { desc: 'Z to A', asc: 'A to Z' } },
    { value: 'file_mod_time', label: 'File modified', chip: { desc: 'Recently modified', asc: 'Oldest modified' }, dir: { desc: 'Newest first', asc: 'Oldest first' } },
    { value: 'random', label: 'Random', chip: { desc: 'Random', asc: 'Random' }, dir: { desc: '', asc: '' } },
  ];
  const SIZES = [
    { value: 0, label: 'Any size', chip: 'Any size' },
    { value: 720, label: '720p and up', chip: '720p+' },
    { value: 1080, label: '1080p and up', chip: '1080p+' },
    { value: 2160, label: '4K only', chip: '4K' },
  ] as const;

  let root: HTMLDivElement;

  const sort = $derived(SORTS.find((s) => s.value === model.prefs.sort) ?? SORTS[0]);
  const size = $derived(SIZES.find((s) => s.value === model.prefs.minRes) ?? SIZES[0]);
  const dirOptions = $derived([
    { value: 'desc' as const, label: sort.dir.desc },
    { value: 'asc' as const, label: sort.dir.asc },
  ]);
  const items = $derived(model.visible);
  const allOn = $derived(items.length > 0 && items.every((s) => selection.has(s.id)));
  const selectLabel = $derived(`Select all ${items.length.toLocaleString()}${model.hasMore ? ' loaded' : ''}`);

  const toggle = (id: string): void => { ui.menu = ui.menu === id ? null : id; };

  function pickSort(v: SortId): void {
    model.setSort(v);
    if (v === 'random') ui.menu = null;
  }
  function pickDir(d: Dir): void {
    model.setDir(d);
    ui.menu = null;
  }
  function pickSize(v: number): void {
    model.setMinRes(v);
    ui.menu = null;
  }
  /* a press outside the tools row closes the open menu (Escape is handled by ui.back) */
  function away(e: PointerEvent): void {
    if (ui.menu?.startsWith('lib:') && !(e.target instanceof Node && root.contains(e.target))) ui.menu = null;
  }
  function onSearchKey(e: KeyboardEvent): void {
    if (e.key === 'Enter') model.flushQ();
  }
</script>

<svelte:window onpointerdown={away} />

<div class="lib-tools" bind:this={root}>
  <label class="field grow">
    <Icon name="search" />
    <input type="search" placeholder={model.total ? `Search ${model.total.toLocaleString()} scenes` : 'Search scenes'} aria-label="Search scenes" autocomplete="off" value={model.q}
      oninput={(e) => model.setQ(e.currentTarget.value)} onkeydown={onSearchKey} />
  </label>

  <div class="lib-menuwrap">
    <button type="button" class="chip" aria-haspopup="menu" aria-expanded={ui.menu === SORT} onclick={() => toggle(SORT)}>
      <Icon name="sort" />{sort.chip[model.prefs.dir]}<Icon name="chevD" small />
    </button>
    {#if ui.menu === SORT}
      <div class="lib-menu pop enter" role="group" aria-label="Sort scenes" use:focusLayer>
        <Choice options={SORTS} value={model.prefs.sort} label="Sort by" onpick={pickSort} />
        {#if model.prefs.sort !== 'random'}
          <div class="dir"><Seg options={dirOptions} value={model.prefs.dir} label="Direction" onchange={pickDir} /></div>
        {/if}
      </div>
    {/if}
  </div>

  <div class="lib-menuwrap">
    <button type="button" class="chip" data-on={model.prefs.minRes ? '' : undefined} aria-haspopup="menu" aria-expanded={ui.menu === SIZE} onclick={() => toggle(SIZE)}>
      <Icon name="film" />{size.chip}<Icon name="chevD" small />
    </button>
    {#if ui.menu === SIZE}
      <div class="lib-menu pop enter" role="group" aria-label="Minimum size" use:focusLayer>
        <Choice options={SIZES} value={model.prefs.minRes} label="Minimum size" onpick={pickSize} />
      </div>
    {/if}
  </div>

  <TagPicker />

  <button type="button" class="chip" data-on={model.prefs.hideDone ? '' : undefined} aria-pressed={model.prefs.hideDone} onclick={() => model.toggleHideDone()}>
    <Icon name="checkCircle" />Not cleaned yet
  </button>

  <div class="sel">
    {#if selection.count > 0}<span class="cap muted">{selection.count.toLocaleString()} selected</span>{/if}
    {#if items.length > 0}
      <button type="button" class="linkbtn" onclick={() => (allOn ? selection.clear() : selection.add(items))}>{allOn ? 'Clear' : selectLabel}</button>
    {/if}
    {#if selection.count > 0 && !allOn}
      <button type="button" class="linkbtn" onclick={() => selection.clear()}>Clear</button>
    {/if}
  </div>
</div>
