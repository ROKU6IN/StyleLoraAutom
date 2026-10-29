import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import GenerationCosts, { type CostState, type CostSummary } from '../src/features/GenerationCosts';
import { queryClient } from '../src/state/queryClient';
import { setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';

const state: CostState = {
  source_id: 1,
  source_version: 'a'.repeat(64),
  status: 'paused',
  cost_policy: 'ask',
  cycle_id: 'b'.repeat(32),
  acknowledged: false,
  confirmation_required: true,
  can_confirm: true,
  reason: null,
  pending: { item_id: 1, estimated_anlas: 5 },
};
const summary: CostSummary = {
  estimated_anlas: 7,
  observed_estimated_anlas: 7,
  unknown_estimated_anlas: 7,
  rejected_estimated_anlas: 0,
  estimate_complete: true,
  counts: { unknown: 1, rejected: 0 },
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(value = state, estimate = summary, disabled = false) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation(async (url) => Response.json(String(url).endsWith('/cost-state') ? value : estimate));
  render(
    <QueryClientProvider client={queryClient}>
      <GenerationCosts pid="p" sourceId={1} disabled={disabled} onChanged={async () => {}} />
    </QueryClientProvider>,
  );
  return fetcher;
}
it('keeps unknown estimates separate and does not invent balance or actual charges', async () => {
  setup();
  await screen.findByText('累计本地估算：7 Anlas');
  expect(screen.getByText(/结果未知：1 次/)).toBeInTheDocument();
  expect(screen.getByText(/余额、订阅和体力未知/)).toBeInTheDocument();
});
it('missing records show an incomplete total and deny never offers a consent action', async () => {
  setup(
    { ...state, cost_policy: 'deny', confirmation_required: false, can_confirm: false },
    { ...summary, estimated_anlas: null, estimate_complete: false },
  );
  await screen.findByText('累计本地估算：不完整，不能给出总额');
  expect(screen.queryByRole('button', { name: '确认本周期费用并继续' })).not.toBeInTheDocument();
});
it('allow requires a checked hold action and sends PATCH with a stable idempotency key and version', async () => {
  const fetcher = setup({ ...state, confirmation_required: false });
  await screen.findByLabelText('运行费用策略');
  fireEvent.change(screen.getByLabelText('运行费用策略'), { target: { value: 'allow' } });
  fireEvent.click(screen.getByRole('button', { name: '保存费用策略' }));
  const hold = screen.getByRole('button', { name: /按住确认费用操作/ });
  expect(hold).toBeDisabled();
  fireEvent.click(screen.getByLabelText('我理解费用范围并同意'));
  fireEvent.keyDown(hold, { key: ' ' });
  await waitFor(
    () => expect(fetcher.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(true),
    { timeout: 2000 },
  );
  fireEvent.keyUp(hold, { key: ' ' });
  const [, options] = fetcher.mock.calls.find(([, options]) => options?.method === 'PATCH')!;
  expect(options?.headers).toMatchObject({ 'If-Match': '"' + 'a'.repeat(64) + '"' });
  expect((options?.headers as Record<string, string>)['Idempotency-Key']).toBeTruthy();
  expect(JSON.parse(options?.body as string)).toEqual({ cost_policy: 'allow', confirm_allow: true });
});
it('cycle confirmation explains unlimited cycle scope and does not send merely by opening', async () => {
  const fetcher = setup();
  fireEvent.click(await screen.findByRole('button', { name: '确认本周期费用并继续' }));
  expect(screen.getByText(/不是只同意当前这一张，也不是费用上限/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /按住确认费用操作/ })).toBeDisabled();
  expect(fetcher.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
});
it('unavailable production adapter and dirty source both prevent confirmation', async () => {
  setup({ ...state, can_confirm: false, reason: 'generation:adapter_unavailable' }, summary, true);
  expect(await screen.findByRole('button', { name: '确认本周期费用并继续' })).toBeDisabled();
  expect(screen.getByText(/真实生成适配尚未开放/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存费用策略' })).toBeDisabled();
});
