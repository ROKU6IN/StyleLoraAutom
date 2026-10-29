import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import UnknownDisposition, { type UnknownDispositionView } from '../src/features/UnknownDisposition';
import { setSessionToken } from '../src/api/client';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';

const row: UnknownDispositionView = {
  source_id: 1,
  source_status: 'paused',
  unknown_count: 1,
  pending_count: 2,
  unknown_items: [{ item_id: 7, seq: 1, attempt: 1 }],
  items_truncated: false,
  busy: false,
  reason: null,
  can_stop: true,
  decision: null,
  upstream_outcome: 'unknown',
  version: 'a'.repeat(64),
};
const completed: UnknownDispositionView = {
  ...row,
  source_status: 'canceled',
  can_stop: false,
  version: 'b'.repeat(64),
  decision: {
    decision_id: 'decision-1',
    action: 'stop_source_preserve_unknown',
    phase: 'completed',
    unknown_count: 1,
    pending_count: 2,
    created_at: '2026-10-29T00:00:00.000000Z',
  },
};

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(value = row, disabled = false) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const state = { value, lost: false, getFails: false };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    if (options?.method === 'POST') {
      if (state.lost) throw new TypeError('lost');
      state.value = completed;
    } else if (state.getFails)
      return Response.json({ error: { code: 'nai:manual_review_invalid' } }, { status: 409 });
    return Response.json(state.value);
  });
  render(
    <QueryClientProvider client={queryClient}>
      <UnknownDisposition pid="p" sourceId={1} disabled={disabled} />
    </QueryClientProvider>,
  );
  return { state, fetcher };
}
async function confirm() {
  fireEvent.click(await screen.findByRole('button', { name: '核对并结束此来源' }));
  fireEvent.click(screen.getByLabelText('我确认结束此来源，未提交条目将不再运行'));
  fireEvent.click(screen.getByLabelText('我理解未知请求可能已生成或扣费，结束来源不会结清或退款'));
  const button = screen.getByRole('button', { name: /按住结束来源/ });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}
async function hold(button: HTMLElement) {
  fireEvent.keyDown(button, { key: ' ' });
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), { timeout: 2500 });
  fireEvent.keyUp(button, { key: ' ' });
}
it('loads only local evidence and explains unknown charges', async () => {
  const { fetcher } = setup();
  expect(await screen.findByText(/来源 #1：结果待核查 1 项，尚未提交 2 项/)).toBeVisible();
  expect(screen.getByText(/不能证明未生成、未扣费或已退款/)).toBeVisible();
  expect(fetcher.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
});
it('requires two confirmations and a hold, preserving the reviewed version', async () => {
  const { fetcher } = setup();
  fireEvent.click(await screen.findByRole('button', { name: '核对并结束此来源' }));
  const button = screen.getByRole('button', { name: /按住结束来源/ });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByLabelText('我确认结束此来源，未提交条目将不再运行'));
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByLabelText('我理解未知请求可能已生成或扣费，结束来源不会结清或退款'));
  await hold(button);
  expect(await screen.findByText('已保存人工结束决定；上游结果仍未知。')).toBeVisible();
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === 'POST');
  expect(writes).toHaveLength(1);
  expect(JSON.parse(writes[0][1]?.body as string)).toEqual({
    confirm_stop_source: true,
    acknowledge_unknown_charge: true,
  });
  expect(writes[0][1]?.headers).toMatchObject({ 'If-Match': '"' + row.version + '"' });
  expect((writes[0][1]?.headers as Record<string, string>)['Idempotency-Key']).toBeTruthy();
});
it('blocks a confirmation after evidence changes', async () => {
  const { state, fetcher } = setup();
  const button = await confirm();
  state.value = { ...row, version: 'c'.repeat(64) };
  await queryClient.invalidateQueries({ queryKey: ['project', 'p'] });
  await waitFor(() => expect(button).toBeDisabled());
  expect(screen.getByText('处置证据已变化或不可读取，请关闭后重新核对。')).toBeVisible();
  expect(fetcher.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
});
it('blocks unavailable evidence instead of offering a force action', async () => {
  setup({ ...row, reason: 'nai:manual_review_evidence_missing', can_stop: false });
  expect(await screen.findByRole('button', { name: '核对并结束此来源' })).toBeDisabled();
  expect(screen.getByText(/缺少可绑定到该次请求的本地凭据/)).toBeVisible();
});
it('blocks unsaved drafts', async () => {
  setup(row, true);
  expect(await screen.findByRole('button', { name: '核对并结束此来源' })).toBeDisabled();
});
it('blocks offline write actions', async () => {
  setup();
  useSession.getState().setConnection('offline');
  expect(await screen.findByRole('button', { name: '核对并结束此来源' })).toBeDisabled();
});
it('shows a persisted prepared decision without auto completing it', async () => {
  const { fetcher } = setup({ ...row, decision: { ...completed.decision!, phase: 'prepared' } });
  expect(await screen.findByRole('button', { name: '核对并完成结束记录' })).toBeEnabled();
  expect(screen.getByText(/发现未完成的本地结束记录/)).toBeVisible();
  expect(fetcher.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
});
it('lost response blocks repeats until an explicit successful fresh read', async () => {
  const { state, fetcher } = setup();
  const button = await confirm();
  state.lost = true;
  await hold(button);
  expect(await screen.findByText(/保存结果未知，已停止重复提交/)).toBeVisible();
  expect(screen.getByRole('button', { name: '核对并结束此来源' })).toBeDisabled();
  await queryClient.invalidateQueries({ queryKey: ['project', 'p'] });
  expect(screen.getByRole('button', { name: '核对并结束此来源' })).toBeDisabled();
  state.getFails = true;
  fireEvent.click(screen.getByRole('button', { name: '重新读取处置记录' }));
  await screen.findByText(/人工处置记录损坏或无法读取/);
  expect(screen.getByText(/保存结果未知，已停止重复提交/)).toBeVisible();
  state.getFails = false;
  state.value = completed;
  fireEvent.click(screen.getByRole('button', { name: '重新读取处置记录' }));
  await screen.findByText('已保存人工结束决定；上游结果仍未知。');
  expect(screen.queryByText(/保存结果未知，已停止重复提交/)).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});
