<script lang="ts">
  /* The "how is this priced" button. The breakdown itself floats in Receipt.svelte, so a scrolling list never clips it.
     Hover or keyboard focus shows it and leaving hides it; a click pins it open until it is clicked again or Escape. */
  import Icon from '../../ui/Icon.svelte';
  import { ui } from '../../stores/ui.svelte';

  let { id }: { id: string } = $props();

  /** opened by hover or focus rather than a click */
  let auto = false;

  function show(): void {
    if (ui.receipt !== null) return;
    ui.receipt = id;
    auto = true;
  }
  function hide(): void {
    if (auto && ui.receipt === id) ui.receipt = null;
    auto = false;
  }
  function click(): void {
    if (auto) { auto = false; return; }
    ui.receipt = ui.receipt === id ? null : id;
  }
</script>

<span class="tip" data-id={id}>
  <button type="button" class="iconbtn sm2" aria-label="Cost breakdown" aria-expanded={ui.receipt === id}
    onpointerenter={(e) => { if (e.pointerType === 'mouse') show(); }}
    onpointerleave={(e) => { if (e.pointerType === 'mouse') hide(); }}
    onfocus={(e) => { if (e.currentTarget.matches(':focus-visible')) show(); }}
    onblur={hide}
    onclick={click}><Icon name="info" small /></button>
</span>
