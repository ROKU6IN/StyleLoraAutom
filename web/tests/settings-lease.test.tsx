import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import SettingsLease, { type LeaseStatus } from '../src/features/SettingsLease';
import { queryClient } from '../src/state/queryClient';
import { setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';

const idle: LeaseStatus = {
  blocked: false,
  phase: 'idle',
  reason: null,
  inflight: false,
  source_id: null,
  changed_keys: [],
  recovery_adapter_available: false,
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(value: LeaseStatus, failure = false) {
  setSessionToken('fixture-only');
  useSession.getState().setConnection('online');
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation(async () =>
      failure
        ? Response.json({ error: { code: 'profile:journal_unavailable' } }, { status: 503 })
        : Response.json(value),
    );
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsLease />
    </QueryClientProvider>,
  );
  return fetcher;
}
it('reads local idle status without probing or exposing a write action', async () => {
  const fetcher = setup(idle);
  await screen.findByText('没有待处理的设置恢复记录');
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toBe('/api/backends/nai_launcher/settings-lease');
  expect(fetcher.mock.calls[0][1]?.method).toBe('GET');
});
it('shows conflict and in-flight blockers without a force-clear control', async () => {
  setup({
    ...idle,
    blocked: true,
    phase: 'conflict',
    reason: 'profile:restore_conflict',
    inflight: true,
    source_id: 12,
    changed_keys: ['steps'],
  });
  await screen.findByText('设置尚未释放');
  expect(screen.getByText(/保留外部修改/)).toBeInTheDocument();
  expect(screen.getByText(/仍有生成结果未确定/)).toBeInTheDocument();
  expect(screen.getByText(/关联来源编号：12/)).toBeInTheDocument();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
it('shows corrupt storage as blocked and never as idle', async () => {
  setup({ ...idle, blocked: true, phase: 'invalid', reason: 'profile:invalid_journal' });
  await screen.findByText(/恢复记录格式或校验失败/);
  expect(screen.queryByText('没有待处理的设置恢复记录')).not.toBeInTheDocument();
});
it('a failed status request does not claim settings were restored', async () => {
  setup(idle, true);
  await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument(), { timeout: 4500 });
  expect(screen.queryByText('没有待处理的设置恢复记录')).not.toBeInTheDocument();
});
