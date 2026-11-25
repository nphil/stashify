import { get, post } from './client';
import type { DiscoveredRunner, GpuMeter, Health, RoutePreview, Runner } from './types';

export const listRunners = (signal?: AbortSignal) => get<Runner[]>('api/runners', { signal });
export const listGpus = (signal?: AbortSignal) => get<GpuMeter[]>('api/gpus', { signal });
export const getHealth = (signal?: AbortSignal) => get<Health>('api/health', { signal });
export const routePreview = (op: string, engine?: string, runner?: string, signal?: AbortSignal) =>
  get<RoutePreview>('api/route-preview', { query: { op, engine, runner }, signal });

export interface RunnerInput { name: string; url: string; token?: string; prefer?: string[]; ops?: string[]; mac?: string }
export const addRunner = (b: RunnerInput) => post<Runner>('api/runners', b);
export const removeRunner = (url: string) => post<{ ok: boolean }>('api/runners/remove', { url });
export const testRunner = (b: { url: string; token?: string; name?: string }) => post<{ ok: boolean; error?: string; ops?: string[]; engines?: Record<string, string> }>('api/runners/test', b);
export const discoverRunners = () => post<DiscoveredRunner[]>('api/runners/discover', {});
/** proposed endpoint: send the wake packet for a runner that `can_wake`; `eta_s` = about how long until it answers */
export const wakeRunner = (url: string) => post<{ ok: boolean; eta_s?: number }>('api/runners/wake', { url });
