import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import GenerationLauncherCheck, { type LauncherCheck } from '../src/features/GenerationLauncherCheck';
import { queryClient } from '../src/state/queryClient';
import { setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';

const empty: LauncherCheck = {
  source_id: 1,
  source_version: 'saved-v1',
  operation_id: null,
  stale: false,
  result: null,
};
const checked: LauncherCheck = {
  ...empty,
  result: {
    source_version: 'saved-v1',
    status: 'checked',
    reason: null,
    checked_at: '2026-10-28T01:00:00Z',
    settings_match: true,
    generation_enabled: false,
    launcher_version: '4.3.1',
    fields: [{ key: 'uc_preset', requested: null, expected: 3, observed: 3, matches: true, locked: false }],
    issues: [],
    warnings: [],
    unverified: ['inherited_references', 'batch_size', 'durable_attempt_outcome'],
    request_sizes: { portrait: { width: 512, height: 768 } },
    default_aspect: 'portrait',
  },
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(
  state: LauncherCheck = empty,
  options: { disabled?: boolean; operation?: Record<string, unknown> } = {},
) {
  setSessionToken('fixture-only');
  useSession.getState().setConnection('online');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (String(input).includes('/api/operations/'))
      return Response.json(options.operation ?? { status: 'done' });
    return Response.json(
      init?.method === 'POST' ? { status_url: '/api/operations/check-1', task: { id: 'check-1' } } : state,
    );
  });
  render(
    <QueryClientProvider client={queryClient}>
      <GenerationLauncherCheck
        pid="project"
        sourceId={1}
        sourceVersion="saved-v1"
        disabled={options.disabled ?? false}
      />
    </QueryClientProvider>,
  );
  return fetcher;
}
it('loads only the local cached report and never starts a read automatically', async () => {
  const fetcher = setup();
  await screen.findByText('尚未预检此来源。');
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  expect(fetcher.mock.calls[0][0]).toBe('/api/projects/project/sources/1/launcher-check');
  expect(fetcher.mock.calls[0][1]?.method).toBe('GET');
});
it('uses the saved original source version and a request key for explicit checks', async () => {
  const fetcher = setup();
  fireEvent.click(screen.getByRole('button', { name: '读取并比较 Launcher 参数' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(true));
  const call = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(call[0]).toBe('/api/projects/project/sources/1/launcher-check');
  expect(call[1]?.body).toBe('{}');
  expect(new Headers(call[1]?.headers).get('If-Match')).toBe('"saved-v1"');
  expect(new Headers(call[1]?.headers).get('Idempotency-Key')).toBeTruthy();
});
it('explains None mapping and does not enable generation on matching fields', async () => {
  setup(checked);
  await screen.findByText('可读取的参数一致，仍需完成生成适配。');
  expect(screen.getByRole('cell', { name: 'None（无预设）' })).toBeInTheDocument();
  expect(screen.getByText('页面继承的参考图是否为空')).toBeInTheDocument();
  expect(screen.getByText('页面批量设置是否确实只生成一张')).toBeInTheDocument();
  expect(screen.getByText(/预检不会锁定来源、授权费用/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /生成图片|更新 Launcher/ })).not.toBeInTheDocument();
});
it('marks an older source report stale without automatically checking again', async () => {
  const fetcher = setup({ ...checked, stale: true });
  await screen.findByText(/来源版本已变化/);
  expect(screen.queryByText('可读取的参数一致，仍需完成生成适配。')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true);
});
it('blocks a check with unsaved changes but allows canceling an existing one', async () => {
  const fetcher = setup(
    { ...empty, operation_id: 'check-1' },
    { disabled: true, operation: { status: 'running', version: 'operation-v1' } },
  );
  const cancel = await screen.findByRole('button', { name: '取消参数预检' });
  await waitFor(() => expect(cancel).toBeEnabled());
  expect(screen.getByRole('button', { name: '正在预检…' })).toBeDisabled();
  fireEvent.click(cancel);
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, init]) =>
          url === '/api/operations/check-1/cancel' &&
          new Headers(init?.headers).get('If-Match') === '"operation-v1"',
      ),
    ).toBe(true),
  );
});
it('shows parameter mismatches and locks without editing them', async () => {
  setup({
    ...checked,
    result: {
      ...checked.result!,
      settings_match: false,
      fields: [{ key: 'steps', requested: 28, expected: 28, observed: 14, matches: false, locked: true }],
      issues: ['launcher_check:negative_locked'],
    },
  });
  await screen.findByText('来源参数与 Launcher 当前设置存在差异。');
  expect(screen.getByRole('cell', { name: '不同（当前模型固定）' })).toBeInTheDocument();
  expect(screen.getByText(/来源含自定义负面词/)).toBeInTheDocument();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
});
it('retains the last report when the operation is canceled', async () => {
  setup({ ...checked, operation_id: 'check-1' }, { operation: { status: 'canceled' } });
  await screen.findByText('参数预检已取消，保留上次结果。');
  expect(screen.getByRole('table')).toBeInTheDocument();
});
it('shows upstream errors without claiming a settings match', async () => {
  setup({
    ...checked,
    result: {
      ...checked.result!,
      status: 'unavailable',
      settings_match: null,
      fields: [],
      reason: 'backend:readonly_version_unreviewed',
    },
  });
  await screen.findByRole('alert');
  expect(screen.getByRole('alert')).toHaveTextContent('只读检查仅支持');
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});
