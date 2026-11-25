<script lang="ts">
  /* The price worked out line by line. Shared by the floating breakdown and the paid confirm dialog. */
  import type { Offer, Quote } from '../../api/types';
  import { fmtMin, gbf, usd } from '../../util/format';
  import { feeText } from './offerText';

  let { offer, quote }: { offer: Offer; quote: Quote } = $props();
  const fee = $derived(feeText(offer));
  const gbTotal = $derived(quote.gbUp + quote.gbDown + quote.gbModels);
</script>

<div class="rc">
  <span>Compute<span class="why">{fmtMin(quote.minutes)} × {usd(offer.pricePerHour)}/h</span></span><span class="v">{usd(quote.compute)}</span>
  <span>Data transfer<span class="why">{fee ? `${gbf(gbTotal)} × ${fee}` : `No transfer fee at ${offer.provider}`}</span></span><span class="v">{usd(quote.transfer)}</span>
  {#if gbTotal > 0}
    <div class="sub">
      <span>Upload · your video</span><span>{gbf(quote.gbUp)}{quote.upMin !== undefined ? ` · ${fmtMin(quote.upMin)}` : ''}</span>
      <span>Download · the result</span><span>{gbf(quote.gbDown)}{quote.downMin !== undefined ? ` · ${fmtMin(quote.downMin)}` : ''}</span>
      <span>Machine setup · models</span><span>{gbf(quote.gbModels)}</span>
    </div>
  {/if}
  <span>Storage<span class="why">{quote.diskGb > 0 ? `${quote.diskGb} GB for ` : ''}{fmtMin(quote.minutes)}</span></span><span class="v">{usd(quote.storage)}</span>
  <div class="tot"><span>Estimated total</span><span>{usd(quote.total)}</span></div>
</div>
