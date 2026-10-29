import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import StorageThresholds, { type StorageConfig } from '../src/features/StorageThresholds';
import StorageNotice from '../src/features/StorageNotice';
import { DraftProvider, useDraftGuard } from '../src/hooks/drafts';
import { setSessionToken } from '../src/api/client';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';

const MIB = 1024 ** 2;
const initial: StorageConfig = {
  revision: 0,
  version: 'v0',
  storage_thresholds: { project_bytes: 20480 * MIB, total_bytes: 102400 * MIB },
  limits: { minimum_bytes: MIB, maximum_bytes: 1024 ** 5, multiple_bytes: MIB },
};
function Leave() {
  const guard = useDraftGuard();
  const [left, setLeft] = useState(false);
  return <button onClick={() => guard.run(() => setLeft(true))}>{left ? '已离开' : '离开设置'}</button>;
}
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function form() {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const state = { config: structuredClone(initial), lost: false, conflict: false, readError: false };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
    if (options?.method === 'PATCH') {
      if (state.conflict)
        return Response.json({ error: { code: 'config:revision_conflict' } }, { status: 409 });
      const body = JSON.parse(options.body as string);
      state.config = {
        ...state.config,
        version: 'v1',
        revision: 1,
        storage_thresholds: body.storage_thresholds,
      };
      if (state.lost) throw new TypeError('lost');
    }
    if (state.readError) return Response.json({ error: { code: 'config:invalid_storage' } }, { status: 422 });
    return Response.json(state.config);
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          <StorageThresholds />
          <Leave />
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return {
    state,
    fetcher,
    writes: () => fetcher.mock.calls.filter(([, options]) => options?.method === 'PATCH'),
  };
}
it('reads saved thresholds without saving or scanning on mount', async () => {
  const { fetcher, writes } = form();
  expect(await screen.findByLabelText('单项目提醒阈值（MiB）')).toHaveValue(20480);
  expect(screen.getByRole('button', { name: '保存提醒设置' })).toBeDisabled();
  expect(writes()).toHaveLength(0);
  expect(fetcher.mock.calls.every(([url]) => url === '/api/config')).toBe(true);
});
it('converts MiB to bytes and saves with the loaded version and request key', async () => {
  const { writes } = form();
  fireEvent.change(await screen.findByLabelText('单项目提醒阈值（MiB）'), { target: { value: '3' } });
  fireEvent.click(screen.getByRole('button', { name: '保存提醒设置' }));
  await screen.findByText(/已保存：单项目 3.00 MiB/);
  expect(writes()).toHaveLength(1);
  const options = writes()[0][1]!;
  expect(JSON.parse(options.body as string)).toEqual({
    storage_thresholds: { project_bytes: 3 * MIB, total_bytes: 102400 * MIB },
  });
  expect(new Headers(options.headers).get('If-Match')).toBe('"v0"');
  expect(new Headers(options.headers).get('Idempotency-Key')).toBeTruthy();
});
it('invalid or fractional MiB cannot be saved', async () => {
  const { writes } = form();
  const input = await screen.findByLabelText('单项目提醒阈值（MiB）');
  for (const value of ['', '0', '-1', '1.5', '1073741825']) {
    fireEvent.change(input, { target: { value } });
    expect(screen.getByRole('button', { name: '保存提醒设置' })).toBeDisabled();
  }
  expect(writes()).toHaveLength(0);
});
it('conflicts preserve typed settings and do not overwrite them on refresh', async () => {
  const { state, writes } = form();
  const input = await screen.findByLabelText('单项目提醒阈值（MiB）');
  fireEvent.change(input, { target: { value: '5' } });
  state.conflict = true;
  state.config = { ...initial, version: 'new', revision: 2 };
  queryClient.setQueryData(['product-config'], state.config);
  fireEvent.click(screen.getByRole('button', { name: '保存提醒设置' }));
  await screen.findByText(/设置已被另一次操作修改/);
  expect(input).toHaveValue(5);
  expect(writes()).toHaveLength(1);
  expect(new Headers(writes()[0][1]!.headers).get('If-Match')).toBe('"v0"');
});
it('unknown save outcomes block replay until an explicit reload reconciles the saved value', async () => {
  const { state, writes } = form();
  state.lost = true;
  fireEvent.change(await screen.findByLabelText('单项目提醒阈值（MiB）'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存提醒设置' }));
  await screen.findByText(/保存结果未知，已停止重复提交/);
  expect(screen.getByRole('button', { name: '保存提醒设置' })).toBeDisabled();
  expect(writes()).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '重新载入已保存值（放弃输入）' }));
  await screen.findByText(/已保存：单项目 2.00 MiB/);
  await waitFor(() => expect(screen.queryByText(/保存结果未知，已停止重复提交/)).not.toBeInTheDocument());
  expect(writes()).toHaveLength(1);
});
it('leaving with a threshold draft requires a choice and cancel preserves it', async () => {
  form();
  const input = await screen.findByLabelText('单项目提醒阈值（MiB）');
  fireEvent.change(input, { target: { value: '8' } });
  fireEvent.click(screen.getByRole('button', { name: '离开设置' }));
  await screen.findByRole('dialog', { name: '有未保存的存储提醒设置' });
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
  expect(input).toHaveValue(8);
  expect(screen.queryByRole('button', { name: '已离开' })).not.toBeInTheDocument();
});
it('read failures preserve existing drafts and disable writing', async () => {
  const { state, writes } = form();
  const input = await screen.findByLabelText('单项目提醒阈值（MiB）');
  fireEvent.change(input, { target: { value: '8' } });
  state.readError = true;
  await queryClient.invalidateQueries({ queryKey: ['product-config'] });
  await screen.findByText(/产品设置文件损坏/);
  expect(input).toHaveValue(8);
  expect(screen.getByRole('button', { name: '保存提醒设置' })).toBeDisabled();
  expect(writes()).toHaveLength(0);
});

const observation = {
  as_of: '2026-10-28T00:00:00Z',
  config_changed: false,
  partial: false,
  exceeds_threshold: true,
  project_threshold_bytes: MIB,
};
const example = {
  current_project: { ...observation, project_id: 'p', total_bytes: 2 * MIB },
  known_projects: {
    ...observation,
    scope: 'selected_known',
    project_count: 2,
    unavailable_count: 0,
    observed_total_bytes: 3 * MIB,
    total_threshold_bytes: 2 * MIB,
    over_threshold_count: 1,
    expired: false,
    history_changed: false,
  },
};
function notice(value: unknown = example, pid = 'p') {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  queryClient.setQueryDefaults(['storage-notices'], { retry: false });
  const state = { value, fail: false };
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation(async () =>
      state.fail
        ? Response.json({ error: { code: 'config:invalid_storage' } }, { status: 503 })
        : Response.json(state.value),
    );
  const result = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <StorageNotice pid={pid} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { state, fetcher, result };
}
it('global notices show historic scope, time, threshold and a storage link using GET only', async () => {
  const { fetcher } = notice();
  await screen.findByRole('status');
  expect(screen.getByText(/选定项目范围（2 个）/)).toBeVisible();
  expect(screen.getByText(/当前项目上次扫描/)).toHaveTextContent('2026-10-28T00:00:00Z');
  expect(screen.getByRole('link', { name: '查看存储与重新扫描' })).toHaveAttribute(
    'href',
    '/settings/storage',
  );
  expect(fetcher.mock.calls.every(([, opts]) => opts?.method === 'GET')).toBe(true);
});
it('no completed scans produces no false zero or healthy message', async () => {
  const { fetcher } = notice({ current_project: null, known_projects: null });
  await waitFor(() => expect(fetcher).toHaveBeenCalled());
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  expect(screen.queryByText(/0 B/)).not.toBeInTheDocument();
});
it('an old project handle never produces a current project warning', async () => {
  notice({ ...example, known_projects: null }, 'new-handle');
  await waitFor(() => expect(queryClient.getQueryData(['storage-notices', 'new-handle'])).toBeTruthy());
  expect(screen.queryByText(/当前项目上次扫描/)).not.toBeInTheDocument();
});
it('incomplete observations are lower bounds and unavailable projects are not zero', async () => {
  notice({
    current_project: null,
    known_projects: { ...example.known_projects, partial: true, unavailable_count: 1 },
  });
  expect(await screen.findByText(/1 个项目不可访问，未按零占用计算/)).toBeVisible();
});
it('threshold changes, expired details and changed history require a new scan', async () => {
  notice({
    current_project: null,
    known_projects: { ...example.known_projects, config_changed: true, expired: true, history_changed: true },
  });
  const text = await screen.findByText(/明细快照已过期/);
  expect(text).toHaveTextContent('阈值已修改');
  expect(text).toHaveTextContent('最近项目记录已变化');
});
it('failure to refresh never presents cached size as a successful fresh check', async () => {
  const { state } = notice();
  await screen.findByText(/当前项目上次扫描/);
  state.fail = true;
  await queryClient.invalidateQueries({ queryKey: ['storage-notices'] });
  await screen.findByText(/暂时无法读取存储提醒/);
  expect(screen.queryByText(/当前项目上次扫描/)).not.toBeInTheDocument();
});
