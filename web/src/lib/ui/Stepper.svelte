<script lang="ts">
  import Icon from './Icon.svelte';

  let { label, display, onstep, canDec = true, canInc = true }: {
    /** what is being stepped, e.g. "stop per rental" */
    label: string;
    /** the value as shown, e.g. "$3.00" */
    display: string;
    onstep: (dir: -1 | 1) => void;
    canDec?: boolean;
    canInc?: boolean;
  } = $props();
</script>

<div class="stepper" role="group" aria-label={label}>
  <button type="button" class="iconbtn" aria-label={`Lower ${label}`} aria-disabled={!canDec || undefined} onclick={() => canDec && onstep(-1)}><Icon name="minus" /></button>
  <b aria-live="polite">{display}</b>
  <button type="button" class="iconbtn" aria-label={`Raise ${label}`} aria-disabled={!canInc || undefined} onclick={() => canInc && onstep(1)}><Icon name="plus" /></button>
</div>
