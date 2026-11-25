import { get, post } from './client';
import type { CloudSettings, CloudStatus, CloudTest } from './types';

export const getCloudSettings = (signal?: AbortSignal) => get<CloudSettings>('api/cloud/settings', { signal });
/** Send only what changed. A secret set to null is removed. 400 -> { error }, 409 -> { code: 'busy' }. */
export const saveCloudSettings = (patch: Record<string, unknown>) => post<CloudSettings>('api/cloud/settings', patch);
export const getCloudStatus = (signal?: AbortSignal) => get<CloudStatus>('api/cloud/status', { signal });
/** Test the key just typed, or the saved one when `key` is empty. `provider` defaults to Vast.ai. */
export const testCloudKey = (key?: string, provider: 'vast' | 'runpod' = 'vast') =>
  post<CloudTest>('api/cloud/test', { ...(provider !== 'vast' && { provider }), ...(key && { [`${provider}_api_key`]: key }) });
