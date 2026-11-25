<script lang="ts">
  import { SvelteMap } from 'svelte/reactivity';
  import { addRunner, discoverRunners, removeRunner, testRunner } from '../../api/runners';
  import type { DiscoveredRunner, GpuStats, OpId, Runner } from '../../api/types';
  import { OPS, QUALITIES, QUALITY_ORDER, engineOf, qualitiesOf } from '../../choices';
  import { budget } from '../../stores/budget.svelte';
  import { nav } from '../../stores/nav.svelte';
  import { runners } from '../../stores/runners.svelte';
  import { toast } from '../../stores/toast.svelte';
  import WakeButton from '../../shell/WakeButton.svelte';
  import Dialog from '../../ui/Dialog.svelte';
  import Icon from '../../ui/Icon.svelte';
  import type { IconName } from '../../ui/icons';
  import PageHead from '../../ui/PageHead.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import Status, { type StatusState } from '../../ui/Status.svelte';
  import Switch from '../../ui/Switch.svelte';
  import { fmtMin, plural, usd } from '../../util/format';

  /* what the server calls each op, mapped to the app's op ids */
  const OP_KEYS: Record<string, OpId> = { decensor: 'decensor', 'decensor+upscale': 'decensor_up', decensor_up: 'decensor_up', upscale: 'upscale', transcode: 'transcode' };
  const cap1 = (s: string): string => s.charAt(0).toUpperCase() + s.slice(1);
  const msgOf = (e: unknown, fallback: string): string => (e instanceof Error && e.message ? e.message : fallback);

  /* ---------- one row ---------- */
  const isPreferred = (r: Runner): boolean => !!r.prefer && r.prefer.length > 0;
  const isAsleep = (r: Runner): boolean => !r.online && (r.asleep ?? /^asleep/i.test(r.note ?? ''));

  function iconOf(r: Runner): IconName {
    const k = r.kind ?? '';
    if (/cloud/i.test(k)) return 'cloud';
    return /nas|server|rack|box/i.test(k) ? 'server' : 'monitor';
  }

  /** switch positions shown before the server confirms them */
  const preferGuess = new SvelteMap<string, boolean>();
  const rowError = new SvelteMap<string, string>();

  function statusOf(r: Runner, load: number | undefined): { state: StatusState; word: string } {
    if (r.online) {
      if (r.note && /token/i.test(r.note)) return { state: 'warn', word: 'Access token refused' };
      if (r.paused) return { state: 'warn', word: 'Paused' };
      if (r.busy) return { state: 'busy', word: load === undefined ? 'Busy' : `Busy · ${Math.round(load)}%` };
      return { state: 'ok', word: 'Ready' };
    }
    if (isAsleep(r)) {
      const w = runners.wakes[r.url];
      if (w && !w.failed) { const left = runners.wakeLeft(r.url); return { state: 'asleep', word: left ? `Waking · about ${left} s` : 'Waking up' }; }
      if (w?.failed) return { state: 'warn', word: 'Didn’t wake' };
      return { state: 'asleep', word: r.wake_eta_s ? `Asleep · wakes in about ${r.wake_eta_s} s` : 'Asleep' };
    }
    return { state: 'offline', word: 'Offline' };
  }

  function specLine(r: Runner): string {
    const engines = [...new Set(Object.entries(r.engines ?? {}).filter(([k]) => k !== 'transcode').map(([, v]) => cap1(v)))];
    let quality: string | null = null;
    if (engines.length > 0) {
      const q = r.qualities ?? qualitiesOf(r, 'decensor');
      quality = q.length >= QUALITY_ORDER.length ? 'every quality' : `${q.map((x) => QUALITIES[x].name).join(', ')} quality only`;
    }
    return [r.gpu ?? r.kind, r.vram_gb ? `${r.vram_gb} GB` : null, engines.join(' / ') || null, quality].filter((x): x is string => !!x).join(' · ');
  }

  interface OpBadge { key: string; label: string; icon: IconName; engine: string | null }
  function opBadges(r: Runner): OpBadge[] {
    return (r.ops ?? []).map((key) => {
      const id = OP_KEYS[key];
      if (!id) return { key, label: key, icon: 'bolt', engine: null };
      const e = r.engines ? engineOf(r, id) : undefined;
      return { key, label: OPS[id].name, icon: OPS[id].icon, engine: e ? cap1(e) : null };
    });
  }

  interface Gauge { k: string; v: string; pct?: number }
  function gaugesOf(g: GpuStats): Gauge[] {
    const gb = (mb: number): string => (mb / 1024).toFixed(1);
    return [
      { k: 'GPU', v: `${Math.round(g.util)}%`, pct: g.util },
      { k: 'Video memory', v: `${gb(g.mem_used)} / ${Math.round(g.mem_total / 1024)} GB`, pct: g.mem_total > 0 ? (g.mem_used / g.mem_total) * 100 : 0 },
      { k: 'Temperature', v: `${Math.round(g.temp)} °C`, pct: g.temp },
      { k: 'Power', v: `${Math.round(g.power)} W` },
    ];
  }

  /* ---------- actions on a row ---------- */
  /* ---------- wake address (MAC) for a runner that has none ---------- */
  let macFor = $state<string | null>(null);
  let macText = $state('');
  let macBusy = $state(false);
  async function saveMac(r: Runner): Promise<void> {
    const m = macText.trim();
    if (!m || macBusy) return;
    macBusy = true;
    rowError.delete(r.url);
    try {
      await addRunner({ name: r.name, url: r.url, mac: m });
      await runners.refresh();
      macFor = null;
      toast.show(`Saved the wake address for ${r.name}`);
    } catch (e) {
      rowError.set(r.url, msgOf(e, 'The server refused that address.'));
    } finally {
      macBusy = false;
    }
  }

  async function setPrefer(r: Runner, on: boolean): Promise<void> {
    rowError.delete(r.url);
    preferGuess.set(r.url, on);
    try {
      await addRunner({ name: r.name, url: r.url, prefer: on ? (r.ops ?? []) : [] });
      await runners.refresh();
    } catch (e) {
      rowError.set(r.url, msgOf(e, 'Could not save that change.'));
    } finally {
      preferGuess.delete(r.url);
    }
  }

  /* remove */
  let removing = $state<Runner | null>(null);
  let removeBusy = $state(false);
  let removeError = $state('');
  async function confirmRemove(): Promise<void> {
    const r = removing;
    if (!r || removeBusy) return;
    removeBusy = true;
    removeError = '';
    try {
      await removeRunner(r.url);
      await runners.refresh();
      toast.show(`Removed ${r.name}`);
      removing = null;
    } catch (e) {
      removeError = msgOf(e, 'Could not remove it.');
    } finally {
      removeBusy = false;
    }
  }

  /* ---------- add dialog ---------- */
  let adding = $state(false);
  let name = $state('');
  let url = $state('');
  let token = $state('');
  let mac = $state('');
  let prefer = $state(false);
  let formError = $state('');
  let saving = $state(false);

  type Found = { status: 'idle' } | { status: 'busy' } | { status: 'done'; list: DiscoveredRunner[] } | { status: 'error'; text: string };
  let found = $state<Found>({ status: 'idle' });

  type Test = { status: 'idle' } | { status: 'busy'; key: string } | { status: 'ok' | 'fail'; key: string; text: string; ops: string[] };
  let test = $state<Test>({ status: 'idle' });

  const cleanUrl = (): string => {
    const u = url.trim().replace(/\/+$/, '');
    return u && !/^https?:\/\//i.test(u) ? `http://${u}` : u;
  };
  const formKey = $derived(`${url.trim()}|${token.trim()}`);
  /** a test result only counts while the address and token are unchanged */
  const testNow = $derived(test.status !== 'idle' && test.key === formKey ? test : null);

  function openAdd(): void {
    name = ''; url = ''; token = ''; mac = ''; prefer = false; formError = '';
    found = { status: 'idle' };
    test = { status: 'idle' };
    adding = true;
  }

  async function find(): Promise<void> {
    found = { status: 'busy' };
    try { found = { status: 'done', list: await discoverRunners() }; } catch (e) { found = { status: 'error', text: msgOf(e, 'Could not search the network.') }; }
  }

  function pick(d: DiscoveredRunner): void {
    name = d.name;
    url = d.url;
    formError = '';
  }

  async function runTest(): Promise<void> {
    const target = cleanUrl();
    if (!target) { formError = 'Enter the machine’s address first.'; return; }
    formError = '';
    const key = formKey;
    test = { status: 'busy', key };
    try {
      const res = await testRunner({ url: target, token: token.trim() || undefined, name: name.trim() || undefined });
      if (key !== formKey) return;
      const ops = res.ops ?? [];
      const labels = ops.map((o) => (OP_KEYS[o] ? OPS[OP_KEYS[o]].name : o));
      test = res.ok
        ? { status: 'ok', key, ops, text: labels.length ? `Connected. It can run ${labels.join(', ')}.` : 'Connected.' }
        : { status: 'fail', key, ops: [], text: res.error ?? 'Could not connect.' };
    } catch (e) {
      if (key === formKey) test = { status: 'fail', key, ops: [], text: msgOf(e, 'Could not connect.') };
    }
  }

  async function save(): Promise<void> {
    if (saving) return;
    const target = cleanUrl();
    if (!target) { formError = 'Enter the machine’s address.'; return; }
    saving = true;
    formError = '';
    try {
      if (testNow?.status !== 'ok') await runTest();
      const t = testNow;
      if (t?.status !== 'ok') return; // the test result says why, right above the buttons
      await addRunner({ name: name.trim() || target, url: target, token: token.trim() || undefined, mac: mac.trim() || undefined, prefer: prefer ? t.ops : [] });
      await runners.refresh();
      toast.show(`Added ${name.trim() || target}`);
      adding = false;
    } catch (e) {
      formError = msgOf(e, 'The server refused it.');
    } finally {
      saving = false;
    }
  }

  /* ---------- cloud row ---------- */
  const running = $derived(budget.status?.active.length ?? 0);
</script>

<div class="narrow">
  <PageHead title="Runners" sub="Where jobs can run">
    {#snippet right()}
      <button type="button" class="btn" onclick={openAdd}><Icon name="plus" />Add runner</button>
    {/snippet}
  </PageHead>

  {#if !runners.loaded}
    <div class="panel card rlist" aria-busy="true">
      <div class="col" style="gap: var(--lu-space-3); padding: var(--lu-space-4) 0">
        <Skeleton rows={1} small width="30%" /><Skeleton rows={1} small width="60%" /><Skeleton rows={1} small width="45%" />
      </div>
    </div>
  {:else if runners.error && runners.list.length === 0}
    <div class="banner bad" role="alert">
      <Icon name="alert" /><span>Couldn’t load your runners. {runners.error}</span>
      <button type="button" class="btn right" onclick={() => runners.refresh()}><Icon name="refresh" />Retry</button>
    </div>
  {:else}
    {#if runners.error}
      <div class="banner" role="status" style="margin-bottom: var(--lu-space-3)">
        <Icon name="alert" /><span>Couldn’t refresh just now. Showing the last list.</span>
        <button type="button" class="btn right" onclick={() => runners.refresh()}><Icon name="refresh" />Retry</button>
      </div>
    {/if}
    <div class="panel card rlist">
      {#each runners.list as r (r.url)}
        {@const gpu = runners.meter(r.url)?.gpu}
        {@const st = statusOf(r, gpu?.util)}
        {@const fixed = r.source === 'env'}
        <article class="runner" aria-label={r.name}>
          <span class={['gt', !r.online && 'mute']}><Icon name={iconOf(r)} /></span>
          <div class="col" style="min-width: 0">
            <div class="rtop">
              <h2 class="h">{r.name}</h2>
              <Status state={st.state} word={st.word} />
              {#if r.node}<span class="cap muted">{r.node}</span>{/if}
            </div>
            {#if specLine(r)}<div class="rsub">{specLine(r)}</div>{/if}
            {#if r.online && r.busy && r.queue_eta_s}<div class="rsub">Starts yours in about {fmtMin(Math.ceil(r.queue_eta_s / 60))}.</div>{/if}
            {#if isAsleep(r) && r.can_wake}<div class="rsub">Wakes on request{r.wake_eta_s ? ` in about ${r.wake_eta_s} s` : ''}, then sleeps when idle.</div>{/if}
            {#if r.note && !isAsleep(r)}<div class="rsub">{cap1(r.note)}</div>{/if}
            {#if opBadges(r).length}
              <div class="chips" style="margin-top: var(--lu-space-2)">
                {#each opBadges(r) as b (b.key)}<span class="badge"><Icon name={b.icon} />{b.label}{b.engine ? ` · ${b.engine}` : ''}</span>{/each}
                {#if fixed}<span class="badge"><Icon name="lock" />Set by the server</span>{/if}
              </div>
            {/if}
            {#if gpu && r.online}
              <dl class="gauges" aria-label={`${r.name} GPU, live`}>
                {#each gaugesOf(gpu) as g (g.k)}
                  <div><dt>{g.k}</dt><dd><span class="num">{g.v}</span>{#if g.pct !== undefined}<span class="mini" aria-hidden="true"><i style:width={`${Math.min(100, Math.max(0, g.pct))}%`}></i></span>{/if}</dd></div>
                {/each}
              </dl>
            {/if}
            {#if macFor === r.url && !r.can_wake}
              <form class="macrow" onsubmit={(e) => { e.preventDefault(); void saveMac(r); }}>
                <label class="flab"><span>Wake address (MAC)</span><span class="field"><input type="text" bind:value={macText} placeholder="aa:bb:cc:dd:ee:ff" autocomplete="off" autocapitalize="off" spellcheck="false" /></span></label>
                <button type="submit" class="btn primary" aria-disabled={!macText.trim() || macBusy || undefined}>Save</button>
              </form>
            {/if}
            {#if rowError.get(r.url)}<p class="rerr" role="alert"><Icon name="alert" small />{rowError.get(r.url)}</p>{/if}
          </div>
          <div class="ract">
            {#if !r.online}
              <WakeButton runner={r} variant="btn" />
              {#if !r.can_wake && !fixed}<button type="button" class="btn quiet" onclick={() => { macFor = macFor === r.url ? null : r.url; macText = ''; }}>Add wake address</button>{/if}
            {/if}
            <span class="prefer">
              <span class="muted">Prefer</span>
              <Switch label={`Prefer ${r.name} for new jobs`} checked={preferGuess.get(r.url) ?? isPreferred(r)} disabled={fixed} onchange={(v) => setPrefer(r, v)} />
            </span>
            <button type="button" class="iconbtn" aria-label={`Remove ${r.name}`} aria-disabled={fixed || undefined} onclick={() => { if (!fixed) { removeError = ''; removing = r; } }}><Icon name="trash" /></button>
          </div>
        </article>
      {:else}
        <div class="empty">
          <span class="gt"><Icon name="server" /></span>
          <h2 class="h">No runner set up yet</h2>
          <p>A runner is a machine with a GPU that does the work. Add one on your network, or use Cloud for the big jobs.</p>
          <button type="button" class="btn" onclick={openAdd}><Icon name="plus" />Add runner</button>
        </div>
      {/each}

      <article class="runner" aria-label="Cloud GPU">
        <span class="gt inf"><Icon name="cloud" /></span>
        <div class="col" style="min-width: 0">
          <div class="rtop">
            <h2 class="h">Cloud GPU</h2>
            {#if budget.connected}<Status state="ok" word="Connected" />{:else}<Status state="warn" word="Not set up" />{/if}
            <span class="cap muted">Vast.ai</span>
          </div>
          <div class="rsub">
            {#if budget.connected && budget.left !== null}
              {usd(Math.max(0, (budget.caps?.monthCap ?? 0) - (budget.caps?.monthToDate ?? 0)))} left this month{#if budget.caps?.credit != null} · credit {usd(budget.caps.credit)}{/if}{#if running > 0} · {plural(running, 'rental')} running{/if}
            {:else}
              Add your provider key to rent a GPU for the big jobs.
            {/if}
          </div>
        </div>
        <div class="ract">
          <button type="button" class="btn" onclick={() => nav.go('cloud')}><Icon name="sliders" />{budget.connected ? 'Cloud settings' : 'Set up Cloud'}</button>
        </div>
      </article>
    </div>
  {/if}
</div>

{#if removing}
  <Dialog title={`Remove ${removing.name}?`} onclose={() => { if (!removeBusy) removing = null; }}>
    <p>Jobs will no longer go to this machine. Jobs already running there keep going. You can add it again later.</p>
    {#if removeError}<p class="rerr" role="alert"><Icon name="alert" small />{removeError}</p>{/if}
    {#snippet actions()}
      <!-- svelte-ignore a11y_autofocus: the safe choice takes focus when the dialog opens -->
      <button type="button" class="btn" autofocus onclick={() => { removing = null; }}>Keep it</button>
      <button type="button" class="btn t-danger" aria-disabled={removeBusy || undefined} onclick={confirmRemove}><Icon name="trash" />{removeBusy ? 'Removing…' : 'Remove'}</button>
    {/snippet}
  </Dialog>
{/if}

{#if adding}
  <Dialog title="Add runner" onclose={() => { if (!saving) adding = false; }}>
    <form id="add-runner" class="dform" onsubmit={(e) => { e.preventDefault(); void save(); }}>
      <div class="col" style="gap: var(--lu-space-2)">
        <button type="button" class="btn" aria-disabled={found.status === 'busy' || undefined} onclick={() => found.status !== 'busy' && find()}><Icon name="wifi" />Find machines on the network</button>
        <div role="status" aria-live="polite">
          {#if found.status === 'busy'}
            <div class="col" style="gap: var(--lu-space-2); padding-top: var(--lu-space-2)"><span class="cap muted">Looking for machines…</span><Skeleton rows={2} /></div>
          {:else if found.status === 'error'}
            <p class="rerr"><Icon name="alert" small />{found.text}</p>
          {:else if found.status === 'done'}
            {#if found.list.length === 0}
              <p class="cap muted">Nothing found. Check the machine is on, or type its address below.</p>
            {:else}
              <div class="opt-list" role="group" aria-label="Machines found on the network">
                {#each found.list as d (d.url)}
                  <button type="button" class="opt" aria-pressed={url.trim() === d.url} aria-disabled={d.registered || undefined} onclick={() => !d.registered && pick(d)}>
                    <span class="gt sm"><Icon name={/nas|server|rack|box/i.test(d.kind ?? '') ? 'server' : 'monitor'} /></span>
                    <span class="om"><span class="on">{d.name}</span><span class="oh">{d.url}</span></span>
                    {#if d.registered}<span class="badge"><Icon name="check" />Already added</span>{/if}
                  </button>
                {/each}
              </div>
            {/if}
          {/if}
        </div>
      </div>

      <label class="flab"><span>Name</span><span class="field"><input type="text" bind:value={name} placeholder="Garage PC" autocomplete="off" /></span></label>
      <label class="flab"><span>Address</span><span class="field"><input type="url" bind:value={url} placeholder="http://192.168.1.50:8711" autocomplete="off" autocapitalize="off" spellcheck="false" /></span></label>
      <label class="flab"><span>Access token</span><span class="field"><input type="password" bind:value={token} placeholder="Leave empty to use the shared one" autocomplete="off" /></span></label>
      <label class="flab"><span>Wake address (MAC), optional</span><span class="field"><input type="text" bind:value={mac} placeholder="aa:bb:cc:dd:ee:ff" autocomplete="off" autocapitalize="off" spellcheck="false" /></span></label>
      <div class="setrow" style="padding: 0">
        <div class="sl"><b>Prefer this machine</b><span>New jobs go here first when it is free.</span></div>
        <Switch bind:checked={prefer} label="Prefer this machine" />
      </div>

      <div role="status" aria-live="polite">
        {#if testNow?.status === 'busy'}
          <p class="cap muted">Testing the connection…</p>
        {:else if testNow?.status === 'ok'}
          <p class="fmsg ok"><Icon name="checkCircle" small />{testNow.text}</p>
        {:else if testNow?.status === 'fail'}
          <p class="fmsg bad"><Icon name="xCircle" small />{testNow.text}</p>
        {/if}
      </div>
      {#if formError}<p class="fmsg bad" role="alert"><Icon name="alert" small />{formError}</p>{/if}
    </form>
    {#snippet actions()}
      <button type="button" class="btn" onclick={() => { if (!saving) adding = false; }}>Cancel</button>
      <button type="button" class="btn" aria-disabled={testNow?.status === 'busy' || undefined} onclick={() => testNow?.status !== 'busy' && runTest()}><Icon name="bolt" />Test connection</button>
      <button type="submit" form="add-runner" class="btn primary" aria-disabled={saving || undefined}>{saving ? 'Adding…' : 'Add runner'}</button>
    {/snippet}
  </Dialog>
{/if}
