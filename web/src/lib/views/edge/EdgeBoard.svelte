<script lang="ts">
  import { wakeRunner } from '../../api/runners';
  import { nav } from '../../stores/nav.svelte';
  import { ui } from '../../stores/ui.svelte';
  import { runners } from '../../stores/runners.svelte';
  import { toast } from '../../stores/toast.svelte';
  import Icon from '../../ui/Icon.svelte';
  import PageHead from '../../ui/PageHead.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import Status from '../../ui/Status.svelte';
  import { usd } from '../../util/format';

  /* Mock-only board: every empty, loading and error state side by side, built from the same pieces the real pages use.
     The numbers are the mockup's example run, not live data. */
  const RUN_COST = 2.4;
  const LEFT = 0.6;
  const PRICE_CAP = 0.1;

  async function wake(): Promise<void> {
    const r = runners.list.find((x) => !x.online && x.can_wake);
    if (!r) { toast.show('No sleeping runner to wake'); return; }
    try {
      await wakeRunner(r.url);
      toast.show(`Waking ${r.name}`);
      await runners.refresh();
    } catch (e) {
      toast.show(e instanceof Error ? e.message : 'Could not wake it');
    }
  }

  async function retry(): Promise<void> {
    await runners.refresh();
    toast.show('Refreshed');
  }
</script>

<div class="narrow">
  <PageHead title="States board" sub="Empty, loading and error states">
    {#snippet right()}<span class="badge"><Icon name="info" />Mock-only board</span>{/snippet}
  </PageHead>

  <div class="edge">
    <section class="efr" aria-label="No runner online">
      <h2 class="h">No runner online</h2>
      <div class="banner bad" role="alert"><Icon name="power" /><span>No runner is online. Wake the NAS (about 40 s) or use Cloud.</span></div>
      <div class="opt-list">
        <div class="erow">
          <span class="gt sm bad"><Icon name="monitor" /></span>
          <span class="om"><span class="on">This PC</span><Status state="offline" word="Offline · last seen 2 h ago" /></span>
        </div>
        <div class="erow">
          <span class="gt sm mute"><Icon name="server" /></span>
          <span class="om"><span class="on">NAS</span><Status state="asleep" word="Asleep · wakes in about 40 s" /></span>
          <button type="button" class="btn" onclick={wake}><Icon name="power" />Wake</button>
        </div>
      </div>
    </section>

    <section class="efr" aria-label="Cloud not set up">
      <h2 class="h">Cloud not set up</h2>
      <div class="empty">
        <span class="gt"><Icon name="cloud" /></span>
        <h3 class="h">Cloud is not set up yet</h3>
        <p>Add your Vast.ai key and Jasna licence to rent a GPU for the big jobs.</p>
        <button type="button" class="btn" onclick={() => nav.go('cloud')}><Icon name="key" />Set up Cloud</button>
      </div>
    </section>

    <section class="efr" aria-label="No offers under your price cap">
      <h2 class="h">No offers under your price cap</h2>
      <div class="empty">
        <span class="gt warn"><Icon name="coin" /></span>
        <h3 class="h">No machine fits under your limits</h3>
        <p>Nothing that can run this job costs less than {usd(PRICE_CAP)}/h. Raise the limit, include community hosts, or run it at home.</p>
        <button type="button" class="btn" onclick={() => nav.go('cloud')}><Icon name="sliders" />Change limits</button>
      </div>
    </section>

    <section class="efr" aria-label="Over budget">
      <h2 class="h">Over budget</h2>
      <div class="banner bad" role="alert"><Icon name="coin" /><span>This run (~{usd(RUN_COST)}) is more than what is left this month ({usd(LEFT)}).</span></div>
      <div class="row wrap">
        <button type="button" class="btn" onclick={() => (ui.browse = true)}><Icon name="swap" />Pick a cheaper GPU</button>
        <button type="button" class="btn" onclick={() => nav.go('cloud')}><Icon name="sliders" />Change monthly limit</button>
      </div>
    </section>

    <section class="efr" aria-label="Loading offers" aria-busy="true">
      <h2 class="h">Loading offers</h2>
      <div class="cap muted" role="status">Looking for machines…</div>
      <div class="skels">
        {#each { length: 4 } as _, i (i)}
          <div class="eskel"><Skeleton small width="60%" /><Skeleton small width="90%" /><Skeleton small width="75%" /><Skeleton small width="40%" /></div>
        {/each}
      </div>
    </section>

    <section class="efr" aria-label="Stale or error">
      <h2 class="h">Stale or error</h2>
      <div class="banner" role="status">
        <Icon name="alert" /><span>Couldn’t refresh just now. Showing the list from 9 min ago.</span>
        <button type="button" class="btn right" onclick={retry}><Icon name="refresh" />Retry</button>
      </div>
      <div>
        <div class="qrow">
          <span class="gt sm"><Icon name="building" /></span>
          <div class="col grow"><b class="ell">RTX 5090 · Frankfurt</b><span class="cap muted">Vast.ai · 21 min · as of 9 min ago</span></div>
          <b>~{usd(0.38)}</b>
        </div>
        <div class="qrow">
          <span class="gt sm"><Icon name="building" /></span>
          <div class="col grow"><b class="ell">RTX 4090 · Amsterdam</b><span class="cap muted">Vast.ai · 29 min · as of 9 min ago</span></div>
          <b>~{usd(0.33)}</b>
        </div>
      </div>
    </section>
  </div>
</div>
