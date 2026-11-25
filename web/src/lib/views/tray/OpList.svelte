<script lang="ts">
  import { OP_ORDER, OPS } from '../../choices';
  import type { OpId } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import { options } from '../../stores/options.svelte';
  import { roving } from './roving';

  /* Transcode runs at home only, so leaving the cloud is part of choosing it. */
  function pick(id: OpId): void {
    options.op = id;
    if (!OPS[id].cloud && options.cloud) options.where = null;
  }
</script>

<div class="opt-list" role="radiogroup" aria-label="What to do" use:roving>
  {#each OP_ORDER as id (id)}
    {@const o = OPS[id]}
    {@const on = options.op === id}
    <button type="button" class="opt" role="radio" aria-checked={on} tabindex={on ? 0 : -1} onclick={() => pick(id)}>
      <span class="gt sm"><Icon name={o.icon} /></span>
      <span class="om"><span class="on">{o.name}</span><span class="oh">{o.hint}{o.cloud ? '' : ' · runs at home only'}</span></span>
      <span class="radio"><Icon name="check" /></span>
    </button>
  {/each}
</div>
