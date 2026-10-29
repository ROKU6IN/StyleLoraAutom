import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import GenerationRecovery, { type RecoveryItem } from '../src/features/GenerationRecovery';
import { setSessionToken } from '../src/api/client';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';

const row: RecoveryItem = {
  source_id: 1,
  item_id: 7,
  seq: 1,
  attempt: 1,
  status: 'sent',
  phase: 'needs_reconcile',
  reason: null,
  lease_phase: 'applied',
  busy: false,
  can_reconcile: true,
  can_restore: false,
  version: 'a'.repeat(64),
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(value = row, disabled = false) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const state = { value, outcome: 'done', lost: false };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (options?.method === 'POST') {
      if (state.lost) throw new TypeError('lost response');
      return Response.json(
        {
          task: { id: 'operation', kind: 'operation', project_id: 'p' },
          status_url: '/api/operations/operation',
        },
        { status: 202 },
      );
    }
    if (String(url).includes('/api/operations/'))
      return Response.json({ status: state.outcome, error: null, result: null });
    if (String(url).endsWith('/7')) return Response.json(state.value);
    return Response.json({
      items: [state.value],
      total: 1,
      limit: 20,
      upper_id: 7,
      has_more: false,
      next_cursor: null,
    });
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <GenerationRecovery pid="p" sourceId={1} disabled={disabled} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { fetcher, state };
}
it('loads local evidence without invoking recovery when no adapter is available', async () => {
  const { fetcher } = setup({ ...row, can_reconcile: false, reason: 'generation:adapter_unavailable' });
  expect(await screen.findByRole('button', { name: '核查条目 #7' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '恢复设置 #7' })).toBeDisabled();
  expect(fetcher.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
});
it('sends only the explicit read-only action and follows the accepted operation', async () => {
  const { fetcher } = setup();
  fireEvent.click(await screen.findByRole('button', { name: '核查条目 #7' }));
  await screen.findByText('核查或恢复任务已完成，请核对最新条目状态。');
  const [, options] = fetcher.mock.calls.find(([, value]) => value?.method === 'POST')!;
  expect(JSON.parse(options?.body as string)).toEqual({
    item_id: 7,
    action: 'reconcile',
    confirm_restore: false,
  });
  expect(options?.headers).toMatchObject({ 'If-Match': '"' + row.version + '"' });
  expect((options?.headers as Record<string, string>)['Idempotency-Key']).toBeTruthy();
  expect(fetcher.mock.calls.filter(([, value]) => value?.method === 'POST')).toHaveLength(1);
});
it('requires current evidence, a checkbox and a hold for settings restoration', async () => {
  const value = { ...row, status: 'done', phase: 'needs_restore', can_reconcile: false, can_restore: true };
  const { fetcher } = setup(value);
  fireEvent.click(await screen.findByRole('button', { name: '恢复设置 #7' }));
  const hold = screen.getByRole('button', { name: /按住恢复设置/ });
  expect(hold).toBeDisabled();
  fireEvent.click(screen.getByLabelText('我已核对条目并同意恢复设置'));
  await waitFor(() => expect(hold).toBeEnabled());
  fireEvent.keyDown(hold, { key: ' ' });
  await waitFor(
    () => expect(fetcher.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(true),
    { timeout: 2000 },
  );
  fireEvent.keyUp(hold, { key: ' ' });
  const [, options] = fetcher.mock.calls.find(([, value]) => value?.method === 'POST')!;
  expect(JSON.parse(options?.body as string)).toEqual({
    item_id: 7,
    action: 'restore',
    confirm_restore: true,
  });
});
it('disables a restore confirmation whose evidence changes', async () => {
  const { state } = setup({ ...row, phase: 'needs_restore', can_reconcile: false, can_restore: true });
  fireEvent.click(await screen.findByRole('button', { name: '恢复设置 #7' }));
  fireEvent.click(screen.getByLabelText('我已核对条目并同意恢复设置'));
  await waitFor(() => expect(screen.getByRole('button', { name: /按住恢复设置/ })).toBeEnabled());
  state.value = { ...state.value, version: 'b'.repeat(64) };
  await queryClient.invalidateQueries({ queryKey: ['project', 'p'] });
  await waitFor(() => expect(screen.getByText('恢复证据已变化或不可读取，请关闭后重新核对。')).toBeVisible());
  expect(screen.getByRole('button', { name: /按住恢复设置/ })).toBeDisabled();
});
it('unknown admission disables further submissions and links to task inspection', async () => {
  const { fetcher, state } = setup();
  fireEvent.click(await screen.findByRole('button', { name: '核查条目 #7' }));
  await screen.findByText('核查或恢复任务已完成，请核对最新条目状态。');
  state.lost = true;
  fireEvent.click(await screen.findByRole('button', { name: '核查条目 #7' }));
  await screen.findByText(/提交结果未知，已停止重发/);
  expect(screen.getByRole('button', { name: '核查条目 #7' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '刷新本地证据' }));
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(2);
  expect(screen.queryByText('核查或恢复任务已完成，请核对最新条目状态。')).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: '查看任务进度与结果' })).toHaveAttribute('href', '/tasks');
});
it('a running operation and a dirty draft both prevent another recovery action', async () => {
  setup(row, true);
  expect(await screen.findByRole('button', { name: '核查条目 #7' })).toBeDisabled();
  expect(screen.getByText(/请先保存或处理生成源草稿/)).toBeVisible();
});
it('keeps accepted work visible and disables buttons while the operation runs', async () => {
  const { state } = setup();
  state.outcome = 'running';
  fireEvent.click(await screen.findByRole('button', { name: '核查条目 #7' }));
  expect(await screen.findByText(/恢复任务正在后台处理/)).toBeVisible();
  expect(screen.getByRole('button', { name: '核查条目 #7' })).toBeDisabled();
});
it('filters and looks up records with read-only queries', async () => {
  const { fetcher } = setup();
  await screen.findByRole('button', { name: '核查条目 #7' });
  fireEvent.change(screen.getByLabelText('核查条目状态'), { target: { value: 'sent' } });
  await waitFor(() =>
    expect(fetcher.mock.calls.some(([url]) => String(url).includes('status=sent'))).toBe(true),
  );
  fireEvent.change(screen.getByLabelText('按条目编号查询'), { target: { value: '7' } });
  fireEvent.click(screen.getByRole('button', { name: '查询条目' }));
  await waitFor(() =>
    expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/recovery/7'))).toBe(true),
  );
  expect(fetcher.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
});
