import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import ProductDefaults, { type ProductDefaultsValue } from '../src/features/ProductDefaults';
import { DraftProvider } from '../src/hooks/drafts';
import { setSessionToken } from '../src/api/client';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function form() {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const state = {
    lost: false,
    conflict: false,
    value: {
      version: 'v0',
      cost_policy: 'ask',
      wd_enabled: false,
      llm_limits: { concurrency: 2, timeout_s: 300 },
      mcp: { discovery_mode: 'default' },
    } as ProductDefaultsValue,
  };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    if (options?.method === 'PATCH') {
      if (state.conflict)
        return Response.json({ error: { code: 'config:revision_conflict' } }, { status: 409 });
      state.value = { ...state.value, ...JSON.parse(options.body as string), version: 'v1' };
      if (state.lost) throw new TypeError('lost');
    }
    return Response.json(state.value);
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          <ProductDefaults />
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { state, writes: () => fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH') };
}
it('reads without writes and requires explicit allow acknowledgement', async () => {
  const { writes } = form();
  const select = await screen.findByLabelText('新来源费用策略');
  expect(writes()).toHaveLength(0);
  fireEvent.change(select, { target: { value: 'allow' } });
  expect(screen.getByRole('button', { name: '保存产品设置' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: /我确认新来源默认允许/ }));
  fireEvent.click(screen.getByRole('button', { name: '保存产品设置' }));
  await waitFor(() => expect(writes()).toHaveLength(1));
  const options = writes()[0][1]!;
  expect(new Headers(options.headers).get('If-Match')).toBe('"v0"');
  expect(JSON.parse(options.body as string)).toMatchObject({ cost_policy: 'allow', confirm_allow: true });
});
it('validates limits and preserves drafts on version conflict', async () => {
  const { state, writes } = form();
  state.conflict = true;
  const input = await screen.findByLabelText('LLM 并发上限（1–8）');
  fireEvent.change(input, { target: { value: '1.5' } });
  expect(screen.getByRole('button', { name: '保存产品设置' })).toBeDisabled();
  fireEvent.change(input, { target: { value: '1' } });
  fireEvent.click(screen.getByRole('button', { name: '保存产品设置' }));
  await waitFor(() => expect(writes()).toHaveLength(1));
  expect(input).toHaveValue(1);
  expect(state.value.llm_limits.concurrency).toBe(2);
});
it('blocks repeat after a lost response until explicit readback', async () => {
  const { state, writes } = form();
  state.lost = true;
  fireEvent.change(await screen.findByLabelText('LLM 请求时限（5–600 秒）'), { target: { value: '15' } });
  fireEvent.click(screen.getByRole('button', { name: '保存产品设置' }));
  await screen.findByText(/保存结果未知/);
  expect(screen.getByRole('button', { name: '保存产品设置' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '重新读取产品设置（放弃草稿）' }));
  await waitFor(() => expect(screen.queryByText(/保存结果未知/)).not.toBeInTheDocument());
  expect(screen.getByLabelText('LLM 请求时限（5–600 秒）')).toHaveValue(15);
  expect(writes()).toHaveLength(1);
});
