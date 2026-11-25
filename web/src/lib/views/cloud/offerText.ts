/* Words and small rules the picker, the browser, the receipt and the confirm dialog share. */
import type { Offer, OpId, QualityId, RecRole, Tier } from '../../api/types';
import { OPS, QUALITIES } from '../../choices';
import type { IconName } from '../../ui/icons';
import { usd } from '../../util/format';

export const TIER_LABEL: Readonly<Record<Tier, string>> = { dc: 'Data centre', comm: 'Community' };
export const TIER_ICON: Readonly<Record<Tier, IconName>> = { dc: 'building', comm: 'users' };
export const ROLE_ICON: Readonly<Record<RecRole, IconName>> = { value: 'star', fast: 'bolt', cheap: 'coin', reliable: 'shieldCheck' };

/** "2× RTX 4090" */
export const gpuName = (o: Offer): string => `${o.count}× ${o.gpu}`;
/** video memory over all the cards, in GB */
export const vramTotal = (o: Offer): number => Math.round(o.vramGb * o.count);
/** the town without the country: "Prague" from "Prague, CZ" */
export const city = (o: Offer): string => o.location.split(',')[0].trim();
export const fmtN = (n: number): string => Math.round(n).toLocaleString('en-US');
/** what keeps this machine from being chosen (it can't run the job, goes over a limit, or is busy), or null */
export const whyNot = (o: Offer): string | null => (o.compatible ? (o.rented ? 'Rented right now' : null) : (o.incompatibleReason ?? 'Can’t run this job'));
/** "Decensor · High detail" */
export const jobName = (op: OpId, q: QualityId): string => OPS[op].name + (op === 'upscale' ? '' : ` · ${QUALITIES[q].name}`);

/** the host's transfer fee as "$0.005/GB", or null when it charges none */
export function feeText(o: Offer): string | null {
  const hi = Math.max(o.bwDownPerGb, o.bwUpPerGb);
  if (hi <= 0) return null;
  const lo = Math.min(o.bwDownPerGb, o.bwUpPerGb);
  return lo > 0 && lo !== hi ? `${usd(lo, 3)}–${usd(hi, 3)}/GB` : `${usd(hi, 3)}/GB`;
}
