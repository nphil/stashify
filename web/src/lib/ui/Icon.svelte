<script lang="ts">
  import { ICONS, type IconName } from './icons';

  let { name, small = false }: { name: IconName; small?: boolean } = $props();
</script>

<svg class={['ico', small && 's']} viewBox="0 0 24 24" aria-hidden="true" focusable="false">{@html ICONS[name]}</svg>
