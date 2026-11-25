import { get, mediaUrl, post } from './client';
import type { Job, JobLog } from './types';

export type JobAction = 'cancel' | 'pause' | 'resume' | 'replace' | 'discard';

/** All jobs, oldest first as the server sends them. The jobs store reverses them. */
export const listJobs = (signal?: AbortSignal) => get<Job[]>('api/jobs', { signal });
export const getJob = (id: string, signal?: AbortSignal) => get<Job>(`api/jobs/${id}`, { signal });
export const jobLog = (id: string, after: number, signal?: AbortSignal) => get<JobLog>(`api/jobs/${id}/log`, { query: { after }, signal });
/** 409 means the job is not in a state that allows this. */
export const jobAction = (id: string, action: JobAction) => post<Job>(`api/jobs/${id}/${action}`);

/** Media the browser loads itself. */
export const jobMedia = {
  live: (id: string) => mediaUrl(`api/jobs/${id}/live.mp4`),
  before: (id: string) => mediaUrl(`api/jobs/${id}/preview/before.jpg`),
  after: (id: string) => mediaUrl(`api/jobs/${id}/preview/after.jpg`),
  segBefore: (id: string, n: number) => mediaUrl(`api/jobs/${id}/seg/${n}/before.mp4`),
  segAfter: (id: string, n: number) => mediaUrl(`api/jobs/${id}/seg/${n}/after.mp4`),
  sample: (id: string) => mediaUrl(`api/jobs/${id}/sample.mp4`),
};
