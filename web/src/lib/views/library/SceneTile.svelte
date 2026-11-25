<script lang="ts">
  import type { Scene } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import Thumb from '../../ui/Thumb.svelte';
  import { fmtDur, gbf, resName } from '../../util/format';
  import { displayTitle, isDone } from './scene';

  /* One button per scene: pressed = selected. It holds no other control. */
  let { scene, index, selected, tabbable, onpick }: {
    scene: Scene;
    /** position in the shown list (the grid uses it for arrow keys and ranges) */
    index: number;
    selected: boolean;
    /** the one tile that Tab lands on */
    tabbable: boolean;
    onpick: (scene: Scene, index: number, shift: boolean) => void;
  } = $props();

  const file = $derived(scene.files[0]);
  const title = $derived(displayTitle(scene));
  const done = $derived(isDone(scene));
  const res = $derived(file?.height ? resName(file.height) : '');
  const dur = $derived(file?.duration ? fmtDur(file.duration) : '');
  const sub = $derived([scene.studio?.name, file?.size ? gbf(file.size / 1e9) : ''].filter(Boolean).join(' · '));
</script>

<!-- Title and facts come first in the markup, so the button's name (taken from what is visible) reads "Title, studio · size, 1080p, 41:22"; the picture goes on top with CSS `order`. -->
<button type="button" class="tile" aria-pressed={selected} tabindex={tabbable ? 0 : -1} data-i={index}
  onclick={(e) => onpick(scene, index, e.shiftKey)}>
  <span class="tt">{title}</span>
  <span class="ts">{sub || '\u00a0'}</span>
  <span class="frame"><span class="pic">
    <Thumb id={scene.id} tn={scene.tn} />
    <span class="chk"><Icon name="check" /></span>
    {#if res}<span class="pp tr pill-pic">{res}</span>{/if}
    {#if dur}<span class="pp br pill-pic">{dur}</span>{/if}
    {#if done}<span class="pp bl pill-pic ok"><Icon name="checkCircle" />Cleaned</span>{/if}
  </span></span>
</button>
