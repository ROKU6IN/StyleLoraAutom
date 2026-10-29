import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MediaCache } from '../src/api/media';
import { request, setSessionToken } from '../src/api/client';
beforeEach(() => setSessionToken('test-private-token'));
afterEach(() => {
  setSessionToken(null);
  vi.unstubAllGlobals();
});
it('fetches media with bearer headers, reuses shared requests, and revokes LRU blobs', async () => {
  const fetcher = vi.fn(async () => new Response('image'));
  vi.stubGlobal('fetch', fetcher);
  const cache = new MediaCache(1, 100);
  const first = cache.acquire('/api/a'),
    second = cache.acquire('/api/a');
  expect(await first.ready).toBe(await second.ready);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0]).toEqual([
    '/api/a',
    expect.objectContaining({ headers: { Authorization: 'Bearer test-private-token' }, credentials: 'omit' }),
  ]);
  first.release();
  second.release();
  const third = cache.acquire('/api/b');
  await third.ready;
  expect(URL.revokeObjectURL).toHaveBeenCalled();
  third.release();
  cache.clear();
});
it('caps media fetches at six and cancels unneeded pending requests', async () => {
  let active = 0,
    max = 0;
  const releases: (() => void)[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_url: string, options: RequestInit) =>
        new Promise<Response>((resolve, reject) => {
          active++;
          max = Math.max(max, active);
          releases.push(() => {
            active--;
            resolve(new Response('x'));
          });
          options.signal?.addEventListener('abort', () => {
            active--;
            reject(new DOMException('aborted', 'AbortError'));
          });
        }),
    ),
  );
  const cache = new MediaCache();
  const leases = Array.from({ length: 12 }, (_, i) => cache.acquire('/api/' + i));
  leases.forEach((l) => void l.ready.catch(() => undefined));
  await Promise.resolve();
  expect(max).toBe(6);
  leases.forEach((l) => l.release());
  releases.forEach((r) => r());
  await new Promise((r) => setTimeout(r, 0));
  expect(max).toBe(6);
  cache.clear();
});
it('rejects cleanly when authentication disappears before a queued fetch', async () => {
  setSessionToken(null);
  const cache = new MediaCache();
  const lease = cache.acquire('/api/image');
  await expect(lease.ready).rejects.toMatchObject({ status: 401 });
  lease.release();
  cache.clear();
});
it('does not retry a mutation after a network failure and marks its outcome unknown', async () => {
  const fetcher = vi.fn().mockRejectedValue(new TypeError('disconnected'));
  vi.stubGlobal('fetch', fetcher);
  await expect(request('POST', '/api/projects', {})).rejects.toMatchObject({
    outcome: 'unknown',
    code: 'request:unknown',
  });
  expect(fetcher).toHaveBeenCalledOnce();
});
