<script module lang="ts">
  export type StatusState = 'ok' | 'busy' | 'asleep' | 'offline' | 'warn';
</script>

<script lang="ts">
  import Icon from './Icon.svelte';

  const MAP: Record<StatusState, { tone: string; glyph: 'dot' | 'moon' | 'power' | 'alert' }> = {
    ok: { tone: 'ok', glyph: 'dot' },
    busy: { tone: 'run', glyph: 'dot' },
    asleep: { tone: 'off', glyph: 'moon' },
    offline: { tone: 'bad', glyph: 'power' },
    warn: { tone: 'warn', glyph: 'alert' },
  };

  let { state, word }: { state: StatusState; word: string } = $props();
  const m = $derived(MAP[state]);
</script>

<!-- A glyph plus a word, never colour alone. -->
<span class={['st', m.tone]}>{#if m.glyph === 'dot'}<i class="dot"></i>{:else}<Icon name={m.glyph} />{/if}{word}</span>
