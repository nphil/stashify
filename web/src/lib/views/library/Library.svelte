<script lang="ts">
  import { onMount } from 'svelte';
  import PageHead from '../../ui/PageHead.svelte';
  import LibraryTools from './LibraryTools.svelte';
  import { model } from './model.svelte';
  import SceneGrid from './SceneGrid.svelte';

  onMount(() => model.start());
</script>

<div class="lib">
  <PageHead title="Library" quiet />
  <LibraryTools />
  <SceneGrid />
  <p class="lib-live" role="status" aria-live="polite">{model.countLine}</p>
</div>
