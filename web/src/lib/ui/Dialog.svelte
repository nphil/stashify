<script lang="ts">
  import type { Snippet } from 'svelte';

  /* A modal question. Render it only while it is open; closing = unmounting. The browser traps focus,
     blocks the page behind it and puts focus back on the button that opened it. */
  let { title, onclose, children, actions }: {
    title: string;
    onclose: () => void;
    children: Snippet;
    /** the buttons; give the safe one `autofocus` */
    actions: Snippet;
  } = $props();

  const uid = $props.id();
  let dlg: HTMLDialogElement;

  $effect(() => {
    if (!dlg.open) dlg.showModal();
    return () => { if (dlg.open) dlg.close(); };
  });
</script>

<dialog class="dlg" bind:this={dlg} aria-labelledby={`${uid}-t`}
  oncancel={(e) => { e.preventDefault(); onclose(); }}
  onclick={(e) => { if (e.target === dlg) onclose(); }}>
  <div class="dh"><h2 class="ttl" id={`${uid}-t`}>{title}</h2></div>
  <div class="db">{@render children()}</div>
  <div class="df">{@render actions()}</div>
</dialog>
