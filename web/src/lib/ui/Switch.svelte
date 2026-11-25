<script lang="ts">
  let { checked = $bindable(false), label, disabled = false, onchange }: {
    checked?: boolean;
    /** what the switch controls, read by screen readers */
    label: string;
    disabled?: boolean;
    onchange?: (v: boolean) => void;
  } = $props();

  function flip(): void {
    if (disabled) return;
    checked = !checked;
    onchange?.(checked);
  }
</script>

<button type="button" class="sw" role="switch" aria-checked={checked} aria-label={label} aria-disabled={disabled || undefined} onclick={flip}></button>
