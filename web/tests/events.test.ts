import { expect, it, vi } from 'vitest';
import { SseDecoder, EventCursor, retryDelay, type EventData } from '../src/events/sseClient';
import { CoalescedInvalidation } from '../src/events/invalidate';
it('parses arbitrary byte-decoded SSE chunks, CRLF boundaries, comments and multiline data', () => {
  const parser = new SseDecoder();
  expect(parser.push(': ping\r')).toEqual([]);
  expect(parser.push('\nid: epoch:2\r\nevent: image.updated\r\ndata: {"x":\r')).toEqual([]);
  expect(parser.push('\ndata: 1}\r\n\r\n')).toEqual([
    { id: 'epoch:2', event: 'image.updated', data: '{"x":\n1}', retry: undefined },
  ]);
});
it('bounds multi-line event bodies across separate chunks', () => {
  const parser = new SseDecoder();
  expect(() => {
    for (let i = 0; i < 200; i++) parser.push('data: ' + 'x'.repeat(1024) + '\n');
  }).toThrow('budget');
});
it('accepts filtered-stream sequence gaps and rejects duplicates in one epoch', () => {
  const cursor = new EventCursor();
  const frame = { id: 'e:3', event: 'image.updated', data: '{}' };
  const data = { epoch: 'e', sequence: 3 } as EventData;
  expect(cursor.accept(frame, data)).toBe(true);
  expect(cursor.accept(frame, data)).toBe(false);
  expect(cursor.accept({ ...frame, id: 'e:100' }, { ...data, sequence: 100 })).toBe(true);
  expect(cursor.accept({ ...frame, id: 'new:1' }, { ...data, epoch: 'new', sequence: 1 })).toBe(true);
  cursor.reset();
  expect(cursor.id).toBe('');
});
it('backs off with bounded jitter', () => {
  expect(retryDelay(0, 0)).toBe(800);
  expect(retryDelay(100, 0.5)).toBe(30_000);
});
it('coalesces event bursts, preserves dirty-during-refresh and never overlaps GETs', async () => {
  vi.useFakeTimers();
  let finish: () => void = () => {};
  const refresh = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
  );
  const batch = new CoalescedInvalidation(refresh);
  for (let i = 0; i < 100; i++) batch.request();
  await vi.advanceTimersByTimeAsync(300);
  expect(refresh).toHaveBeenCalledTimes(1);
  batch.request();
  await vi.advanceTimersByTimeAsync(300);
  expect(refresh).toHaveBeenCalledTimes(1);
  finish();
  await vi.advanceTimersByTimeAsync(300);
  expect(refresh).toHaveBeenCalledTimes(2);
  finish();
  batch.close();
  batch.request();
  await vi.advanceTimersByTimeAsync(1000);
  expect(refresh).toHaveBeenCalledTimes(2);
});
