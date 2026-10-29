import { beforeEach, describe, expect, it } from 'vitest';
import { useSelection } from '../src/state/selection';
import { usePrefs, validPreferences } from '../src/state/prefs';
import { fromAnnotation, manualBody, draftChanged, rebaseDraft } from '../src/features/workbench/draft';
import { isEditingTarget, reviewKey } from '../src/features/workbench/keyboard';
import { queryFromUrl } from '../src/api/endpoints';
import type { AnnotationView } from '../src/api/types';
const annotation = {
  image_id: 1,
  tags: [
    { tag: 'blue hair', conf: null },
    { tag: 'smile', conf: null },
  ],
  natural_language: 'original',
  refs: [{ image_id: 1, source: 'original', type: 'tags', annotation_id: 4 }],
} as AnnotationView;
describe('selection and drafts', () => {
  beforeEach(() => useSelection.getState().reset('project'));
  it('keeps selection independent from focused image and selects only loaded shift range', () => {
    const state = useSelection.getState();
    state.toggle(1);
    state.focusOn(8);
    expect([...(useSelection.getState().selection as { ids: Set<number> }).ids]).toEqual([1]);
    state.range(5, [1, 3, 5, 8]);
    expect([...(useSelection.getState().selection as { ids: Set<number> }).ids]).toEqual([1, 3, 5]);
  });
  it('freezes the all-filter query instead of conflating it with loaded IDs', () => {
    const query = { source_ids: [1] };
    useSelection.getState().all(query, 1200);
    query.source_ids.push(2);
    expect(useSelection.getState().selection).toEqual({
      kind: 'filter',
      query: { source_ids: [1] },
      total: 1200,
    });
    useSelection.getState().reset('next');
    expect(useSelection.getState().focus).toBeNull();
  });
  it('normalizes tags while preserving references and natural-language opt-in', () => {
    const draft = { ...fromAnnotation(annotation), tags: 'blue_hair, smile, smile\ncat' };
    expect(draftChanged(draft, annotation)).toBe(true);
    const body = manualBody(draft, annotation);
    expect(body.tags).toEqual([{ tag: 'blue hair' }, { tag: 'smile' }, { tag: 'cat' }]);
    expect(body.read_set).toEqual(annotation.refs);
    expect(body.write_nl).toBe(false);
  });
  it('rebases only explicit tag additions/removals and retains remote-only edits', () => {
    const draft = { ...fromAnnotation(annotation), tags: 'smile, local' };
    const remote = {
      ...annotation,
      tags: [
        { tag: 'blue hair', conf: null },
        { tag: 'smile', conf: null },
        { tag: 'remote', conf: null },
      ],
      natural_language: 'remote NL',
    };
    expect(rebaseDraft(draft, annotation, remote)).toEqual({
      tags: 'smile, remote, local',
      nl: 'remote NL',
      writeNl: false,
    });
  });
  it('does not mark normalization-only input dirty or overwrite NL implicitly', () => {
    expect(
      draftChanged(
        { ...fromAnnotation(annotation), tags: 'blue_hair, smile, smile', nl: 'typing' },
        annotation,
      ),
    ).toBe(false);
    expect(draftChanged({ ...fromAnnotation(annotation), nl: 'typing', writeNl: true }, annotation)).toBe(
      true,
    );
  });
  it('guards input and nested contenteditable targets from review shortcuts', () => {
    const input = document.createElement('textarea');
    expect(isEditingTarget(input)).toBe(true);
    const editable = document.createElement('div');
    editable.contentEditable = 'true';
    editable.setAttribute('contenteditable', 'true');
    const span = document.createElement('span');
    editable.append(span);
    expect(isEditingTarget(span)).toBe(true);
    expect(reviewKey('K')).toBe('keep');
    expect(reviewKey('s')).toBeUndefined();
  });
  it('sanitizes URL filters and preference storage to the declared fields', () => {
    expect(
      queryFromUrl(new URLSearchParams('src=1,-2,abc&review=keep,evil&rating=general,wrong')),
    ).toMatchObject({ source_ids: [1], review_statuses: ['keep'], rating: ['general'] });
    expect(validPreferences({ token: 'secret', theme: 'invalid', density: 'comfortable' })).toMatchObject({
      theme: 'light',
      density: 'comfortable',
    });
    usePrefs.getState().update({ theme: 'dark' });
    const saved = JSON.parse(localStorage.getItem('stylelora.prefs.v1')!);
    expect(Object.keys(saved.state).sort()).toEqual([
      'autoAdvance',
      'density',
      'shortcuts',
      'stage',
      'theme',
    ]);
  });
});
