/* What the tray is set to run. Saved between visits, except the cloud machine (a price is never remembered). */
import type { DenoiseId, DetectId, OpId, QualityId, RtxId, SizeId } from '../api/types';
import { WHERE_CLOUD } from '../api/types';
import { loadJson, saveJson } from '../util/persist';

interface Saved {
  op: OpId; q: QualityId; size: SizeId; where: string | null;
  denoise: DenoiseId; rtx: RtxId; rtxFx: boolean; preview: boolean; detect: DetectId;
}
const KEY = 'stashify.opts';
const DEFAULTS: Saved = { op: 'decensor', q: 'balanced', size: 'keep', where: null, denoise: 'off', rtx: 'off', rtxFx: false, preview: false, detect: 'fast' };

class Options {
  op = $state<OpId>(DEFAULTS.op);
  q = $state<QualityId>(DEFAULTS.q);
  /** transcode output size */
  size = $state<SizeId>(DEFAULTS.size);
  /** a runner url, `'cloud'`, or null = the first online runner */
  where = $state<string | null>(DEFAULTS.where);
  denoise = $state<DenoiseId>(DEFAULTS.denoise);
  rtx = $state<RtxId>(DEFAULTS.rtx);
  /** RTX denoise + deblur */
  rtxFx = $state(DEFAULTS.rtxFx);
  preview = $state(DEFAULTS.preview);
  detect = $state<DetectId>(DEFAULTS.detect);
  /** the rented machine picked in the cloud picker or browser; null = the recommended best-value one */
  offerId = $state<string | null>(null);

  get cloud(): boolean { return this.where === WHERE_CLOUD; }

  /** how many Advanced knobs differ from their defaults */
  get knobCount(): number {
    return Number(this.denoise !== 'off') + Number(this.rtx !== 'off') + Number(this.rtxFx) + Number(this.preview) + Number(this.detect !== 'fast');
  }

  #apply(s: Saved): void {
    this.op = s.op; this.q = s.q; this.size = s.size; this.where = s.where === WHERE_CLOUD ? null : s.where;
    this.denoise = s.denoise; this.rtx = s.rtx; this.rtxFx = s.rtxFx; this.preview = s.preview; this.detect = s.detect;
  }
  /** Back to factory settings, with a few values forced (the mock `?s=` states use this). */
  reset(over: Partial<Saved> = {}): void {
    this.#apply({ ...DEFAULTS, ...over });
    this.where = over.where ?? null;
    this.offerId = null;
  }

  constructor() {
    this.#apply(loadJson<Saved>(KEY, DEFAULTS));
    $effect.root(() => {
      $effect(() => {
        saveJson(KEY, {
          op: this.op, q: this.q, size: this.size, where: this.where === WHERE_CLOUD ? null : this.where,
          denoise: this.denoise, rtx: this.rtx, rtxFx: this.rtxFx, preview: this.preview, detect: this.detect,
        } satisfies Saved);
      });
    });
  }
}

export const options = new Options();
