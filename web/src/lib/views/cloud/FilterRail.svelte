<script lang="ts">
  /* The filters of the rental browser: a rail beside the list when there is room, a sheet over it when there is not (the shell CSS decides). */
  import { cloud, STEPS, type StepKey } from '../../stores/cloud.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Stepper from '../../ui/Stepper.svelte';
  import Switch from '../../ui/Switch.svelte';

  const STEPPERS: readonly { key: StepKey; label: string; show: (v: number) => string }[] = [
    { key: 'disk', label: 'Free disk', show: (v) => `${v} GB+` },
    { key: 'rel', label: 'Reliability', show: (v) => `${v}%+` },
    { key: 'days', label: 'Max rental', show: (v) => `${v} d+` },
    { key: 'cuda', label: 'CUDA', show: (v) => `${v.toFixed(1)}+` },
    { key: 'drv', label: 'Driver', show: (v) => `${v}+` },
  ];
  const f = $derived(cloud.filters);
</script>

<aside class="rb-rail" aria-label="Filters">
  <div class="fgrp">
    <div class="fgh">
      <span class="h">Hosts</span>
      {#if cloud.activeCount > 0}<button type="button" class="linkbtn" onclick={() => cloud.resetFilters()}>Reset · {cloud.activeCount}</button>{/if}
    </div>
    <div class="frow">
      <div class="fl"><b>Data centres only</b><span>Secure, always-on hosts</span></div>
      <Switch checked={cloud.dcOnly} label="Data centres only" onchange={(v) => { void cloud.setDcOnly(v); }} />
    </div>
    <div class="frow">
      <div class="fl"><b>Unverified hosts</b><span>Include them in the list</span></div>
      <Switch checked={f.unverified} label="Include unverified hosts" onchange={(v) => cloud.setFilter('unverified', v)} />
    </div>
  </div>
  <div class="fgrp">
    <span class="h">Machine</span>
    {#each STEPPERS as s (s.key)}
      {@const steps = STEPS[s.key]}
      <div class="frow">
        <div class="fl"><b>{s.label}</b></div>
        <Stepper label={s.label} display={f[s.key] === 0 ? 'Any' : s.show(f[s.key])} onstep={(dir) => cloud.step(s.key, dir)}
          canDec={f[s.key] > steps[0]} canInc={f[s.key] < steps[steps.length - 1]} />
      </div>
    {/each}
  </div>
  {#if cloud.facets.rented !== undefined}
    <div class="fgrp">
      <span class="h">Also show</span>
      <div class="frow">
        <div class="fl"><b>Already rented</b><span>Machines that are busy right now</span></div>
        <Switch checked={f.rented} label="Show machines that are already rented" onchange={(v) => cloud.setFilter('rented', v)} />
      </div>
    </div>
  {/if}
  <div class="rail-done"><button type="button" class="btn quiet" onclick={() => (ui.filters = false)}>Done</button></div>
</aside>
