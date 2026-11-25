import { get, mediaUrl } from './client';
import type { ScenesPage, ScenesQuery, Tag } from './types';

export const listScenes = (q: ScenesQuery, signal?: AbortSignal) => get<ScenesPage>('api/scenes', { query: { ...q }, signal });
/** Busiest first. Filtering by name is done in the UI. */
export const listTags = (signal?: AbortSignal) => get<Tag[]>('api/tags', { signal });
export const thumbUrl = (sceneId: string): string => mediaUrl(`api/img/${sceneId}`);
export const videoUrl = (sceneId: string): string => mediaUrl(`api/vid/${sceneId}`);
