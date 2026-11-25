<script lang="ts">
  import Icon from '../ui/Icon.svelte';
  import type { IconName } from '../ui/icons';
  import { nav, type View } from '../stores/nav.svelte';
  import { jobs } from '../stores/jobs.svelte';
  import { ui } from '../stores/ui.svelte';

  interface Item { view: View; icon: IconName; label: string; n: number }
  const items: Item[] = $derived([
    { view: 'library', icon: 'grid', label: 'Library', n: 0 },
    { view: 'jobs', icon: 'bolt', label: 'Jobs', n: jobs.activeCount },
    { view: 'review', icon: 'eye', label: 'Review', n: jobs.reviewCount },
  ]);
</script>

<nav class="bnav" aria-label="Sections">
  {#each items as it (it.view)}
    <button type="button" class="bn" aria-current={nav.view === it.view ? 'page' : undefined} onclick={() => nav.go(it.view)}>
      <Icon name={it.icon} /><span>{it.label}{it.n ? ` · ${it.n}` : ''}</span>
    </button>
  {/each}
  <button type="button" class="bn" data-theme-trigger aria-haspopup="dialog" aria-expanded={ui.menu === 'theme'}
    onclick={() => (ui.menu = ui.menu === 'theme' ? null : 'theme')}>
    <Icon name="swatch" /><span>Theme</span>
  </button>
</nav>
