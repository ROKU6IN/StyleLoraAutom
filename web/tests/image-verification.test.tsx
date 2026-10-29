import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { VerifyImageFile } from '../src/features/workbench/VerifyImageFile';
import { Editor } from '../src/features/workbench/Editor';
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient, projectKey } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';
import type { ImageItem } from '../src/api/types';

const image: ImageItem = {
  image_id: 1,
  source_id: 1,
  filename: '1.png',
  width: 12,
  height: 8,
  file_size: 90,
  review_status: 'reject',
  rating: null,
  file_missing: true,
  has_alpha: null,
  original_url: '/original',
  thumbnail_url: '/thumb',
  version: 'missing-v1',
  annotation_coverage: { original: true, wd: false, llm: false, rule: false, manual: false },
};
const accepted = {
  task: { id: 'op1', kind: 'operation', project_id: 'p1' },
  status_url: '/api/operations/op1',
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(options: { missing?: boolean; editor?: boolean } = {}) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const state = {
    lost: false,
    conflict: false,
    pollError: false,
    status: 'done',
    reason: '',
    outcome: 'rejected',
    posts: 0,
  };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith('/verify-file')) {
      state.posts++;
      if (state.lost) throw new TypeError('lost');
      if (state.conflict)
        return Response.json({ error: { code: 'concurrency:version_conflict' } }, { status: 409 });
      return Response.json(accepted, { status: 202 });
    }
    if (url === accepted.status_url) {
      if (state.pollError) throw new TypeError('lost');
      return Response.json({
        operation_id: 'op1',
        project_id: 'p1',
        kind: 'image.verify_file',
        status: state.status,
        result: {
          image: { ...image, file_missing: false, version: 'available-v2' },
          changed: true,
          verified_bytes: 90,
          as_of: '2026-10-28T05:00:00Z',
        },
        error: state.reason ? { code: state.reason, outcome: state.outcome } : null,
      });
    }
    if (url.includes('/annotations/'))
      return Response.json({
        image_id: 1,
        filename: '1.png',
        tags: [{ tag: 'blue hair' }],
        natural_language: null,
        version: 'annotation-v1',
        refs: [],
        layers: {},
        wd_overlap: {},
        missing_original: false,
      });
    if (url.includes('/images/1')) return Response.json(image);
    return Response.json({ items: [] });
  });
  const value = { ...image, file_missing: options.missing ?? true };
  const tree = () => (
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          {options.editor ? <Editor pid="p1" image={value} /> : <VerifyImageFile pid="p1" image={value} />}
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
  const view = render(tree());
  return {
    state,
    fetcher,
    view,
    tree,
    writes: () => fetcher.mock.calls.filter(([url]) => String(url).endsWith('/verify-file')),
  };
}
it('shows exact managed relative destination and never verifies on mount', () => {
  const { state } = setup();
  expect(screen.getByText('images/1.png')).toBeInTheDocument();
  expect(screen.getByText(/审图决定与标注保持原样/)).toBeInTheDocument();
  expect(state.posts).toBe(0);
});
it('does not show a recovery action for an available image', () => {
  const { fetcher } = setup({ missing: false });
  expect(screen.queryByRole('region', { name: '恢复原图核验' })).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});
it('sends empty body, current version and request key then refreshes availability', async () => {
  const { writes } = setup();
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/原图核验通过/);
  expect(writes()).toHaveLength(1);
  const init = writes()[0][1]!;
  expect(init.method).toBe('POST');
  expect(JSON.parse(init.body as string)).toEqual({});
  expect(new Headers(init.headers).get('If-Match')).toBe('"missing-v1"');
  expect(new Headers(init.headers).get('Idempotency-Key')).toBeTruthy();
  expect(queryClient.getQueryData<ImageItem>(projectKey('p1', 'image', 1))?.file_missing).toBe(false);
});
it('unknown submission blocks new work and finds the same request with the same version and key', async () => {
  const { state, writes } = setup();
  state.lost = true;
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/提交结果未知，已停止新核验/);
  expect(screen.getByRole('button', { name: '核验已放回的原图' })).toBeDisabled();
  state.lost = false;
  fireEvent.click(screen.getByRole('button', { name: '找回原核验任务' }));
  await screen.findByText(/原图核验通过/);
  expect(writes()).toHaveLength(2);
  for (const header of ['If-Match', 'Idempotency-Key'])
    expect(new Headers(writes()[0][1]!.headers).get(header)).toBe(
      new Headers(writes()[1][1]!.headers).get(header),
    );
});
it('unknown request identity survives leaving and reopening the image', async () => {
  const { state, view, tree } = setup();
  state.lost = true;
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/提交结果未知，已停止新核验/);
  view.unmount();
  render(tree());
  expect(screen.getByRole('button', { name: '核验已放回的原图' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '找回原核验任务' })).toBeInTheDocument();
  expect(state.posts).toBe(1);
});
it('failed hashes keep the record missing and show a retryable explicit action', async () => {
  const { state } = setup();
  state.status = 'failed';
  state.reason = 'image:hash_mismatch';
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/原图内容已变化/);
  expect(queryClient.getQueryData(projectKey('p1', 'image', 1))).toBeUndefined();
  expect(screen.getByRole('button', { name: '核验已放回的原图' })).toBeEnabled();
  expect(screen.queryByText(/原图核验通过/)).not.toBeInTheDocument();
});
it('stale image versions are rejected without silently resubmitting', async () => {
  const { state } = setup();
  state.conflict = true;
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/内容已被另一次操作修改/);
  expect(state.posts).toBe(1);
  expect(screen.queryByText(/原图核验通过/)).not.toBeInTheDocument();
});
it('poll errors preserve the accepted operation and never submit again', async () => {
  const { state } = setup();
  state.pollError = true;
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/无法连接本地服务/);
  expect(screen.getByRole('button', { name: '核验已放回的原图' })).toBeDisabled();
  state.pollError = false;
  fireEvent.click(screen.getByRole('button', { name: '查询核验结果' }));
  await screen.findByText(/原图核验通过/);
  expect(state.posts).toBe(1);
});
it('canceled operations never show verified success', async () => {
  const { state } = setup();
  state.status = 'canceled';
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/核验已取消/);
  expect(screen.queryByText(/原图核验通过/)).not.toBeInTheDocument();
});
it('offline and expired sessions cannot start verification', () => {
  const { state, view, tree } = setup();
  for (const connection of ['offline', 'reauth', 'draining'] as const) {
    useSession.getState().setConnection(connection);
    view.rerender(tree());
    expect(screen.getByRole('button', { name: '核验已放回的原图' })).toBeDisabled();
  }
  expect(state.posts).toBe(0);
});
it('verification does not discard an unsaved annotation draft', async () => {
  setup({ editor: true });
  const tags = await screen.findByLabelText('标签 · 1');
  await waitFor(() => expect(tags).toHaveValue('blue hair'));
  fireEvent.change(tags, { target: { value: 'my unsaved tags' } });
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/原图核验通过/);
  expect(tags).toHaveValue('my unsaved tags');
  expect(screen.getByText('草稿未保存')).toBeInTheDocument();
});

it('unknown database commit blocks a new verification until an explicit read reconciles state', async () => {
  const { state, fetcher } = setup();
  state.status = 'failed';
  state.reason = 'storage:commit_unknown';
  state.outcome = 'unknown';
  fireEvent.click(screen.getByRole('button', { name: '核验已放回的原图' }));
  await screen.findByText(/核验提交尚未确认/);
  expect(screen.getByRole('button', { name: '核验已放回的原图' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '核对图片状态' }));
  await waitFor(() => expect(screen.queryByText(/核验提交尚未确认/)).not.toBeInTheDocument());
  expect(
    fetcher.mock.calls.some(([url, init]) => url === '/api/projects/p1/images/1' && init?.method === 'GET'),
  ).toBe(true);
  expect(state.posts).toBe(1);
});
