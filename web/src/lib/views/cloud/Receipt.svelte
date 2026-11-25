<script lang="ts">
  /* The cost breakdown, floating above everything so a scrolling list never clips it. One copy, driven by `ui.receipt`.
     It finds the button that opened it, is placed once, and follows it while anything scrolls. */
  import { budget } from '../../stores/budget.svelte';
  import { cloud } from '../../stores/cloud.svelte';
  import { ui } from '../../stores/ui.svelte';
  import Skeleton from '../../ui/Skeleton.svelte';
  import { usd } from '../../util/format';
  import BillRows from './BillRows.svelte';

  const id = $derived(ui.receipt);
  const offer = $derived(id === null ? null : cloud.offerById(id));
  const quote = $derived(offer?.estimate ?? null);
  const waiting = $derived(id !== null && offer === null && (cloud.state === 'idle' || cloud.state === 'loading' || cloud.browseState === 'loading'));
  const shown = $derived(quote !== null || waiting);

  let box = $state<HTMLDivElement>();

  $effect(() => {
    const el = box;
    const want = id;
    void quote; // placed again when the skeleton turns into the real bill
    void waiting;
    if (!el || want === null) return;
    let anchor: HTMLElement | null = null;
    let frame = 0;
    let tries = 0;
    let placed = false;

    /* the button that was pressed or hovered, else the one on a picker tile, else the first */
    const find = (): HTMLElement | null => {
      const all = [...document.querySelectorAll<HTMLElement>(`.tip[data-id="${CSS.escape(want)}"] > .iconbtn`)];
      return all.find((b) => b === document.activeElement) ?? all.find((b) => b.matches(':hover')) ?? all.find((b) => b.closest('.recw') !== null) ?? all[0] ?? null;
    };
    const place = (): void => {
      frame = 0;
      if (!anchor || !anchor.isConnected) anchor = find();
      if (!anchor) {
        // the page may not have drawn it yet; once it was shown, a missing button means it scrolled away or was removed
        if (placed || ++tries > 30) { ui.receipt = null; return; }
        frame = requestAnimationFrame(place);
        return;
      }
      const r = anchor.getBoundingClientRect();
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const left = Math.min(Math.max(8, r.right - w + 8), innerWidth - w - 8);
      let top = r.bottom + 8;
      if (top + h > innerHeight - 8) top = r.top - h - 8;
      if (top < 8) top = 8;
      if (top + h > innerHeight - 8) top = Math.max(8, innerHeight - h - 8);
      el.style.left = `${left}px`;
      el.style.top = `${top}px`;
      el.style.visibility = 'visible';
      placed = true;
    };
    const later = (): void => { if (frame === 0) frame = requestAnimationFrame(place); };
    const away = (e: PointerEvent): void => {
      if (e.target instanceof Element && e.target.closest('.rcfloat, .tip') === null) ui.receipt = null;
    };
    place();
    document.addEventListener('scroll', later, { capture: true, passive: true });
    window.addEventListener('resize', later);
    document.addEventListener('pointerdown', away, true);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener('scroll', later, { capture: true });
      window.removeEventListener('resize', later);
      document.removeEventListener('pointerdown', away, true);
    };
  });
</script>

{#if shown}
  <div class="rcfloat pop enter" role="region" aria-label="Cost breakdown" bind:this={box} style="visibility: hidden">
    {#if offer && quote}
      <BillRows {offer} {quote} />
      <p class="note">Could reach about {usd(quote.max)} if the machine runs slower.{#if budget.caps} Stops by itself at {usd(budget.caps.jobCap)}.{/if}</p>
    {:else}
      <Skeleton rows={5} small />
    {/if}
  </div>
{/if}
