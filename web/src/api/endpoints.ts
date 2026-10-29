import { request, waitOperation } from './client';
import type { Accepted, Grant, ImageQuery, ImagePage, Review } from './types';
export const projectPath = (pid: string) => `/api/projects/${pid}`;
export function queryString(query: ImageQuery) {
  const params = new URLSearchParams();
  for (const [key, values] of Object.entries({
    source_id: query.source_ids,
    review_status: query.review_statuses,
    rating: query.rating,
  }))
    values?.forEach((value) => params.append(key, String(value)));
  if (query.file_missing != null) params.set('file_missing', String(query.file_missing));
  if (query.q) params.set('q', query.q);
  return params.toString();
}
export function readImages(
  pid: string,
  query: ImageQuery,
  sort: string,
  cursor?: string,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams(queryString(query));
  params.set('limit', '200');
  params.set('sort', sort);
  if (cursor) params.set('cursor', cursor);
  return request<ImagePage>('GET', `${projectPath(pid)}/images?${params}`, undefined, { signal });
}
export async function pickDirectory(
  purpose: 'project_create' | 'project_open' | 'import_root',
  pid?: string,
) {
  const accepted = await request<Accepted>('POST', '/api/filesystem/grants', {
    purpose,
    ...(pid ? { project_id: pid } : {}),
  });
  return waitOperation<Grant>(accepted);
}
export function queryFromUrl(params: URLSearchParams): ImageQuery {
  const numbers = (params.get('src') ?? '')
    .split(',')
    .filter((value) => /^\d+$/.test(value) && Number(value) > 0)
    .map(Number);
  const reviews = (params.get('review') ?? '')
    .split(',')
    .filter((value) => ['keep', 'reject', 'unreviewed'].includes(value)) as Review[];
  const rating = (params.get('rating') ?? '')
    .split(',')
    .filter((value) => ['general', 'sensitive', 'questionable', 'explicit'].includes(value));
  return {
    source_ids: numbers,
    review_statuses: reviews,
    rating,
    q: params.get('q') ?? '',
    file_missing: params.get('missing') === 'true' ? true : null,
  };
}
