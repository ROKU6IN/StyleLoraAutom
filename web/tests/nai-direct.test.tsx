import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import NaiDirect from '../src/features/NaiDirect';
import { queryClient } from '../src/state/queryClient';
import { setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';

const state = {
  version: 'saved-v1',
  available: true,
  credentials: { has_secret: true, origin: 'stored' },
  account: null,
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(options: { environment?: boolean; fail?: boolean; conflict?: boolean } = {}) {
  setSessionToken('session-fixture');
  useSession.getState().setConnection('online');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (String(input).includes('/api/operations/'))
      return Response.json(
        options.fail
          ? { status: 'failed', error: { code: 'nai:unauthorized' } }
          : {
              status: 'done',
              result: {
                ...state,
                account: {
                  active: true,
                  tier: 3,
                  subscription_anlas: 42,
                  purchased_anlas: 7,
                  checked_at: 'now',
                },
              },
            },
      );
    if (init?.method === 'POST') return Response.json({ status_url: '/api/operations/check' });
    if (init?.method === 'PUT' && options.conflict)
      return Response.json({ error: { code: 'concurrency:version_conflict' } }, { status: 409 });
    return Response.json({
      ...state,
      credentials: {
        has_secret: init?.method !== 'DELETE',
        origin: options.environment ? 'environment' : 'stored',
      },
    });
  });
  render(
    <QueryClientProvider client={queryClient}>
      <NaiDirect />
    </QueryClientProvider>,
  );
  return fetcher;
}
it('only reads local state on mount and clearly labels unknown prices', async () => {
  const fetcher = setup();
  await screen.findByText(/当前状态：凭据可用/);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toBe('/api/nai-direct');
  expect(screen.getByText(/单张费用目前显示为未知/)).toBeInTheDocument();
});
it('saves with a version and clears the password without putting it in cached state', async () => {
  const fetcher = setup();
  await screen.findByText(/当前状态：凭据可用/);
  const field = screen.getByLabelText('Persistent API Token');
  expect(field).toHaveAttribute('type', 'password');
  fireEvent.change(field, { target: { value: 'private-fixture-token' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 Token' }));
  await screen.findByText('Token 已加密保存在本机。');
  const call = fetcher.mock.calls.find(([, init]) => init?.method === 'PUT')!;
  expect(new Headers(call[1]?.headers).get('If-Match')).toBe('"saved-v1"');
  expect(call[1]?.body).toBe(JSON.stringify({ value: 'private-fixture-token' }));
  expect(field).toHaveValue('');
  expect(JSON.stringify(queryClient.getQueryData(['nai-direct']))).not.toContain('private-fixture-token');
});
it('only queries account after an explicit click and does not generate', async () => {
  const fetcher = setup();
  await screen.findByText(/当前状态：凭据可用/);
  fireEvent.click(screen.getByRole('button', { name: '查询账户状态' }));
  await screen.findByText(/订阅 Anlas 42，购买 Anlas 7/);
  const writes = fetcher.mock.calls.filter(([, init]) => init?.method === 'POST');
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe('/api/nai-direct/account/probe');
  expect(new Headers(writes[0][1]?.headers).get('Idempotency-Key')).toBeTruthy();
});
it('displays upstream auth failure without retrying the probe', async () => {
  const fetcher = setup({ fail: true });
  await screen.findByText(/当前状态：凭据可用/);
  fireEvent.click(screen.getByRole('button', { name: '查询账户状态' }));
  await screen.findByText(/NovelAI 拒绝了凭据/);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
});
it('explains environment precedence and deletes only the stored secret', async () => {
  const fetcher = setup({ environment: true });
  await screen.findByText(/优先于本机保存值/);
  fireEvent.click(screen.getByRole('button', { name: '删除保存的 Token' }));
  await screen.findByText('已删除本机保存的 Token。');
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'DELETE')).toHaveLength(1);
  expect(screen.getByRole('button', { name: '删除保存的 Token' })).toBeDisabled();
  expect(screen.getByText(/当前状态：凭据可用/)).toBeInTheDocument();
});
it('keeps unsaved token on conflict without automatically repeating the write', async () => {
  const fetcher = setup({ conflict: true });
  await screen.findByText(/当前状态：凭据可用/);
  fireEvent.change(screen.getByLabelText('Persistent API Token'), { target: { value: 'new-token' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 Token' }));
  await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
  expect(screen.getByLabelText('Persistent API Token')).toHaveValue('new-token');
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(1);
});
