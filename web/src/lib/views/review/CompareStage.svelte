<script lang="ts">
  import { mediaUrl, MOCK } from '../../api/client';
  import { jobMedia } from '../../api/jobs';
  import { thumbUrl, videoUrl } from '../../api/library';
  import type { Job } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import Seg from '../../ui/Seg.svelte';
  import { fmtDur, gbf, resName } from '../../util/format';
  import { BLOCKS } from '../jobs/jobInfo';
  import Clip from '../jobs/Clip.svelte';

  /* Two videos share a timeline; the slider wipes between them. */
  type Mode = 'full' | 'moments' | 'sample';
  type Ab = 'split' | 'before' | 'after';
  let { job }: { job: Job } = $props();

  const MODE_LABEL: Record<Mode, string> = { full: 'Full video', moments: 'Restored moments', sample: 'Sample reel' };
  const segs = $derived(job.segments ?? []);
  const modes = $derived.by<Mode[]>(() => {
    const m: Mode[] = [];
    if (job.review_scene_id) m.push('full');
    if (segs.length) m.push('moments');
    if (job.sample) m.push('sample');
    return m;
  });
  let pickedMode = $state<Mode | null>(null);
  const mode = $derived<Mode | undefined>(pickedMode && modes.includes(pickedMode) ? pickedMode : modes[0]);
  let moment = $state(0);
  const seg = $derived(segs[Math.min(moment, Math.max(0, segs.length - 1))]);
  const single = $derived(mode === 'sample');

  const nativeHls = !MOCK && !!document.createElement('video').canPlayType('application/vnd.apple.mpegurl');
  let compatibleBefore = $state(false);
  let compatibleAfter = $state(false);
  const compatible = $derived(mode === 'full' && (compatibleBefore || compatibleAfter));
  const sceneVideo = (id: string, hls: boolean) => hls ? mediaUrl(`api/vid/${id}.m3u8`) : videoUrl(id);
  const beforeSrc = $derived(mode === 'moments' && seg ? jobMedia.segBefore(job.id, seg.n) : sceneVideo(job.scene_id, compatibleBefore));
  const afterSrc = $derived(mode === 'moments' && seg ? jobMedia.segAfter(job.id, seg.n) : mode === 'sample' ? jobMedia.sample(job.id) : job.review_scene_id ? sceneVideo(job.review_scene_id, compatibleAfter) : '');
  let attempt = $state(0);
  const clipKey = $derived(`${beforeSrc}|${afterSrc}|${attempt}`);

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

  /* The restored clip leads when it is available. If a side fails, keep the other usable. */
  let before = $state<HTMLVideoElement>();
  let after = $state<HTMLVideoElement>();
  let failedBefore = $state(false);
  let failedAfter = $state(false);
  let playing = $state(false);
  let requested = $state(false);
  let time = $state(MOCK ? 768 : 0);
  let loadedDur = $state(0);
  let target: number | null = null;
  let restartAt: number | null = null;
  let playRequest = 0;
  const leader = $derived(!failedAfter && after ? after : !failedBefore ? before : undefined);
  const follower = $derived(leader === after && !failedBefore ? before : undefined);
  const ready = $derived(loadedDur > 0);
  const maxEnd = $derived(segs.reduce((m, s) => Math.max(m, s.end), 0));
  const dur = $derived(loadedDur || (mode === 'full' ? job.result?.duration || maxEnd : 0));

  $effect.pre(() => {
    clipKey;
    playRequest += 1;
    failedBefore = failedAfter = playing = requested = false;
    loadedDur = 0;
    target = restartAt;
    time = target ?? (MOCK ? 768 : 0);
    restartAt = null;
  });

  function sync(media: HTMLVideoElement, event?: string): void {
    if (media !== leader) {
      if (event === 'loadedmetadata' && media === follower && leader?.readyState) {
        align(media, leader.currentTime);
      }
      return;
    }
    playing = !media.paused && !media.ended;
    if (Number.isFinite(media.duration) && media.duration > 0) loadedDur = media.duration;
    if (target !== null && media.readyState >= HTMLMediaElement.HAVE_METADATA) {
      align(media, target);
      target = null;
    }
    if (target === null) time = media.currentTime;
    if (!follower || follower.readyState < HTMLMediaElement.HAVE_METADATA) return;
    if (!follower.seeking || event === 'seeked') align(follower, media.currentTime, 0.3);
    if (!playing && !requested) follower.pause();
  }

  function align(media: HTMLVideoElement, t: number, tolerance = 0): void {
    const to = Number.isFinite(media.duration) ? Math.min(t, media.duration) : t;
    if (Math.abs(media.currentTime - to) > tolerance) media.currentTime = to;
  }

  function fail(media: HTMLVideoElement, allowCompatibility = true): void {
    if (media !== before && media !== after) return;
    if (allowCompatibility && mode === 'full' && nativeHls) {
      if (media === before && !compatibleBefore) {
        restartAt = time;
        compatibleBefore = true;
        return;
      }
      if (media === after && !compatibleAfter) {
        useCompatibleAfter();
        return;
      }
    }
    media.pause();
    if (media === before) failedBefore = true;
    else failedAfter = true;
    loadedDur = 0;
    if (leader) sync(leader);
    else playing = requested = false;
  }

  function play(media: HTMLVideoElement): Promise<void> {
    return media.play().catch((error: DOMException) => {
      // Switching clips or pressing Pause cancels the pending play. That is not a load failure.
      if (error.name !== 'AbortError') fail(media, error.name !== 'NotAllowedError');
    });
  }

  function toggle(): void {
    if (!leader) return;
    const id = ++playRequest;
    if (playing || requested) {
      requested = false;
      before?.pause();
      after?.pause();
      return;
    }
    requested = true;
    // Both calls must happen during the tap; Safari may block a later call outside that gesture.
    const lead = play(leader);
    const follow = follower ? play(follower) : undefined;
    void Promise.all([lead, follow]).then(() => {
      if (id !== playRequest) return;
      requested = false;
      if (leader) sync(leader);
    });
  }

  function seek(t: number): void {
    if (!leader || !Number.isFinite(t)) return;
    time = t;
    if (leader.readyState >= HTMLMediaElement.HAVE_METADATA) align(leader, t);
    else target = t;
    if (follower && follower.readyState >= HTMLMediaElement.HAVE_METADATA) align(follower, t);
  }
  function useCompatibleAfter(): void {
    restartAt = time;
    compatibleAfter = true;
  }
  function retry(): void {
    restartAt = time;
    attempt += 1;
  }
  const r = $derived(job.result);
  const beforeInfo = $derived([r?.from_height != null ? resName(r.from_height) : null, r?.from_bytes != null ? gbf(r.from_bytes / 1024 ** 3) : null].filter(Boolean).join(' · '));
  const afterInfo = $derived([r?.to_height != null ? resName(r.to_height) : null, r?.to_bytes != null ? gbf(r.to_bytes / 1024 ** 3) : null].filter(Boolean).join(' · '));
  const AB_OPTIONS = [{ value: 'split', label: 'Split' }, { value: 'before', label: 'Before' }, { value: 'after', label: 'After' }] as const;
  const MODE_OPTIONS = $derived(modes.map((m) => ({ value: m, label: MODE_LABEL[m] })));
</script>

{#if !mode}
  <div class="banner inf" role="status"><Icon name="info" /><span>No review video is available for this job.</span></div>
{:else}

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
      {#if MOCK}<div class="scene big"></div><div class="patch blocks">{#each BLOCKS as l, i (i)}<i style:--l={l}></i>{/each}</div>{/if}
      {#if !MOCK && mode === 'full'}<img src={thumbUrl(job.scene_id)} alt="" />{/if}
      {#key clipKey}<Clip src={beforeSrc} label="Original video" bind:video={before} loop={mode !== 'full'} onsync={sync} onerror={fail} />{/key}
    </div>
  {/if}
  <div class="lay after">
    {#if MOCK}<div class="scene big"></div><div class="patch clean"></div>{/if}
    {#if !MOCK && mode === 'full' && job.review_scene_id}<img src={thumbUrl(job.review_scene_id)} alt="" />{/if}
    {#key clipKey}<Clip src={afterSrc} label="Restored video" bind:video={after} loop={mode !== 'full'} onsync={sync} onerror={fail} />{/key}
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
  <button type="button" class="iconbtn" aria-label={playing || requested ? 'Pause' : 'Play'} disabled={!MOCK && !leader} onclick={toggle}><Icon name={playing || requested ? 'pause' : 'play'} /></button>
  {#if dur > 0}
    <div class="tline grow">
      <div class="tk" aria-hidden="true">
        {#if mode === 'full'}{#each segs as s (s.n)}<i style:left={`${(s.start / dur) * 100}%`} style:width={`${Math.max(0.6, ((s.end - s.start) / dur) * 100)}%`}></i>{/each}{/if}
        <em style:left={`${Math.min(100, (time / dur) * 100)}%`}></em>
      </div>
      <input class="seek" type="range" min="0" max={dur} step="any" value={time} aria-label="Position" aria-valuetext={`${fmtDur(time)} of ${fmtDur(dur)}`} disabled={!MOCK && !ready}
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
{#if !MOCK && (failedBefore || failedAfter)}
  <div class="row wrap">
    <span class="cap" role="status">{failedBefore && failedAfter ? 'Neither video could play.' : failedAfter ? 'The restored video could not play.' : 'The original video could not play.'}</span>
    <button type="button" class="chip" onclick={retry}><Icon name="refresh" small />Retry video</button>
  </div>
{:else if !MOCK && requested && !playing}
  <p class="cap muted" role="status">Loading video…</p>
{/if}
{#if compatible}
  <p class="cap muted" role="status">{compatibleBefore && compatibleAfter ? 'Both videos' : compatibleAfter ? 'Restored video' : 'Original video'} · compatible playback up to 1080p</p>
{/if}
{#if nativeHls && mode === 'full' && !compatibleAfter}
  <div><button type="button" class="chip" onclick={useCompatibleAfter}>Use compatible playback for After</button></div>
{/if}
{/if}
