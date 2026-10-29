import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import Jobs from '../src/features/Jobs';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { useSelection } from '../src/state/selection';
import { setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function mount({ available = true, url = '/p/p/jobs' } = {}) {
  queryClient.clear();
  setSessionToken('fixture-token');
  useSession.getState().setConnection('online');
  useSelection.getState().reset('p');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    if (init?.method === 'POST') throw new TypeError('fixture unknown submit');
    const target = String(path);
    let value: unknown = { items: [], has_more: false };
    if (target.includes('capabilities')) value = { available };
    else if (target.includes('tagger/models'))
      value = {
        items: [
          { manifest_id: 'a'.repeat(64), model_id: 'fixture', revision: 'b'.repeat(40), state: 'ready' },
        ],
      };
    else if (target.endsWith('/jobs/7'))
      value = {
        job_id: 7,
        status: 'paused',
        total: 1,
        done_count: 0,
        failed_count: 0,
        skipped_count: 0,
        config_snapshot_json: {},
      };
    return new Response(JSON.stringify(value));
  });
  render(
    <MemoryRouter initialEntries={[url]}>
      <QueryClientProvider client={queryClient}>
        <Jobs pid="p" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return fetcher;
}
async function fill() {
  await screen.findByRole('option', { name: 'fixture · bbbbbbbb' });
  fireEvent.change(screen.getByLabelText('已校验的模型'), { target: { value: 'a'.repeat(64) } });
  fireEvent.change(screen.getByLabelText('通用标签阈值（0–1，必填）'), { target: { value: '0.35' } });
  fireEvent.change(screen.getByLabelText('角色标签阈值（0–1，必填）'), { target: { value: '0.85' } });
}

it('requires an available model and explicit bounded thresholds', async () => {
  const fetcher = mount();
  const start = screen.getByRole('button', { name: '开始标注' });
  expect(start).toBeDisabled();
  await fill();
  expect(start).toBeEnabled();
  fireEvent.change(screen.getByLabelText('角色标签阈值（0–1，必填）'), { target: { value: '1.1' } });
  expect(start).toBeDisabled();
  fireEvent.change(screen.getByLabelText('角色标签阈值（0–1，必填）'), { target: { value: '' } });
  expect(start).toBeDisabled();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true);
});

it('uses a stable batch ID when retrying an ambiguous submission', async () => {
  const fetcher = mount();
  await fill();
  const start = screen.getByRole('button', { name: '开始标注' });
  fireEvent.click(start);
  expect(await screen.findByRole('alert')).toHaveTextContent('本次提交结果未知');
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  await waitFor(() => expect(start).toBeEnabled());
  const requests = fetcher.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([, init]) => JSON.parse(init!.body as string));
  expect(requests).toHaveLength(2);
  expect(requests[0].batch_id).toBe(requests[1].batch_id);
  expect(requests[0].config.backend).toBe('onnx');
  expect(requests[0].mode).toBe('uncovered');
});

it('keeps existing records visible while execution is unavailable', async () => {
  mount({ available: false, url: '/p/p/jobs?job=7' });
  await fill();
  expect(screen.getByRole('button', { name: '开始标注' })).toBeDisabled();
  expect(await screen.findByRole('heading', { name: '任务 #7 · 已暂停' })).toBeVisible();
  fireEvent.click(screen.getByText('冻结的执行配置'));
  expect(screen.getByText('此任务没有当前版本可恢复的执行配置。')).toBeVisible();
});

it('submits the explicit GPU batch limit and keeps CPU at one', async () => {
  const fetcher = mount();
  await fill();
  const batch = screen.getByLabelText('批大小上限');
  expect(batch).toBeDisabled();
  expect(batch).toHaveValue('1');
  fireEvent.change(screen.getByLabelText('设备策略'), { target: { value: 'auto' } });
  expect(batch).toBeEnabled();
  fireEvent.change(batch, { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: '开始标注' }));
  await screen.findByRole('alert');
  let calls = fetcher.mock.calls.filter(([, init]) => init?.method === 'POST');
  expect(JSON.parse(calls[0][1]!.body as string).config).toMatchObject({ provider: 'auto', batch_size: 4 });
  fireEvent.change(screen.getByLabelText('设备策略'), { target: { value: 'cpu' } });
  expect(batch).toHaveValue('1');
  await waitFor(() => expect(screen.getByRole('button', { name: '开始标注' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '开始标注' }));
  await waitFor(() =>
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(2),
  );
  calls = fetcher.mock.calls.filter(([, init]) => init?.method === 'POST');
  expect(JSON.parse(calls[1][1]!.body as string).config).toMatchObject({ provider: 'cpu', batch_size: 1 });
});
