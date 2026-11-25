<script lang="ts">
  import { isAbort } from '../../api/client';
  import { live } from '../../api/events';
  import { jobLog } from '../../api/jobs';
  import type { JobLog, JobLogLine } from '../../api/types';
  import Icon from '../../ui/Icon.svelte';
  import { logNorm } from './jobInfo';

  /* A job's live log. Lines that differ only by numbers merge into one (a progress line stays one line),
     at most 500 are kept, and the view follows the newest line until the person scrolls up. */
  interface Row extends JobLogLine { key: number; norm: string }
  const CAP = 500;
  const LEVEL: Record<string, string> = { warn: 'Warning', error: 'Error' };

  let { id, active, startOpen }: { id: string; active: boolean; startOpen: boolean } = $props();

  const uid = $props.id();
  let openRaw = $state<boolean | null>(null);
  const open = $derived(openRaw ?? startOpen);
  let rows = $state.raw<Row[]>([]);
  let box = $state<HTMLDivElement>();
  let cursor = 0;
  let lastSeq = 0;
  let follow = true;
  let failed = $state(false);

  function apply(d: JobLog): void {
    failed = false;
    cursor = d.cursor ?? cursor;
    const next = rows.slice();
    let changed = false;
    for (const ln of d.lines) {
      if (ln.seq <= lastSeq) continue;
      lastSeq = ln.seq;
      changed = true;
      const norm = logNorm(ln.text);
      const last = next[next.length - 1];
      if (last && last.norm === norm) next[next.length - 1] = { ...ln, key: last.key, norm };
      else next.push({ ...ln, key: ln.seq, norm });
    }
    if (changed) rows = next.length > CAP ? next.slice(-CAP) : next;
  }

  /* Only while the log is open: stream it while the job runs, read it once when the job is over. */
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
    <Icon name={open ? 'chevD' : 'chevR'} />Live log{#if rows.length}<span class="cap muted num">{rows.length} lines</span>{/if}
  </button>
  {#if open}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex: a scrollable log must be reachable by keyboard -->
    <div class="logbody" id={`${uid}-b`} role="log" aria-label="Job log" tabindex="0" bind:this={box} {onscroll}>
      {#each rows as r (r.key)}
        <div class={['ln', `lv-${r.level}`]}>{#if LEVEL[r.level]}<span class="lvl">{LEVEL[r.level]}</span>{/if}{r.text}</div>
      {:else}
        <div class="ln lv-proc">{failed ? 'Could not load the log.' : 'Waiting for output…'}</div>
      {/each}
    </div>
  {/if}
</div>
