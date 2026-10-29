import { expect, it, vi } from 'vitest';
import { ReviewQueue, advanceUndoHistory, type UndoEntry } from '../src/features/workbench/reviewQueue';
import type { ImageItem, ReviewResult } from '../src/api/types';
function setup() {
  let image = { image_id: 1, review_status: 'unreviewed', version: 'v1' } as ImageItem;
  const driver = {
    read: vi.fn(async () => image),
    send: vi.fn(
      async () =>
        ({
          items: [
            { image: { ...image, review_status: 'reject', version: 'v2' }, old_review_status: 'unreviewed' },
          ],
        }) as ReviewResult,
    ),
    optimistic: vi.fn(),
    settled: vi.fn((items: ReviewResult['items']) => {
      image = items[0].image;
    }),
    failed: vi.fn(),
    undo: vi.fn(),
    busy: vi.fn(),
  };
  return { driver, queue: new ReviewQueue(driver) };
}
it('coalesces rapid K/X into one CAS write and records server-returned undo versions', async () => {
  vi.useFakeTimers();
  const { driver, queue } = setup();
  queue.enqueue([1], 'keep');
  queue.enqueue([1], 'reject');
  await vi.advanceTimersByTimeAsync(150);
  expect(driver.send).toHaveBeenCalledOnce();
  expect(driver.send).toHaveBeenCalledWith({
    ids: [1],
    targets: { '1': 'reject' },
    expected_versions: { '1': 'v1' },
  });
  expect(driver.undo).toHaveBeenCalledWith({
    targets: { '1': 'unreviewed' },
    expected_versions: { '1': 'v2' },
    before_versions: { '1': 'v1' },
  });
  queue.close();
});
it('continues a local undo chain without accepting an intervening external write', () => {
  const history: UndoEntry[] = [{ targets: { '1': 'unreviewed' }, expected_versions: { '1': 'v2' } }];
  const undone: UndoEntry = {
    targets: { '1': 'keep' },
    expected_versions: { '1': 'v3' },
    before_versions: { '1': 'v2' },
  };
  const result = [
    {
      image: { image_id: 1, version: 'v4', review_status: 'keep' } as ImageItem,
      old_review_status: 'reject' as const,
    },
  ];
  expect(advanceUndoHistory(history, undone, result)[0].expected_versions['1']).toBe('v4');
  expect(
    advanceUndoHistory(history, { ...undone, before_versions: { '1': 'external' } }, result)[0]
      .expected_versions['1'],
  ).toBe('v2');
  expect(history[0].expected_versions['1']).toBe('v2');
});
it('serializes in-flight writes and uses the resulting version for a later intent', async () => {
  vi.useFakeTimers();
  const { driver, queue } = setup();
  let release: (v: ReviewResult) => void = () => {};
  driver.send.mockImplementationOnce(
    () =>
      new Promise<ReviewResult>((resolve) => {
        release = resolve;
      }),
  );
  queue.enqueue([1], 'keep');
  await vi.advanceTimersByTimeAsync(150);
  queue.enqueue([1], 'reject');
  await vi.advanceTimersByTimeAsync(150);
  expect(driver.send).toHaveBeenCalledTimes(1);
  release({
    items: [
      {
        image: { image_id: 1, version: 'v2', review_status: 'keep' } as ImageItem,
        old_review_status: 'unreviewed',
      },
    ],
  });
  await vi.advanceTimersByTimeAsync(1);
  expect(driver.send).toHaveBeenLastCalledWith({
    ids: [1],
    targets: { '1': 'reject' },
    expected_versions: { '1': 'v2' },
  });
  queue.close();
});
it('reports unknown or conflict once without retrying a POST', async () => {
  vi.useFakeTimers();
  const { driver, queue } = setup();
  driver.send.mockRejectedValue(new Error('unknown'));
  queue.enqueue([1], 'keep');
  await vi.advanceTimersByTimeAsync(5000);
  expect(driver.send).toHaveBeenCalledOnce();
  expect(driver.failed).toHaveBeenCalledOnce();
  expect(driver.undo).not.toHaveBeenCalled();
  queue.close();
});
it('flushes accepted local intent when the workbench is unmounted before debounce', async () => {
  vi.useFakeTimers();
  const { driver, queue } = setup();
  queue.enqueue([1], 'keep');
  queue.close();
  await vi.advanceTimersByTimeAsync(1);
  expect(driver.send).toHaveBeenCalledOnce();
});
