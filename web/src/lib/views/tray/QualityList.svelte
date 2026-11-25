<script lang="ts">
  import { QUALITIES, QUALITY_ORDER, SIZES, SIZE_ORDER } from '../../choices';
  import type { DenoiseId, DetectId, RtxId, SizeId } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import Seg from '../../ui/Seg.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import Switch from '../../ui/Switch.svelte';
  import { options } from '../../stores/options.svelte';
  import { selection } from '../../stores/selection.svelte';
  import { ui } from '../../stores/ui.svelte';
  import { fmtMin, usd } from '../../util/format';
  import { cloudScaled, homeFor, plan } from './plan.svelte';
  import { roving } from './roving';

  const SIZE_OPTS: { value: SizeId; label: string }[] = SIZE_ORDER.map((value) => ({ value, label: SIZES[value] }));
  const DENOISE: { value: DenoiseId; label: string }[] = [{ value: 'off', label: 'Off' }, { value: 'low', label: 'Low' }, { value: 'medium', label: 'Medium' }, { value: 'high', label: 'High' }];
  const RTX: { value: RtxId; label: string }[] = [{ value: 'off', label: 'Off' }, { value: '2', label: '2×' }, { value: '4', label: '4×' }];
  const DETECT: { value: DetectId; label: string }[] = [{ value: 'fast', label: 'Fast' }, { value: 'accurate', label: 'Accurate' }];

  const rows = $derived(
    QUALITY_ORDER.map((id) => {
      const home = options.cloud ? null : homeFor(selection.info, options.op, id, plan.runner);
      return {
        id,
        def: QUALITIES[id],
        home,
        cloud: options.cloud && plan.quote ? cloudScaled(plan.quote, options.op, options.q, id) : null,
        checked: options.q === id,
        disabled: !!home && !home.ok,
      };
    }),
  );
  /* one Tab stop: the chosen row, or the first one that can be chosen */
  const stop = $derived((rows.find((r) => r.checked && !r.disabled) ?? rows.find((r) => !r.disabled))?.id);
  const hasKnobs = $derived(plan.knobs.jasna || plan.knobs.detect);
</script>

{#if options.op === 'transcode'}
  <div class="knob padded">
    <div class="kl"><b>Output size</b><span>Smaller files play and sync faster.</span></div>
    <Seg options={SIZE_OPTS} bind:value={options.size} label="Output size" />
  </div>
{:else if options.op === 'upscale'}
  <p class="cap muted padded">Upscale has a single setting.</p>
{:else}
  <div class="opt-list" role="radiogroup" aria-label="Quality" use:roving>
    {#each rows as r (r.id)}
      <button type="button" class="opt" role="radio" aria-checked={r.checked} aria-disabled={r.disabled || undefined} tabindex={r.id === stop ? 0 : -1}
        onclick={() => { if (!r.disabled) options.q = r.id; }}>
        <span class="om"><span class="on">{r.def.name}</span><span class="oh">{r.def.hint}</span></span>
        {#if r.disabled && r.home}
          <span class="oe"><b class="warn">{r.home.why}</b></span>
        {:else if r.home}
          <span class="oe"><b>{fmtMin(r.home.min)}</b><span>Free</span></span>
        {:else if r.cloud}
          <span class="oe"><b>~{usd(r.cloud.usd)}</b><span>{fmtMin(r.cloud.min)}</span></span>
        {:else if plan.waiting}
          <span class="oe"><Skeleton small width="5ch" /></span>
        {/if}
        <span class="radio"><Icon name="check" /></span>
      </button>
    {/each}
  </div>
{/if}

{#if hasKnobs}
  <button type="button" class="adv" aria-expanded={ui.advanced} aria-controls="tray-knobs" onclick={() => (ui.advanced = !ui.advanced)}>
    <Icon name="sliders" />Advanced{#if plan.knobCount}<span class="count quiet">{plan.knobCount}</span>{/if}<Icon name="chevD" />
  </button>
  {#if ui.advanced}
    <div class="knobs" id="tray-knobs">
      {#if plan.knobs.detect}
        <div class="knob">
          <div class="kl"><b>Detection model</b><span>Accurate finds more, but is slower.</span></div>
          <Seg options={DETECT} bind:value={options.detect} label="Detection model" />
        </div>
      {/if}
      {#if plan.knobs.jasna}
        <div class="knob">
          <div class="kl"><b>Denoise</b><span>Smooths grain in the result.</span></div>
          <Seg options={DENOISE} bind:value={options.denoise} label="Denoise" />
        </div>
        <div class="knob">
          <div class="kl"><b>RTX super-resolution</b><span>Sharper, bigger picture. Needs an RTX card.</span></div>
          <Seg options={RTX} bind:value={options.rtx} label="RTX super-resolution" />
        </div>
        <div class="knob">
          <div class="kl"><b>RTX denoise and deblur</b><span>{options.rtx === 'off' ? 'Turn on RTX super-resolution first.' : 'Cleans the picture before it is scaled.'}</span></div>
          <Switch bind:checked={options.rtxFx} disabled={options.rtx === 'off'} label="RTX denoise and deblur" />
        </div>
        <div class="knob">
          <div class="kl"><b>Live preview</b><span>Watch the result while it runs.</span></div>
          <Switch bind:checked={options.preview} label="Live preview" />
        </div>
      {/if}
    </div>
  {/if}
{/if}
