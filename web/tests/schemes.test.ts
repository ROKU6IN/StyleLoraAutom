import { afterEach, expect, it, vi } from 'vitest';
import { request, setSessionToken } from '../src/api/client';

afterEach(() => {
  vi.restoreAllMocks();
  setSessionToken(null);
});
it('sends both default-scheme versions with an idempotent bodyless DELETE', async () => {
  setSessionToken('test');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
  await expect(
    request('DELETE', '/api/projects/p/import-schemes/1?replacement_default_id=2', undefined, {
      key: 'same-request',
      expectedVersions: { '1': 'old', '2': 'new' },
    }),
  ).resolves.toBeUndefined();
  expect(fetcher.mock.calls[0][1]).toMatchObject({
    method: 'DELETE',
    headers: { 'Idempotency-Key': 'same-request', 'X-Expected-Versions': '{"1":"old","2":"new"}' },
  });
  expect(fetcher.mock.calls[0][1]?.body).toBeUndefined();
});
it('does not retry ambiguous deletion or replace its preconditions', async () => {
  setSessionToken('test');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('offline'));
  await expect(
    request('DELETE', '/api/projects/p/export-schemes/2', undefined, {
      version: 'snapshot',
      key: 'same-request',
    }),
  ).rejects.toMatchObject({ code: 'request:unknown' });
  expect(fetcher).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls[0][1]?.headers).toMatchObject({
    'If-Match': '"snapshot"',
    'Idempotency-Key': 'same-request',
  });
});
