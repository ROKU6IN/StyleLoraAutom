import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import Models from '../src/features/Models';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
});
function mount({ missingRuntime = false, entries = false, failWrite = false } = {}) {
  queryClient.clear();
  setSessionToken('fixture-token');
  useSession.getState().setConnection('online');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (init?.method === 'POST') {
      if (failWrite) throw new TypeError('disconnected');
      throw new Error('Unexpected install');
    }
    const caps = {
      available: false,
      trusted_manifest_count: entries ? 1 : 0,
      runtime: {
        available: !missingRuntime,
        version: 'test',
        reason: missingRuntime ? 'tagger:runtime_missing' : null,
      },
      device: { provider: 'cpu', total_bytes: null, free_bytes: null, reason: 'tagger:cuda_unavailable' },
    };
    const models = {
      items: entries
        ? [
            {
              manifest_id: 'a'.repeat(64),
              model_id: 'fixture',
              repo_id: 'test/fixture',
              revision: 'b'.repeat(40),
              state: 'missing',
              total_bytes: 123,
              files: [],
              installation_id: null,
              provenance: {
                license: 'apache-2.0',
                reviewed_at: '2026-10-25',
                model_sha256_source: 'publisher_git_lfs',
                csv_sha256_source: 'locally_computed_after_publisher_git_blob_verification',
                csv_git_blob: 'c'.repeat(40),
              },
            },
          ]
        : [],
      has_more: false,
    };
    return new Response(JSON.stringify(String(url).includes('capabilities') ? caps : models));
  });
  render(
    <QueryClientProvider client={queryClient}>
      <Models />
    </QueryClientProvider>,
  );
  return fetcher;
}

it('shows the unconfigured trust registry without starting a download', async () => {
  const fetcher = mount();
  expect(await screen.findByText('尚无可安装的可信模型')).toBeVisible();
  expect(screen.getByText(/安装模型不会自动运行标注/)).toBeVisible();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true);
  expect(screen.queryByRole('button', { name: '下载并安装' })).not.toBeInTheDocument();
});

it('disables installation when the optional runtime is missing', async () => {
  const fetcher = mount({ missingRuntime: true, entries: true });
  expect(await screen.findByRole('button', { name: '下载并安装' })).toBeDisabled();
  expect(await screen.findByText(/未安装 ONNX Runtime/)).toBeVisible();
  fireEvent.click(screen.getByText('文件与校验信息'));
  expect(screen.getByText(/标签表先核验官方固定版本的 Git 文件摘要/)).toBeVisible();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true);
});

it('an ambiguous install submit is not automatically retried', async () => {
  const fetcher = mount({ entries: true, failWrite: true });
  const button = await screen.findByRole('button', { name: '下载并安装' });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('本次提交结果未知');
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
});
