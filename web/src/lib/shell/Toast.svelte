<script lang="ts">
  import Icon from '../ui/Icon.svelte';
  import { toast } from '../stores/toast.svelte';
</script>

<!-- the live region stays in the page so a screen reader hears each new message -->
<div class="toastwrap" role="status" aria-live="polite">
  {#if toast.message}
    <div class="toast enter"><Icon name="checkCircle" />{toast.message}</div>
  {/if}
</div>
