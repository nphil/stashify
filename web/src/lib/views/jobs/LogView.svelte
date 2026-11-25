<script lang="ts">
  import { isAbort } from '../../api/client';
  import { live } from '../../api/events';
  import { jobLog } from '../../api/jobs';
  import type { JobLog, JobLogLine } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import { isLiveLine } from './jobInfo';

  /* The raw log behind a "Details" disclosure. Real events, warnings and errors append; every progress-bar
     line replaces ONE pinned live line at the bottom, so a long run is a short list. At most CAP lines are
     kept, and the view follows the newest line only while the person is at the bottom. */
  const CAP = 300;
  const LEVEL: Record<string, string> = { warn: 'Warning', error: 'Error' };

  /** `raw` is a line to show first (the failure's own words) when the job is over */
  let { id, active, startOpen, raw = '' }: { id: string; active: boolean; startOpen: boolean; raw?: string } = $props();

  const uid = $props.id();
  let openRaw = $state<boolean | null>(null);
  const open = $derived(openRaw ?? startOpen);
  let rows = $state.raw<(JobLogLine & { key: number })[]>([]);
  let liveLine = $state('');
  let box = $state<HTMLDivElement>();
  let cursor = 0;
  let lastSeq = 0;
  let follow = true;
  let failed = $state(false);

  function apply(d: JobLog): void {
    failed = false;
    cursor = d.cursor ?? cursor;
    let next = rows;
    let copied = false;
    let bar = liveLine;
    for (const ln of d.lines) {
      if (ln.seq <= lastSeq) continue;
      lastSeq = ln.seq;
      if (isLiveLine(ln)) { bar = ln.text; continue; }
      const last = next[next.length - 1];
      if (last && last.level === ln.level && last.text === ln.text) continue;      // the same line twice in a row
      if (!copied) { next = next.slice(); copied = true; }
      next.push({ ...ln, key: ln.seq });
    }
    if (copied) rows = next.length > CAP ? next.slice(-CAP) : next;
    if (bar !== liveLine) liveLine = bar;
  }

  /* Only while the details are open: stream them while the job runs, read them once when it is over. */
  $effect(() => {
    if (!open) return;
    if (active) {
      return live<JobLog>(`log:${id}`, { poll: (signal) => jobLog(id, cursor, signal), everyMs: 1500, onData: apply, onError: () => (failed = true) });
    }
    const ctl = new AbortController();
    jobLog(id, cursor, ctl.signal).then(apply, (e: unknown) => { if (!isAbort(e)) failed = true; });
    return () => ctl.abort();
  });

  $effect(() => {
    void rows;
    void liveLine;
    if (box && follow) box.scrollTop = box.scrollHeight;
  });

  function onscroll(): void {
    if (box) follow = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  }
  function toggle(): void {
    openRaw = !open;
    follow = true;
  }
</script>

<div class="jlog">
  <button type="button" class="btn quiet" aria-expanded={open} aria-controls={`${uid}-b`} onclick={toggle}>
    <Icon name={open ? 'chevD' : 'chevR'} />Details
  </button>
  {#if open}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex: a scrollable log must be reachable by keyboard -->
    <div class="logbody" id={`${uid}-b`} role="log" aria-label="Job details" tabindex="0" bind:this={box} {onscroll}>
      {#if raw && !rows.some((r) => r.level === 'error' && r.text.includes(raw))}<div class="ln lv-error"><span class="lvl">Error</span>{raw}</div>{/if}
      {#each rows as r (r.key)}
        <div class={['ln', `lv-${r.level}`]}>{#if LEVEL[r.level]}<span class="lvl">{LEVEL[r.level]}</span>{/if}{r.text}</div>
      {:else}
        {#if !liveLine && !raw}<div class="ln lv-proc">{failed ? 'Could not load the details.' : 'Waiting for output…'}</div>{/if}
      {/each}
      {#if liveLine}<div class="ln lv-live" aria-hidden="true">{liveLine}</div>{/if}
    </div>
  {/if}
</div>
