import { post } from './client';
import type { Job, SubmitBody } from './types';

/** 202 -> the new job. Paid cloud runs add `offer_id`, `cloud_preset` and `confirm_usd`; 409 `price_changed` means the price or budget moved. */
export const submitJob = (body: SubmitBody) => post<Job>('api/decensor', body);
