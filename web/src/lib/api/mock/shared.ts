/* Numbers more than one mock endpoint needs, so settings, status, offers and quotes always agree.
   Taken from the approved mockup. Handlers may mutate BUDGET (a settings save does). */

/** `?prov=2` = two connected providers (Vast.ai and RunPod); the default is one (Vast.ai) */
export const PROVIDERS: 1 | 2 = new URLSearchParams(location.search).get('prov') === '2' ? 2 : 1;

export const BUDGET = {
  /** prepaid balance at the provider */
  credit: 12.4,
  /** monthly spending limit, and what this month has cost so far */
  month: 10.0,
  spent: 3.15,
  /** most one job may cost (the stop-loss per rental) */
  jobCap: 3.0,
  /** most a machine may cost per hour */
  maxHour: 1.0,
  dcOnly: false,
  /** rentals running right now */
  running: 1,
};
