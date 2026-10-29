import type { ImageQuery, Preview, Tag } from '../../api/types';
import type { components } from '../../api/schema';
export type Scope = { selected_ids: number[] } | { filter: ImageQuery };
export type Rule = components['schemas']['RuleSpec'];
export interface SavedRule {
  id: string;
  name: string;
  rule: Rule;
}
export interface Library {
  version: string;
  rules: SavedRule[];
}
export interface TagHit {
  query: string;
  canonical: string | null;
  translation: string | null;
  category: string | null;
  match: string;
}
export interface CatalogStatus {
  version: string;
  available: boolean;
  tag_count: number;
  alias_count: number;
  translation_count: number;
  coverage: string;
}
export interface BatchPreview extends Preview {
  total: number;
  changed_count: number;
  matched_count?: number;
  wd_fallback_count?: number;
  missing_original_count?: number;
  examples: { image_id: number; before: (string | Tag)[]; after: (string | Tag)[]; reason?: string | null }[];
}
export interface LibraryPreview extends Preview {
  count: number;
  skipped: number;
  mode: string;
  examples: unknown[];
}
export interface LogItem {
  op_id: number;
  affected_count: number;
  status: 'applied' | 'undone';
  version: string;
  created_at: string;
  rule_json: Rule;
}
export const emptyRule = (): Rule => ({ schema_version: 1, condition: null, delete: null, write: null });
export const splitTags = (text: string) => [
  ...new Set(
    text
      .split(/[,\n]/)
      .map((t) => t.trim().toLowerCase().replaceAll('_', ' ').trim())
      .filter(Boolean),
  ),
];
export const displayTags = (tags: (string | Tag)[]) =>
  tags.map((t) => (typeof t === 'string' ? t : t.tag)).join(', ');
