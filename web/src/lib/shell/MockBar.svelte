<script lang="ts">
  import { MOCK_STATES, applyMockState, mockState } from '../mockState.svelte';
  import { theme } from '../stores/theme.svelte';
  import { DEFAULT_DARK, DEFAULT_LIGHT } from '../themes/themes.generated';

  const providers = new URLSearchParams(location.search).get('prov') === '2' ? 2 : 1;
  function flipProviders(): void {
    const u = new URL(location.href);
    if (providers === 2) u.searchParams.delete('prov'); else u.searchParams.set('prov', '2');
    location.href = u.href;
  }
</script>

<!-- Mock mode only. Not part of the product UI; hidden by ?shot=1. -->
<div class="mock" role="toolbar" aria-label="Mock screens">
  <b>Stashify · mock</b>
  {#each MOCK_STATES as [id, label] (id)}
    <button type="button" aria-pressed={mockState.current === id} onclick={() => applyMockState(id)}>{label}</button>
  {/each}
  <span class="sep"></span>
  <button type="button" onclick={() => theme.choose(theme.active.scheme === 'dark' ? DEFAULT_LIGHT : DEFAULT_DARK)}>{theme.active.scheme === 'dark' ? 'Dark' : 'Light'}</button>
  <button type="button" onclick={flipProviders}>{providers} provider{providers > 1 ? 's' : ''}</button>
</div>
