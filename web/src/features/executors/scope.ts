import type { ImageQuery } from '../../api/types';
import type { Selection } from '../../state/selection';

export function executorFilter(query?: ImageQuery, includeRejected = false) {
  return {
    source_ids: query?.source_ids?.length ? query.source_ids : null,
    review_statuses: query?.review_statuses?.length
      ? query.review_statuses
      : includeRejected
        ? ['keep', 'unreviewed', 'reject']
        : null,
    rating: query?.rating?.length ? query.rating : null,
    file_missing: query?.file_missing ?? null,
    q: query?.q || null,
    include_missing: true,
  };
}
export function executorScope(
  mode: string,
  selection: Selection,
  query?: ImageQuery,
  includeRejected = false,
) {
  if (mode !== 'selected') return { filter: executorFilter(query, includeRejected) };
  return selection.kind === 'filter'
    ? { filter: executorFilter(selection.query, true) }
    : { selected_ids: [...selection.ids].sort((a, b) => a - b) };
}
export function filterSummary(query: ImageQuery, includeRejected = false) {
  return [
    query.source_ids?.length ? `来源 ${query.source_ids.join('、')}` : '全部来源',
    query.review_statuses?.length
      ? `审核 ${query.review_statuses.join('、')}`
      : includeRejected
        ? '全部审核状态'
        : '默认排除已剔除',
    query.rating?.length ? `评级 ${query.rating.join('、')}` : '',
    query.q ? `标签包含「${query.q}」` : '',
    query.file_missing === true ? '仅文件缺失' : query.file_missing === false ? '文件未标记缺失' : '',
  ]
    .filter(Boolean)
    .join(' · ');
}
