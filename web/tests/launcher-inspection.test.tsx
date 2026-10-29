import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import LauncherInspection from '../src/features/LauncherInspection';
import type { BackendRegistry, LauncherInspectionResult } from '../src/api/types';
import { queryClient } from '../src/state/queryClient';
import { setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';

const ready: LauncherInspectionResult = {
  status: 'ready',
  reason: null,
  generation_enabled: false,
  checked_at: '2026-10-28T01:00:00Z',
  warnings: [],
  settings: {
    model: 'nai-diffusion-4-5-full',
    effort: null,
    model_mode: 'anime',
    sampler: 'k_euler',
    noise_schedule: 'native',
    steps: 28,
    width: 832,
    height: 1216,
    scale: 5,
    cfg_rescale: 0,
    uc_preset: 3,
    quality_toggle: false,
    variety_plus: false,
    decrisp: false,
    smea: false,
    smea_dyn: false,
    locked_by_effort: ['steps'],
    available_models: [{ id: 'nai-diffusion-5', efforts: ['medium', 'high'] }],
  },
  activity: {
    generation: { status: 'idle', progress: 0 },
    queue: { pending: 2, completed: 3, failed: 1, execution: 'paused', session_progress: 50 },
  },
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(overrides: Partial<BackendRegistry> = {}, handler?: typeof fetch) {
  setSessionToken('fixture-only');
  useSession.getState().setConnection('online');
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation(
      handler ??
        (async () => Response.json({ operation_id: 'read-1', status_url: '/api/operations/read-1' })),
    );
  render(
    <QueryClientProvider client={queryClient}>
      <LauncherInspection registry={{ items: [], probe_operation_id: null, ...overrides }} />
    </QueryClientProvider>,
  );
  return fetcher;
}
it('does not contact Launcher on mount and links official release downloads', () => {
  const fetcher = setup();
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: '官方源码' })).toHaveAttribute(
    'href',
    'https://github.com/Aaalice233/Aaalice_NAI_Launcher',
  );
  expect(screen.getByRole('link', { name: 'Windows 便携版' }).getAttribute('href')).toContain(
    'v4.3.1/NAI_Launcher_Windows_4.3.1%2B46_Portable.zip',
  );
});
it('only the explicit button starts a keyed read-only operation', async () => {
  const fetcher = setup();
  fireEvent.click(screen.getByRole('button', { name: '读取 Launcher 状态' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  expect(fetcher.mock.calls[0][0]).toBe('/api/backends/nai_launcher/inspect');
  const options = fetcher.mock.calls[0][1]!;
  expect(options.method).toBe('POST');
  expect(options.body).toBe('{}');
  expect(new Headers(options.headers).get('Idempotency-Key')).toBeTruthy();
});
it('displays snapshots while keeping generation explicitly unavailable', () => {
  setup({ inspection: ready });
  expect(screen.getByText('nai-diffusion-4-5-full')).toBeInTheDocument();
  expect(screen.getByText('等待 2 · 已完成 3 · 失败 1')).toBeInTheDocument();
  expect(screen.getByText(/固定参数：steps/)).toBeInTheDocument();
  expect(screen.getByText(/生成适配仍未启用/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '开始生成' })).not.toBeInTheDocument();
});
it('shows version mismatch without a stale settings snapshot', () => {
  setup({
    inspection: {
      status: 'unavailable',
      reason: 'backend:readonly_version_unreviewed',
      generation_enabled: false,
      checked_at: ready.checked_at,
      warnings: [],
    },
  });
  expect(screen.getByRole('alert')).toHaveTextContent('只读检查仅支持');
  expect(screen.queryByText('nai-diffusion-4-5-full')).not.toBeInTheDocument();
});
it('cancels with the current operation version and prevents duplicate starts', async () => {
  const fetcher = setup({ inspection_operation_id: 'read-1' }, async (_input, options) =>
    Response.json(
      options?.method === 'POST'
        ? {}
        : { operation_id: 'read-1', status: 'running', version: 'read-version' },
    ),
  );
  const button = await screen.findByRole('button', { name: '取消状态读取' });
  await waitFor(() => expect(button).toBeEnabled());
  expect(screen.getByRole('button', { name: '正在读取…' })).toBeDisabled();
  fireEvent.click(button);
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, options]) =>
          url === '/api/operations/read-1/cancel' &&
          new Headers(options?.headers).get('If-Match') === '"read-version"',
      ),
    ).toBe(true),
  );
});
it('keeps prior snapshot visible after cancellation', async () => {
  setup({ inspection: ready, inspection_operation_id: 'read-1' }, async () =>
    Response.json({ operation_id: 'read-1', status: 'canceled' }),
  );
  await screen.findByText('状态读取已取消，保留上次结果。');
  expect(screen.getByText('nai-diffusion-4-5-full')).toBeInTheDocument();
});
it('disables requests when reauthentication is required', async () => {
  const fetcher = setup();
  useSession.getState().setConnection('reauth');
  await waitFor(() => expect(screen.getByRole('button', { name: '读取 Launcher 状态' })).toBeDisabled());
  expect(fetcher).not.toHaveBeenCalled();
});
it('reports request failure without claiming success', async () => {
  setup({}, async () =>
    Response.json({ error: { code: 'backend:readonly_schema_mismatch' } }, { status: 503 }),
  );
  fireEvent.click(screen.getByRole('button', { name: '读取 Launcher 状态' }));
  await screen.findByRole('alert');
  expect(screen.getByRole('alert')).toHaveTextContent('只读接口');
  expect(screen.queryByText(/只读检查成功/)).not.toBeInTheDocument();
});
