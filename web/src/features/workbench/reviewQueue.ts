import type { ImageItem, Review, ReviewResult } from '../../api/types';
export type UndoEntry = {
  targets: Record<string, Review>;
  expected_versions: Record<string, string>;
  before_versions?: Record<string, string>;
};
export function undoEntry(items: ReviewResult['items']): UndoEntry {
  return {
    targets: Object.fromEntries(items.map((i) => [i.image.image_id, i.old_review_status])),
    expected_versions: Object.fromEntries(items.map((i) => [i.image.image_id, i.image.version])),
    before_versions: Object.fromEntries(
      items.filter((i) => i.old_version).map((i) => [i.image.image_id, i.old_version!]),
    ),
  };
}
export function advanceUndoHistory(history: UndoEntry[], undone: UndoEntry, items: ReviewResult['items']) {
  const next = history.map((entry) => ({ ...entry, expected_versions: { ...entry.expected_versions } }));
  for (const item of items) {
    const id = String(item.image.image_id);
    const previous = [...next].reverse().find((entry) => id in entry.expected_versions);
    // Only our own contiguous write chain may advance; an external edit remains a conflict.
    if (previous && previous.expected_versions[id] === undone.before_versions?.[id])
      previous.expected_versions[id] = item.image.version;
  }
  return next;
}
interface Driver {
  read: (id: number) => Promise<ImageItem>;
  send: (body: unknown) => Promise<ReviewResult>;
  optimistic: (id: number, target: Review) => void;
  settled: (items: ReviewResult['items']) => void;
  failed: (ids: number[], error: unknown) => void;
  undo: (entry: UndoEntry) => void;
  busy: (value: boolean) => void;
}
export class ReviewQueue {
  private pending = new Map<number, Review>();
  private running = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private closed = false;
  constructor(private driver: Driver) {}
  enqueue(ids: number[], target: Review) {
    if (this.closed) return;
    for (const id of ids) this.pending.set(id, target);
    this.driver.busy(true);
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.flush();
    }, 150);
  }
  async flush() {
    if (this.running || !this.pending.size) return;
    this.running = true;
    const batch = new Map(this.pending);
    this.pending.clear();
    const ids = [...batch.keys()];
    try {
      const images = await Promise.all(ids.map((id) => this.driver.read(id)));
      const versions = Object.fromEntries(images.map((i) => [String(i.image_id), i.version]));
      for (const [id, target] of batch) this.driver.optimistic(id, target);
      const result = await this.driver.send({
        ids,
        targets: Object.fromEntries(batch),
        expected_versions: versions,
      });
      this.driver.settled(result.items);
      this.driver.undo({ ...undoEntry(result.items), before_versions: versions });
    } catch (error) {
      this.pending.clear();
      this.driver.failed(ids, error);
    } finally {
      this.running = false;
      if (this.pending.size) void this.flush();
      else this.driver.busy(false);
    }
  }
  close() {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    void this.flush();
  }
}
