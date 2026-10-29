import type { AnnotationView, ManualInput } from '../../api/types';
export interface Draft {
  tags: string;
  nl: string;
  writeNl: boolean;
}
export const fromAnnotation = (a: AnnotationView): Draft => ({
  tags: a.tags.map((t) => t.tag).join(', '),
  nl: a.natural_language ?? '',
  writeNl: false,
});
export const splitTags = (value: string) => [
  ...new Set(
    value
      .split(/[,\n]/)
      .map((t) => t.trim().replaceAll('_', ' ').toLowerCase())
      .filter(Boolean),
  ),
];
export function draftChanged(draft: Draft, base: AnnotationView) {
  return (
    JSON.stringify(splitTags(draft.tags)) !== JSON.stringify(base.tags.map((t) => t.tag)) ||
    (draft.writeNl && draft.nl !== (base.natural_language ?? ''))
  );
}
export function manualBody(draft: Draft, base: AnnotationView): ManualInput {
  return {
    tags: splitTags(draft.tags).map((tag) => ({ tag })),
    natural_language: draft.nl,
    write_nl: draft.writeNl,
    read_set: base.refs,
  };
}
export function tagDelta(draft: Draft, base: AnnotationView) {
  const before = base.tags.map((t) => t.tag),
    after = splitTags(draft.tags);
  return { add: after.filter((t) => !before.includes(t)), remove: before.filter((t) => !after.includes(t)) };
}
// Rebase is explicit: retain unrelated external edits, then apply this draft's tag delta.
export function rebaseDraft(draft: Draft, base: AnnotationView, fresh: AnnotationView): Draft {
  const delta = tagDelta(draft, base);
  return {
    ...draft,
    tags: [
      ...new Set([...fresh.tags.map((t) => t.tag).filter((t) => !delta.remove.includes(t)), ...delta.add]),
    ].join(', '),
    nl: draft.writeNl ? draft.nl : (fresh.natural_language ?? ''),
  };
}
