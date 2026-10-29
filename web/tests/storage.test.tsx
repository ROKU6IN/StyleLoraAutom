import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import Storage from '../src/features/Storage';
import { setSessionToken } from '../src/api/client';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';

const preview = {
  preview_id: 'preview',
  digest: 'a'.repeat(64),
  kind: 'thumbnails',
  count: 1,
  bytes: 50,
  hardlinked_files: 0,
  expires_at: '2099-01-01T00:00:00Z',
  skipped: [],
  candidates: [{ path: 'cache/thumbnails/a.webp', bytes: 50, image_id: null }],
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(hasProject = true) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const state = { action: '', preview, lost: false, status: 'done', failed: 0 };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, opts) => {
    if (opts?.method === 'POST') {
      if (state.lost) throw new TypeError('lost');
      state.action = String(url).split('/storage/')[1];
      return Response.json(
        { task: { id: state.action, kind: 'operation' }, status_url: '/api/operations/' + state.action },
        { status: 202 },
      );
    }
    if (String(url).includes('/api/operations/'))
      return Response.json({
        status: state.status,
        error: null,
        result:
          state.action === 'cleanup/preview'
            ? state.preview
            : state.action === 'cleanup'
              ? {
                  status: state.failed ? 'partial' : 'complete',
                  deleted: 1,
                  failed: state.failed,
                  unlinked_bytes: 50,
                  results: state.failed
                    ? [{ path: 'cache/failed.webp', status: 'failed', code: 'cleanup:file_failed' }]
                    : [],
                }
              : null,
      });
    if (String(url).startsWith('/api/projects?')) return Response.json({ items: [], total: 0 });
    return Response.json({ scan: null, stale: false });
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <Storage pid={hasProject ? 'p' : undefined} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { state, fetcher };
}
it('does not scan or delete on opening the page', async () => {
  const { fetcher } = setup();
  await screen.findByText('尚未扫描，没有当前占用数据。');
  expect(fetcher.mock.calls.every(([, opts]) => opts?.method === 'GET')).toBe(true);
});
it('requires an open project for cleanup while allowing the recent project list', () => {
  const { fetcher } = setup(false);
  expect(screen.getByText('请先打开项目')).toBeVisible();
  expect(
    fetcher.mock.calls.every(
      ([url, opts]) =>
        (String(url).startsWith('/api/projects?') || String(url) === '/api/config') && opts?.method === 'GET',
    ),
  ).toBe(true);
  expect(screen.queryByRole('button', { name: '预览候选文件' })).not.toBeInTheDocument();
});
it('scan is explicit and has no arbitrary path input', async () => {
  const { fetcher } = setup();
  fireEvent.click(screen.getByRole('button', { name: '扫描当前项目' }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.some(
        ([url, opts]) => String(url).endsWith('/storage/scans') && opts?.method === 'POST',
      ),
    ).toBe(true),
  );
  expect(
    JSON.parse(fetcher.mock.calls.find(([, opts]) => opts?.method === 'POST')![1]?.body as string),
  ).toEqual({});
});
it('freezes the preview scope and requires checkbox plus hold before cleanup', async () => {
  const { fetcher } = setup();
  fireEvent.click(screen.getByRole('button', { name: '预览候选文件' }));
  const hold = await screen.findByRole('button', { name: /按住清理这些文件/ });
  expect(hold).toBeDisabled();
  expect(screen.getByText('cache/thumbnails/a.webp')).toBeVisible();
  fireEvent.click(screen.getByLabelText('我已核对文件范围并理解清理后果'));
  fireEvent.keyDown(hold, { key: ' ' });
  await screen.findByText('清理完成', {}, { timeout: 2500 });
  fireEvent.keyUp(hold, { key: ' ' });
  const posts = fetcher.mock.calls.filter(([, opts]) => opts?.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(JSON.parse(posts[1][1]?.body as string)).toEqual({
    preview_id: preview.preview_id,
    digest: preview.digest,
    confirm: true,
  });
  expect((posts[1][1]?.headers as Record<string, string>)['Idempotency-Key']).toBeTruthy();
});
it('never silently retries an unknown request and links to task inspection', async () => {
  const { state, fetcher } = setup();
  state.lost = true;
  fireEvent.click(screen.getByRole('button', { name: '预览候选文件' }));
  await screen.findByText(/请求结果未知，已停止新操作/);
  expect(screen.getByRole('button', { name: '扫描当前项目' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '预览候选文件' })).toBeDisabled();
  expect(screen.getByRole('link', { name: '查看任务记录' })).toHaveAttribute('href', '/tasks');
  expect(fetcher.mock.calls.filter(([, opts]) => opts?.method === 'POST')).toHaveLength(1);
});
it('empty or expired previews cannot be confirmed', async () => {
  const { state } = setup();
  state.preview = { ...preview, count: 0, candidates: [] };
  fireEvent.click(screen.getByRole('button', { name: '预览候选文件' }));
  expect(await screen.findByRole('button', { name: /按住清理这些文件/ })).toBeDisabled();
  expect(screen.getByLabelText('我已核对文件范围并理解清理后果')).toBeDisabled();
});
it('all frozen candidates remain reviewable across pages', async () => {
  const { state } = setup();
  state.preview = {
    ...preview,
    count: 51,
    candidates: Array.from({ length: 51 }, (_, i) => ({
      path: `cache/thumbnails/${i}.webp`,
      bytes: 1,
      image_id: null,
    })),
  };
  fireEvent.click(screen.getByRole('button', { name: '预览候选文件' }));
  await screen.findByText('cache/thumbnails/0.webp');
  expect(screen.queryByText('cache/thumbnails/50.webp')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(screen.getByText('cache/thumbnails/50.webp')).toBeVisible();
});
it('in-flight operations disable further actions', async () => {
  const { state } = setup();
  state.status = 'running';
  fireEvent.click(screen.getByRole('button', { name: '扫描当前项目' }));
  await screen.findByText(/后台任务正在处理/);
  expect(screen.getByRole('button', { name: '预览候选文件' })).toBeDisabled();
});
it('closing a cleanup preview never submits deletion', async () => {
  const { fetcher } = setup();
  fireEvent.click(screen.getByRole('button', { name: '预览候选文件' }));
  fireEvent.click(await screen.findByRole('button', { name: '关闭预览' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});
