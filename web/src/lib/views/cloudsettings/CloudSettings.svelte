<script lang="ts">
  import { SvelteMap } from 'svelte/reactivity';
  import { testCloudKey } from '../../api/cloudSettings';
  import type { CloudActive, CloudRental, SecretState } from '../../api/types';
  import { budget } from '../../stores/budget.svelte';
  import { toast } from '../../stores/toast.svelte';
  import Dialog from '../../ui/Dialog.svelte';
  import Icon from '../../ui/Icon.svelte';
  import type { IconName } from '../../ui/icons';
  import PageHead from '../../ui/PageHead.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import Status from '../../ui/Status.svelte';
  import Stepper from '../../ui/Stepper.svelte';
  import Switch from '../../ui/Switch.svelte';
  import { fmtWhen, usd } from '../../util/format';

  const msgOf = (e: unknown, fallback: string): string => (e instanceof Error && e.message ? e.message : fallback);

  /* ---------- keys ---------- */
  type KeyField = 'vast_api_key' | 'runpod_api_key' | 'jasna_license';
  interface KeyMeta { title: string; full: string; what: string; provider: 'vast' | 'runpod' | null; icon: IconName; tone: string; active: string }
  const KEY_META: Record<KeyField, KeyMeta> = {
    vast_api_key: { title: 'Vast.ai', full: 'Vast.ai API key', what: 'API key', provider: 'vast', icon: 'cloud', tone: 'inf', active: 'Connected' },
    runpod_api_key: { title: 'RunPod', full: 'RunPod API key', what: 'API key', provider: 'runpod', icon: 'cloud', tone: 'inf', active: 'Connected' },
    jasna_license: { title: 'Jasna licence', full: 'Jasna licence key', what: 'Licence key', provider: null, icon: 'shieldCheck', tone: 'pos', active: 'Active' },
  };

  const settings = $derived(budget.settings);
  const status = $derived(budget.status);

  /** result of a row's Test button */
  const rowTest = new SvelteMap<KeyField, { busy: boolean; ok: boolean; text: string }>();

  function balanceOf(field: KeyField): number | null {
    const p = KEY_META[field].provider;
    if (!p) return null;
    const row = status?.providers?.find((x) => x.id === p);
    return row?.balance ?? (p === 'vast' ? status?.credit ?? null : null);
  }

  function keyHint(field: KeyField, s: SecretState): string {
    if (!s.set) return field === 'jasna_license' ? 'No licence saved yet.' : 'No key saved yet.';
    const noun = field === 'jasna_license' ? 'Supporter licence' : 'Key';
    const bal = balanceOf(field);
    return [s.last4 ? `${noun} ending …${s.last4}` : `${noun} saved`, bal !== null ? `credit ${usd(bal)}` : null, s.source === 'env' ? 'set by the server' : null].filter((x): x is string => !!x).join(' · ');
  }

  async function testRow(field: KeyField): Promise<void> {
    const provider = KEY_META[field].provider;
    if (!provider || rowTest.get(field)?.busy) return;
    rowTest.set(field, { busy: true, ok: false, text: 'Testing…' });
    try {
      const r = await testCloudKey(undefined, provider);
      rowTest.set(field, { busy: false, ok: r.ok, text: r.ok ? (r.balance != null ? `Key works. Balance ${usd(r.balance)}.` : 'Key works.') : r.error ?? 'The provider did not accept the key.' });
    } catch (e) {
      rowTest.set(field, { busy: false, ok: false, text: msgOf(e, 'Could not reach the provider.') });
    }
  }

  /* add / replace dialog */
  let keyEdit = $state<KeyField | null>(null);
  let keyText = $state('');
  let keyError = $state('');
  let keyBusy = $state(false);
  let keyTest = $state<{ busy: boolean; ok: boolean; text: string } | null>(null);

  function openKey(field: KeyField): void {
    keyText = ''; keyError = ''; keyTest = null;
    keyEdit = field;
  }
  async function testTyped(): Promise<void> {
    const f = keyEdit;
    if (!f || keyTest?.busy) return;
    const provider = KEY_META[f].provider;
    if (!provider) return;
    if (!keyText.trim()) { keyError = 'Paste the key first.'; return; }
    keyError = '';
    keyTest = { busy: true, ok: false, text: 'Testing…' };
    try {
      const r = await testCloudKey(keyText.trim(), provider);
      keyTest = { busy: false, ok: r.ok, text: r.ok ? (r.balance != null ? `Key works. Balance ${usd(r.balance)}.` : 'Key works.') : r.error ?? 'The provider did not accept the key.' };
    } catch (e) {
      keyTest = { busy: false, ok: false, text: msgOf(e, 'Could not reach the provider.') };
    }
  }
  async function saveKey(): Promise<void> {
    const f = keyEdit;
    if (!f || keyBusy) return;
    if (!keyText.trim()) { keyError = 'Paste the key first.'; return; }
    keyBusy = true;
    keyError = '';
    try {
      await budget.save({ [f]: keyText.trim() });
      rowTest.delete(f);
      toast.show(`${KEY_META[f].title} key saved`);
      keyEdit = null;
    } catch (e) {
      keyError = msgOf(e, 'The server refused the key.');
    } finally {
      keyBusy = false;
    }
  }

  /* remove dialog */
  let keyRemove = $state<KeyField | null>(null);
  let removeBusy = $state(false);
  let removeError = $state('');
  async function confirmRemove(): Promise<void> {
    const f = keyRemove;
    if (!f || removeBusy) return;
    removeBusy = true;
    removeError = '';
    try {
      await budget.save({ [f]: null });
      rowTest.delete(f);
      toast.show(`${KEY_META[f].title} key removed`);
      keyRemove = null;
    } catch (e) {
      removeError = msgOf(e, 'The server refused to remove it.');
    } finally {
      removeBusy = false;
    }
  }

  /* ---------- limits: shown at once, saved 250 ms after the last tap, put back if the server says no ---------- */
  type NumKey = 'max_price' | 'job_cap' | 'month_cap' | 'max_bw_cost' | 'max_job_hours';
  type Draft = { max_price?: number; job_cap?: number; month_cap?: number; max_bw_cost?: number; max_job_hours?: number; datacenter_only?: boolean };
  const DRAFT_KEYS: readonly (keyof Draft)[] = ['max_price', 'job_cap', 'month_cap', 'max_bw_cost', 'max_job_hours', 'datacenter_only'];

  interface NumDef {
    key: NumKey;
    title: string;
    hint: string;
    min: number;
    max: number;
    /** [values below this, step] pairs, smallest first */
    steps: readonly (readonly [number, number])[];
    show: (v: number) => string;
  }
  const NUM_DEFS: readonly NumDef[] = [
    { key: 'max_price', title: 'Most per hour', hint: 'Machines dearer than this are greyed out.', min: 0.1, max: 50, steps: [[Infinity, 0.1]], show: (v) => usd(v) },
    { key: 'job_cap', title: 'Safety stop per rental', hint: 'A run that reaches this stops and nothing partial is kept.', min: 0.5, max: 1000, steps: [[10, 0.5], [100, 5], [Infinity, 25]], show: (v) => usd(v) },
    { key: 'month_cap', title: 'Monthly limit', hint: 'Runs that would pass it are refused.', min: 1, max: 10000, steps: [[50, 1], [200, 5], [Infinity, 25]], show: (v) => usd(v) },
    { key: 'max_bw_cost', title: 'Most per GB of transfer', hint: 'Hosts that charge more for internet traffic are skipped.', min: 0.005, max: 10, steps: [[0.1, 0.005], [1, 0.05], [Infinity, 0.5]], show: (v) => `${usd(v, 3)}/GB` },
    { key: 'max_job_hours', title: 'Longest rental', hint: 'A rental is shut down after this long, whatever it is doing.', min: 0.5, max: 168, steps: [[12, 0.5], [Infinity, 6]], show: (v) => `${+v.toFixed(1)} h` },
  ];

  let draft = $state<Draft>({});
  let pending: Draft = {};
  let limitError = $state('');
  let timer: ReturnType<typeof setTimeout> | undefined;
  let flushing = false;

  const numValue = (k: NumKey): number => draft[k] ?? settings?.[k] ?? 0;
  const dcValue = $derived(draft.datacenter_only ?? settings?.datacenter_only ?? false);
  /** only show the optional limits the server actually reports */
  const numDefs = $derived(NUM_DEFS.filter((d) => settings?.[d.key] != null));

  function stepOf(d: NumDef, v: number, dir: -1 | 1): number {
    const probe = dir < 0 ? v - d.min / 1000 : v;
    return d.steps.find(([below]) => probe < below)?.[1] ?? 1;
  }
  function nudge(d: NumDef, dir: -1 | 1): void {
    const v = numValue(d.key);
    const next = Math.round((v + dir * stepOf(d, v, dir)) * 1000) / 1000;
    if (next < d.min || next > d.max) return;
    const patch: Draft = {};
    patch[d.key] = next;
    queue(patch);
  }

  function queue(patch: Draft): void {
    Object.assign(draft, patch);
    Object.assign(pending, patch);
    limitError = '';
    clearTimeout(timer);
    timer = setTimeout(() => void flush(), 250);
  }

  async function flush(): Promise<void> {
    if (flushing) return; // the running flush picks up whatever piled up
    flushing = true;
    try {
      while (Object.keys(pending).length > 0) {
        const patch = pending;
        pending = {};
        try {
          await budget.save(patch);
          for (const k of DRAFT_KEYS) if (k in patch && !(k in pending)) delete draft[k];
        } catch (e) {
          // later taps were built on the refused value: drop them all and show what the server has
          pending = {};
          draft = {};
          limitError = msgOf(e, 'The server refused that change.');
        }
      }
    } finally {
      flushing = false;
    }
  }

  /* leaving the page must not lose a tap that is still waiting for its timer */
  $effect(() => () => {
    clearTimeout(timer);
    if (Object.keys(pending).length > 0) void flush();
  });

  /* ---------- this month + rentals ---------- */
  const spent = $derived(status?.month_to_date ?? 0);
  const monthCap = $derived(numValue('month_cap'));
  const pct = $derived(monthCap > 0 ? Math.min(100, (spent / monthCap) * 100) : 0);
  const left = $derived(Math.max(0, monthCap - spent));

  const PRESETS: Record<string, string> = { 'close-ups': 'Fast', detail: 'Balanced', 'detail-close-ups': 'High detail' };
  interface LedgerRow { key: string; when: string; scene: string; preset: string | null; machine: string; usd: number; live: boolean }
  const rows = $derived.by((): LedgerRow[] => {
    if (!status) return [];
    const live = status.active.map((a: CloudActive, i): LedgerRow => ({
      key: `a${a.id ?? i}`, live: true, usd: a.usd ?? 0,
      when: a.started ? fmtWhen(a.started) : 'Now',
      scene: a.scene ?? (a.scene_id ? `Scene ${a.scene_id}` : 'Rental'),
      preset: a.preset ? PRESETS[a.preset] ?? a.preset : null,
      machine: [a.gpu, a.provider].filter(Boolean).join(' · '),
    }));
    const done = status.recent.map((r: CloudRental, i): LedgerRow => ({
      key: `r${i}`, live: false, usd: r.usd,
      when: fmtWhen(r.ended),
      scene: r.scene ?? (r.scene_id ? `Scene ${r.scene_id}` : 'Rental'),
      preset: r.preset ? PRESETS[r.preset] ?? r.preset : null,
      machine: [r.gpu, r.provider].filter(Boolean).join(' · '),
    }));
    return [...live, ...done];
  });
</script>

{#snippet keyRow(field: KeyField, s: SecretState)}
  {@const m = KEY_META[field]}
  {@const test = rowTest.get(field)}
  <div class="setrow wraps">
    <span class={['gt', m.tone]}><Icon name={m.icon} /></span>
    <div class="sl">
      <b>{m.title}</b>
      <span>{keyHint(field, s)}</span>
      {#if test}<span class={['tres', test.busy ? '' : test.ok ? 'ok' : 'bad']} role="status">{#if !test.busy}<Icon name={test.ok ? 'checkCircle' : 'xCircle'} small />{/if}{test.text}</span>{/if}
    </div>
    {#if s.set}<Status state="ok" word={m.active} />{:else}<Status state="warn" word="Not set" />{/if}
    <div class="sact">
      {#if s.set && m.provider}
        <button type="button" class="btn" aria-disabled={test?.busy || undefined} onclick={() => testRow(field)}><Icon name="bolt" />Test</button>
      {/if}
      {#if s.source !== 'env'}
        <button type="button" class="btn" onclick={() => openKey(field)}><Icon name="key" />{s.set ? 'Replace key' : 'Add key'}</button>
        {#if s.set}<button type="button" class="iconbtn" aria-label={`Remove ${m.title} key`} onclick={() => { removeError = ''; keyRemove = field; }}><Icon name="trash" /></button>{/if}
      {/if}
    </div>
  </div>
{/snippet}

<div class="narrow">
  <PageHead title="Cloud settings" sub="Provider, limits and this month" />

  {#if !settings}
    {#if budget.error}
      <div class="banner bad" role="alert">
        <Icon name="alert" /><span>Couldn’t load the cloud settings. {budget.error}</span>
        <button type="button" class="btn right" onclick={() => budget.loadSettings()}><Icon name="refresh" />Retry</button>
      </div>
    {:else}
      <div class="panel card cset" aria-busy="true">
        <div class="col" style="gap: var(--lu-space-3); padding: var(--lu-space-4) 0"><Skeleton small width="35%" /><Skeleton small width="70%" /><Skeleton small width="50%" /></div>
      </div>
    {/if}
  {:else}
    <div class="panel card cset">
      {@render keyRow('vast_api_key', settings.vast_api_key)}
      {#if settings.runpod_api_key}
        {@render keyRow('runpod_api_key', settings.runpod_api_key)}
      {:else}
        <div class="setrow wraps">
          <span class="gt mute"><Icon name="cloud" /></span>
          <div class="sl"><b>RunPod</b><span>More providers plug in here. Their offers join the same list.</span></div>
          <span class="badge">Coming soon</span>
        </div>
      {/if}
      {#if settings.jasna_license}{@render keyRow('jasna_license', settings.jasna_license)}{/if}

      {#each numDefs as d (d.key)}
        <div class="setrow wraps">
          <div class="sl"><b>{d.title}</b><span>{d.hint}</span></div>
          <Stepper label={d.title.toLowerCase()} display={d.show(numValue(d.key))} onstep={(dir) => nudge(d, dir)} canDec={numValue(d.key) - stepOf(d, numValue(d.key), -1) >= d.min - 1e-9} canInc={numValue(d.key) + stepOf(d, numValue(d.key), 1) <= d.max + 1e-9} />
        </div>
      {/each}
      <div class="setrow">
        <div class="sl"><b>Data centres only</b><span>Skip community hosts, always.</span></div>
        <Switch label="Data centres only" checked={dcValue} onchange={(v) => queue({ datacenter_only: v })} />
      </div>
      {#if limitError}
        <div class="sect"><p class="rerr" role="alert"><Icon name="alert" small />{limitError} The previous value is back.</p></div>
      {/if}

      <div class="sect">
        <div class="row" style="justify-content: space-between; margin-bottom: var(--lu-space-2)">
          <b>This month</b>
          {#if status}<span class="cap muted num">{usd(spent)} of {usd(monthCap)}</span>{/if}
        </div>
        {#if status}
          <div class="bar thick" role="img" aria-label={`${usd(spent)} of ${usd(monthCap)} spent`}><i class="spent" style:width={`${pct}%`}></i></div>
          <p class="cap muted num" style="margin-top: var(--lu-space-2)">
            {#if spent >= monthCap}<Icon name="alert" small /> Limit reached. New runs are refused until next month or a higher limit.{:else}{usd(left)} left this month{/if}{#if status.credit != null} · credit {usd(status.credit)}{/if}
          </p>
        {:else}
          <Skeleton small width="100%" />
        {/if}
      </div>

      <div class="sect">
        {#if !status}
          <div class="col" style="gap: var(--lu-space-2)"><Skeleton small width="100%" /><Skeleton small width="80%" /></div>
        {:else if rows.length === 0}
          <p class="cap muted">No rentals yet.</p>
        {:else}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex: a scrollable table must be reachable by keyboard -->
          <div class="tscroll" role="region" aria-label="Recent rentals" tabindex="0">
            <table class="ledger">
              <caption class="sr">Recent rentals</caption>
              <thead><tr><th scope="col">When</th><th scope="col">Scene</th><th scope="col">Machine</th><th scope="col" class="r">Cost</th></tr></thead>
              <tbody>
                {#each rows as r (r.key)}
                  <tr>
                    <td class="muted">{r.when}</td>
                    <td>{r.scene}{#if r.preset}<span class="cap muted"> · {r.preset}</span>{/if}{#if r.live} <span class="badge acc"><i class="dot"></i>Live</span>{/if}</td>
                    <td>{r.machine}</td>
                    <td class="r num">{r.live ? 'so far ' : ''}{usd(r.usd)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </div>
    </div>
  {/if}
</div>

{#if keyEdit}
  {@const m = KEY_META[keyEdit]}
  <Dialog title={`${settings?.[keyEdit]?.set ? 'Replace' : 'Add'} ${m.full}`} onclose={() => { if (!keyBusy) keyEdit = null; }}>
    <form id="key-form" class="dform" onsubmit={(e) => { e.preventDefault(); void saveKey(); }}>
      <label class="flab"><span>{m.what}</span><span class="field"><input type="password" bind:value={keyText} autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Paste it here" /></span></label>
      <p class="cap muted">It is stored on your server only, and never shown again. Only the last four characters stay visible.</p>
      <div role="status" aria-live="polite">
        {#if keyTest}<p class={['fmsg', keyTest.busy ? '' : keyTest.ok ? 'ok' : 'bad']}>{#if !keyTest.busy}<Icon name={keyTest.ok ? 'checkCircle' : 'xCircle'} small />{/if}{keyTest.text}</p>{/if}
      </div>
      {#if keyError}<p class="fmsg bad" role="alert"><Icon name="alert" small />{keyError}</p>{/if}
    </form>
    {#snippet actions()}
      <button type="button" class="btn" onclick={() => { if (!keyBusy) keyEdit = null; }}>Cancel</button>
      {#if m.provider}<button type="button" class="btn" aria-disabled={keyTest?.busy || undefined} onclick={testTyped}><Icon name="bolt" />Test</button>{/if}
      <button type="submit" form="key-form" class="btn primary" aria-disabled={keyBusy || undefined}>{keyBusy ? 'Saving…' : 'Save'}</button>
    {/snippet}
  </Dialog>
{/if}

{#if keyRemove}
  {@const m = KEY_META[keyRemove]}
  <Dialog title={`Remove the ${m.full}?`} onclose={() => { if (!removeBusy) keyRemove = null; }}>
    <p>{m.provider ? 'Cloud runs on this provider stop being available until you add a key again.' : 'Cloud runs that need it stop being available until you add a licence again.'}</p>
    {#if removeError}<p class="fmsg bad" role="alert"><Icon name="alert" small />{removeError}</p>{/if}
    {#snippet actions()}
      <!-- svelte-ignore a11y_autofocus: the safe choice takes focus when the dialog opens -->
      <button type="button" class="btn" autofocus onclick={() => { keyRemove = null; }}>Keep it</button>
      <button type="button" class="btn t-danger" aria-disabled={removeBusy || undefined} onclick={confirmRemove}><Icon name="trash" />{removeBusy ? 'Removing…' : 'Remove'}</button>
    {/snippet}
  </Dialog>
{/if}
