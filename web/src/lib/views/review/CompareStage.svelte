<script lang="ts">
  import { MOCK } from '../../api/client';
  import { jobMedia } from '../../api/jobs';
  import { videoUrl } from '../../api/library';
  import type { Job } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import Seg from '../../ui/Seg.svelte';
  import { fmtDur, gbf, resName } from '../../util/format';
  import { BLOCKS } from '../jobs/jobInfo';
  import Clip from '../jobs/Clip.svelte';

  /* Before and after, one over the other, with a handle to wipe between them. The handle is a slider:
     drag it, or focus it and use the arrow keys. Videos play over neutral placeholders, so a missing
     file still leaves something to look at. */
  type Mode = 'full' | 'moments' | 'sample';
  type Ab = 'split' | 'before' | 'after';
  let { job }: { job: Job } = $props();

  const MODE_LABEL: Record<Mode, string> = { full: 'Full video', moments: 'Restored moments', sample: 'Sample reel' };
  const segs = $derived(job.segments ?? []);
  const modes = $derived.by<Mode[]>(() => {
    const m: Mode[] = [];
    if (job.review_scene_id || !(segs.length || job.sample)) m.push('full');
    if (segs.length) m.push('moments');
    if (job.sample) m.push('sample');
    return m;
  });
  let pickedMode = $state<Mode | null>(null);
  const mode = $derived<Mode>(pickedMode && modes.includes(pickedMode) ? pickedMode : modes[0] ?? 'full');
  let moment = $state(0);
  const seg = $derived(segs[Math.min(moment, Math.max(0, segs.length - 1))]);
  const single = $derived(mode === 'sample');

  const beforeSrc = $derived(mode === 'moments' && seg ? jobMedia.segBefore(job.id, seg.n) : videoUrl(job.scene_id));
  const afterSrc = $derived(mode === 'moments' && seg ? jobMedia.segAfter(job.id, seg.n) : mode === 'sample' ? jobMedia.sample(job.id) : videoUrl(job.review_scene_id ?? job.scene_id));
  const clipKey = $derived(`${mode}-${seg?.n ?? 0}`);

  /* ---- wipe ---- */
  let x = $state(50);
  let ab = $state<Ab>('split');
  let rect: DOMRect | null = null;
  let dragging = false;
  let pending = 50;
  let frame = 0;

  function setX(v: number): void {
    x = Math.min(100, Math.max(0, v));
    ab = x <= 0 ? 'after' : x >= 100 ? 'before' : 'split';
  }
  function fromPointer(e: PointerEvent): number {
    return rect && rect.width > 0 ? ((e.clientX - rect.left) / rect.width) * 100 : x;
  }
  function down(e: PointerEvent): void {
    if (!(e.currentTarget instanceof HTMLElement)) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    rect = e.currentTarget.getBoundingClientRect();
    dragging = true;
    setX(fromPointer(e));
  }
  function move(e: PointerEvent): void {
    if (!dragging) return;
    pending = fromPointer(e);
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; setX(pending); });
  }
  function up(): void { dragging = false; }
  function key(e: KeyboardEvent): void {
    const step = e.shiftKey ? 10 : 2;
    const next = e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? x - step
      : e.key === 'ArrowRight' || e.key === 'ArrowUp' ? x + step
      : e.key === 'PageDown' ? x - 10
      : e.key === 'PageUp' ? x + 10
      : e.key === 'Home' ? 0
      : e.key === 'End' ? 100
      : null;
    if (next === null) return;
    e.preventDefault();
    setX(next);
  }
  $effect(() => () => cancelAnimationFrame(frame));

  /* ---- playback: the restored clip leads, the original follows ---- */
  let before = $state<HTMLVideoElement>();
  let after = $state<HTMLVideoElement>();
  let playing = $state(false);
  /* the mockup's review screen has the playhead at 12:48 */
  let time = $state(MOCK ? 768 : 0);
  let loadedDur = $state(0);

  function sync(): void {
    if (!after) return;
    playing = !after.paused;
    time = after.currentTime;
    if (!before) return;
    if (after.paused !== before.paused) {
      if (after.paused) before.pause();
      else before.play().catch(() => { /* the original did not load: the placeholder stays */ });
    }
    if (before.readyState >= 1 && Math.abs(before.currentTime - after.currentTime) > 0.3) before.currentTime = after.currentTime;
  }
  function toggle(): void {
    if (!after) return;
    if (after.paused) {
      after.play().catch(() => { /* not loaded yet (or mock mode): nothing to play */ });
      before?.play().catch(() => { /* same */ });
    } else {
      after.pause();
      before?.pause();
    }
  }
  function seek(t: number): void {
    time = t;
    if (after) after.currentTime = t;
    if (before) before.currentTime = t;
  }
  function meta(): void {
    if (mode === 'full' && after && Number.isFinite(after.duration)) loadedDur = after.duration;
  }

  const maxEnd = $derived(segs.reduce((m, s) => Math.max(m, s.end), 0));
  const dur = $derived(mode === 'full' ? loadedDur || job.result?.duration || maxEnd : 0);
  const r = $derived(job.result);
  const beforeInfo = $derived([r?.from_height != null ? resName(r.from_height) : null, r?.from_bytes != null ? gbf(r.from_bytes / 1024 ** 3) : null].filter(Boolean).join(' · '));
  const afterInfo = $derived([r?.to_height != null ? resName(r.to_height) : null, r?.to_bytes != null ? gbf(r.to_bytes / 1024 ** 3) : null].filter(Boolean).join(' · '));
  const AB_OPTIONS = [{ value: 'split', label: 'Split' }, { value: 'before', label: 'Before' }, { value: 'after', label: 'After' }] as const;
  const MODE_OPTIONS = $derived(modes.map((m) => ({ value: m, label: MODE_LABEL[m] })));
</script>

<div class="row wrap cmp-bar">
  {#if modes.length > 1}
    <Seg options={MODE_OPTIONS} value={mode} label="What to compare" onchange={(v) => { pickedMode = v; }} />
  {/if}
  {#if !single}
    <Seg options={AB_OPTIONS} bind:value={ab} label="Show" onchange={(v) => setX(v === 'split' ? 50 : v === 'before' ? 100 : 0)} />
  {/if}
</div>

<div class={['cmp', single && 'solo']} style:--x={single ? '0%' : `${x}%`}>
  {#if !single}
    <div class="lay">
      <div class="scene big"></div>
      {#if MOCK}<div class="patch blocks">{#each BLOCKS as l, i (i)}<i style:--l={l}></i>{/each}</div>{/if}
      {#key clipKey}<Clip src={beforeSrc} bind:video={before} autoplay={false} loop={mode !== 'full'} />{/key}
    </div>
  {/if}
  <div class="lay after">
    <div class="scene big"></div>
    {#if MOCK}<div class="patch clean"></div>{/if}
    {#key clipKey}<Clip src={afterSrc} bind:video={after} autoplay={false} loop={mode !== 'full'} onsync={sync} onmeta={meta} />{/key}
  </div>
  {#if !single}
    <div class="grip"><span><Icon name="split" /></span></div>
    <div class="wipe" role="slider" tabindex="0" aria-label="Compare before and after" aria-orientation="horizontal"
      aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(x)} aria-valuetext={`${Math.round(x)}% before, ${100 - Math.round(x)}% after`}
      onpointerdown={down} onpointermove={move} onpointerup={up} onpointercancel={up} onkeydown={key}></div>
  {/if}
</div>
<div class="cmp-lbl">
  {#if single}<span><b>After</b> · restored moments only</span>{:else}<span><b>Before</b>{beforeInfo ? ` · ${beforeInfo}` : ''}</span><span><b>After</b>{afterInfo ? ` · ${afterInfo}` : ''}</span>{/if}
</div>

{#if mode === 'moments' && segs.length}
  <div class="row wrap" role="group" aria-label="Restored moments">
    {#each segs as s, i (s.n)}
      <button type="button" class="chip" aria-pressed={i === moment} data-on={i === moment ? '' : undefined} aria-label={`Moment ${i + 1}, ${fmtDur(s.start)} to ${fmtDur(s.end)}`} onclick={() => (moment = i)}>{fmtDur(s.start)}</button>
    {/each}
  </div>
{/if}

<div class="row" style="gap:var(--lu-space-3)">
  <button type="button" class="iconbtn" aria-label={playing ? 'Pause both' : 'Play both'} onclick={toggle}><Icon name={playing ? 'pause' : 'play'} /></button>
  {#if mode === 'full' && dur > 0}
    <div class="tline grow">
      <div class="tk" aria-hidden="true">
        {#each segs as s (s.n)}<i style:left={`${(s.start / dur) * 100}%`} style:width={`${Math.max(0.6, ((s.end - s.start) / dur) * 100)}%`}></i>{/each}
        <em style:left={`${Math.min(100, (time / dur) * 100)}%`}></em>
      </div>
      <input class="seek" type="range" min="0" max={Math.round(dur)} step="1" value={Math.round(time)} aria-label="Position" aria-valuetext={`${fmtDur(time)} of ${fmtDur(dur)}`}
        oninput={(e) => seek(e.currentTarget.valueAsNumber)} />
    </div>
    <span class="cap muted num nowrap">{fmtDur(time)} / {fmtDur(dur)}</span>
  {:else}
    <span class="grow"></span>
  {/if}
</div>
{#if mode === 'full' && segs.length}
  <p class="cap muted">{segs.length} restored {segs.length === 1 ? 'moment is' : 'moments are'} marked on the line.</p>
{/if}
