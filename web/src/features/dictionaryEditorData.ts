export type DictionaryKind = 'features' | 'prompts';
export type Rating = 'general' | 'sensitive' | 'questionable' | 'explicit';
export type FeatureDraft = { tag: string; rating?: Rating | null };
export type EntryDraft = {
  tags: string[];
  natural_language?: string | null;
  aspect?: 'landscape' | 'portrait' | 'square' | null;
  rating?: Rating | null;
};
export type DictionaryContent =
  | { kind: 'features'; dimensions: Record<string, FeatureDraft[]> }
  | { kind: 'prompts'; entries: EntryDraft[] };
export const ratingNames = { general: '一般', sensitive: '敏感', questionable: '可疑', explicit: '露骨' };
export const aspectNames = { landscape: '横图', portrait: '竖图', square: '方图' };
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const known = (v: Record<string, unknown>, keys: string[]) =>
  Object.keys(v).every((key) => keys.includes(key));
const rating = (v: unknown) => v == null || (typeof v === 'string' && Object.hasOwn(ratingNames, v));
const feature = (v: unknown): v is FeatureDraft =>
  object(v) && known(v, ['tag', 'rating']) && typeof v.tag === 'string' && rating(v.rating);
const entry = (v: unknown): v is EntryDraft =>
  object(v) &&
  known(v, ['tags', 'natural_language', 'aspect', 'rating']) &&
  Array.isArray(v.tags) &&
  v.tags.every((tag) => typeof tag === 'string') &&
  (v.natural_language == null || typeof v.natural_language === 'string') &&
  (v.aspect == null || (typeof v.aspect === 'string' && Object.hasOwn(aspectNames, v.aspect))) &&
  rating(v.rating);

// Only structurally representable JSON enters the form. Unknown fields and invalid enums
// stay in the original text, rather than being silently dropped by a form round-trip.
export function readDictionaryContent(kind: DictionaryKind, text: string): DictionaryContent | null {
  try {
    const value: unknown = JSON.parse(text);
    if (kind === 'prompts' && Array.isArray(value) && value.every(entry)) return { kind, entries: value };
    if (
      kind === 'features' &&
      object(value) &&
      Object.values(value).every((rows) => Array.isArray(rows) && rows.every(feature))
    )
      return { kind, dimensions: value as Record<string, FeatureDraft[]> };
  } catch {
    /* The raw draft remains available for correction. */
  }
  return null;
}

export const contentText = (value: DictionaryContent) =>
  JSON.stringify(value.kind === 'features' ? value.dimensions : value.entries, null, 2);
export const normalizeDraftTag = (value: string) =>
  value.trim().toLowerCase().replaceAll('_', ' ').replace(/\s+/g, ' ');

export function dictionaryDraftIssues(kind: DictionaryKind, content: string): string[] {
  const parsed = readDictionaryContent(kind, content);
  if (!parsed) return ['词典结构、字段或枚举值不正确，请在 JSON 编辑区修正；原文已保留。'];
  const issues: string[] = [];
  const add = (message: string) => {
    if (issues.length < 20) issues.push(message);
  };
  const tagIssue = (tags: string[]) =>
    !tags.length || tags.length > 100 || tags.some((t) => !t.trim() || t.length > 200);
  if (parsed.kind === 'prompts') {
    if (parsed.entries.length > 10000) add('提示词最多 10000 条。');
    parsed.entries.forEach((row, i) => {
      if (tagIssue(row.tags)) add(`第 ${i + 1} 条：需 1–100 个非空标签，每个最多 200 字符。`);
      if ((row.natural_language?.length ?? 0) > 4096) add(`第 ${i + 1} 条：描述最多 4096 字符。`);
    });
  } else {
    const names = Object.keys(parsed.dimensions);
    if (!names.length || names.length > 20) add('特征词典需 1–20 个维度。');
    if (new Set(names.map((n) => n.trim())).size !== names.length) add('维度名称去除首尾空白后不能重复。');
    let count = 0;
    for (const [name, rows] of Object.entries(parsed.dimensions)) {
      if (!name.trim() || name.length > 64 || [...name].some((c) => c.charCodeAt(0) < 32))
        add('维度名称需 1–64 字符，不能含控制字符。');
      if (!rows.length) add(`维度「${name}」至少需要一个候选标签。`);
      count += rows.length;
      rows.forEach((row, i) => {
        if (tagIssue([row.tag])) add(`维度「${name}」第 ${i + 1} 条：标签需 1–200 字符。`);
      });
    }
    if (count > 10000) add('特征候选合计最多 10000 条。');
  }
  return issues;
}
