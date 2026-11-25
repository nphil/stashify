<script lang="ts">
  /* Mounts the cloud layers the tray opens: the picker, the full-screen browser, the paid confirmation and the cost float. */
  import { ui } from '../../stores/ui.svelte';
  import { focusLayer } from '../../ui/actions';
  import CloudPicker from './CloudPicker.svelte';
  import ConfirmRental from './ConfirmRental.svelte';
  import OfferBrowser from './OfferBrowser.svelte';
  import Receipt from './Receipt.svelte';
</script>

{#if ui.overlay === 'picker'}
  <div class="a-picker pop enter" role="dialog" aria-label="Pick a cloud GPU" inert={ui.browse || undefined} use:focusLayer><CloudPicker /></div>
{/if}
{#if ui.browse}
  <div class="a-browse pop enter-drawer" role="dialog" aria-label="Rent a GPU" use:focusLayer><OfferBrowser job /></div>
{/if}
{#if ui.overlay === 'confirm'}<ConfirmRental />{/if}
<Receipt />
