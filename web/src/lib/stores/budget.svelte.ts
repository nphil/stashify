/* The owner's cloud limits and what this month has cost. Shared by the header pill, the picker and settings. */
import { adaptStatus } from '../api/cloudAdapter';
import { live } from '../api/events';
import { getCloudSettings, getCloudStatus, saveCloudSettings } from '../api/cloudSettings';
import type { Caps, CloudSettings, CloudStatus } from '../api/types';

class Budget {
  settings = $state.raw<CloudSettings | null>(null);
  status = $state.raw<CloudStatus | null>(null);
  loaded = $state(false);
  error = $state<string | null>(null);

  caps: Caps | null = $derived.by(() => {
    const s = this.settings, st = this.status;
    if (!s || !st) return null;
    return { maxPrice: s.max_price, jobCap: s.job_cap, monthCap: s.month_cap, monthToDate: st.month_to_date, credit: st.credit ?? null, dcOnly: s.datacenter_only };
  });
  /** dollars left this month, or null before the first load */
  left: number | null = $derived(this.caps ? Math.max(0, this.caps.monthCap - this.caps.monthToDate) : null);
  /** a paid provider key is saved */
  connected: boolean = $derived(!!this.settings && (this.settings.vast_api_key.set || !!this.settings.runpod_api_key?.set));

  start(): () => void {
    void this.loadSettings();
    return live<CloudStatus>('cloud', {
      poll: (s) => getCloudStatus(s),
      everyMs: 15_000,
      onData: (st) => { this.status = adaptStatus(st); this.loaded = true; this.error = null; },
      onError: (e) => { this.error = e instanceof Error ? e.message : 'Could not load cloud status'; this.loaded = true; },
    });
  }

  async loadSettings(): Promise<void> {
    try { this.settings = await getCloudSettings(); } catch (e) { this.error = e instanceof Error ? e.message : 'Could not load cloud settings'; }
  }

  /** Save the fields that changed. Throws the server's refusal (ApiError) so the form can show it. */
  async save(patch: Record<string, unknown>): Promise<void> {
    this.settings = await saveCloudSettings(patch);
  }
}

export const budget = new Budget();
