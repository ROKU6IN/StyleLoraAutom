import { create } from 'zustand';
import type { ImageQuery } from '../api/types';
export type Selection =
  | { kind: 'ids'; ids: Set<number> }
  | { kind: 'filter'; query: ImageQuery; total: number };
interface State {
  pid: string | null;
  selection: Selection;
  anchor: number | null;
  focus: number | null;
  reset: (pid: string | null) => void;
  clear: () => void;
  focusOn: (id: number) => void;
  toggle: (id: number) => void;
  range: (id: number, ordered: number[]) => void;
  all: (query: ImageQuery, total: number) => void;
}
export const useSelection = create<State>((set, get) => ({
  pid: null,
  selection: { kind: 'ids', ids: new Set() },
  anchor: null,
  focus: null,
  reset: (pid) => set({ pid, selection: { kind: 'ids', ids: new Set() }, anchor: null, focus: null }),
  clear: () => set({ selection: { kind: 'ids', ids: new Set() }, anchor: null }),
  focusOn: (focus) => set({ focus }),
  toggle: (id) => {
    const old = get().selection;
    const ids = new Set(old.kind === 'ids' ? old.ids : []);
    if (ids.has(id)) ids.delete(id);
    else ids.add(id);
    set({ selection: { kind: 'ids', ids }, anchor: id });
  },
  range: (id, ordered) => {
    const { anchor, selection } = get();
    const start = ordered.indexOf(anchor ?? id),
      end = ordered.indexOf(id);
    const ids = new Set(selection.kind === 'ids' ? selection.ids : []);
    if (start >= 0 && end >= 0)
      ordered.slice(Math.min(start, end), Math.max(start, end) + 1).forEach((i) => ids.add(i));
    set({ selection: { kind: 'ids', ids }, anchor: anchor ?? id });
  },
  all: (query, total) => set({ selection: { kind: 'filter', query: structuredClone(query), total } }),
}));
