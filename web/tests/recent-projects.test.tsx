import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import RecentProjects from '../src/features/RecentProjects';
import KnownStorage from '../src/features/KnownStorage';
import { DraftProvider } from '../src/hooks/drafts';
import { setSessionToken } from '../src/api/client';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';

const first = {
  known_id: 'known-a',
  name: '项目甲',
  display_name: 'a.slproj',
  last_opened_at: '2026-10-28',
  project_id: null,
  status: 'available',
  reason: null,
};
const second = { ...first, known_id: 'known-b', name: '项目乙', display_name: 'b.slproj' };
function Location() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function setup(storage = false) {
  setSessionToken('test');
  useSession.getState().setConnection('online');
  const state = { status: 'done', lost: false, unavailable: false, empty: false, pageError: false };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const url = String(input);
    if (options?.method === 'POST') {
      if (state.lost) throw new TypeError('lost response');
      if (url.endsWith('/cancel')) {
        state.status = 'canceled';
        return Response.json({});
      }
      return Response.json(
        {
          task: { id: 'operation', kind: 'operation', project_id: null },
          status_url: '/api/operations/operation',
        },
        { status: 202 },
      );
    }
    if (url.startsWith('/api/projects?')) {
      const row = url.includes('cursor=') ? second : first;
      return Response.json({
        items: state.empty
          ? []
          : [{ ...row, ...(state.unavailable ? { status: 'unavailable', reason: 'recent:missing' } : {}) }],
        total: state.empty ? 0 : 2,
        next_cursor: !state.empty && !url.includes('cursor=') ? 'recent-next' : null,
      });
    }
    if (url.startsWith('/api/operations/'))
      return Response.json({
        status: state.status,
        version: 'op-version',
        completed: 0,
        total: 2,
        result: { project_id: 'fresh-handle' },
        error: null,
      });
    if (url.startsWith('/api/storage/scans/')) {
      if (state.pageError && url.includes('cursor='))
        return Response.json({ error: { code: 'session:expired' } }, { status: 410 });
      return Response.json({
        items: [
          {
            ...(url.includes('cursor=') ? second : first),
            total_bytes: null,
            partial: true,
            reason: 'recent:missing',
            error_count: 1,
            hardlinked_files: null,
          },
        ],
        next_cursor: url.includes('cursor=') ? null : 'scan-next',
        summary: {
          scan_id: 'operation',
          as_of: '2026-10-28',
          project_count: 2,
          scanned_count: 1,
          unavailable_count: 1,
          observed_total_bytes: 1024,
          partial: true,
          exceeds_threshold: false,
        },
      });
    }
    return Response.json(null);
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          {storage ? <KnownStorage /> : <RecentProjects />}
          <Location />
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { state, fetcher, posts: () => fetcher.mock.calls.filter(([, opts]) => opts?.method === 'POST') };
}
it('listing recent projects never opens a project and pages explicitly', async () => {
  const { posts } = setup();
  await screen.findByText('a.slproj');
  expect(posts()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '更多最近项目' }));
  await screen.findByText('b.slproj');
  fireEvent.click(screen.getByRole('button', { name: '刷新最近项目' }));
  await screen.findByText('a.slproj');
  expect(posts()).toHaveLength(0);
});
it('opens using only known_id and navigates with the returned fresh handle', async () => {
  const { posts } = setup();
  fireEvent.click(await screen.findByRole('button', { name: '打开 项目甲' }));
  await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/p/fresh-handle/workbench'));
  expect(posts()).toHaveLength(1);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ known_id: 'known-a' });
});
it('unavailable recent projects cannot be opened', async () => {
  const { state, posts } = setup();
  await screen.findByText('a.slproj');
  state.unavailable = true;
  await queryClient.invalidateQueries({ queryKey: ['recent-projects'] });
  await screen.findByText(/项目目录已移走/);
  expect(screen.getByRole('button', { name: '打开 项目甲' })).toBeDisabled();
  expect(posts()).toHaveLength(0);
});
it('unknown open responses disable replay and expose task inspection', async () => {
  const { state, posts } = setup();
  state.lost = true;
  fireEvent.click(await screen.findByRole('button', { name: '打开 项目甲' }));
  await screen.findByText(/打开请求结果未知/);
  expect(screen.getByRole('button', { name: '打开 项目甲' })).toBeDisabled();
  expect(screen.getByRole('link', { name: '查看打开任务' })).toHaveAttribute('href', '/tasks');
  expect(posts()).toHaveLength(1);
});
it('an in-flight open prevents another submission', async () => {
  const { state, posts } = setup();
  state.status = 'running';
  fireEvent.click(await screen.findByRole('button', { name: '打开 项目甲' }));
  await screen.findByText(/正在打开项目。/);
  expect(screen.getByRole('button', { name: '打开 项目甲' })).toBeDisabled();
  expect(posts()).toHaveLength(1);
});
it('cross-project scans are explicit and accept only the known project set', async () => {
  const { posts } = setup(true);
  const scan = screen.getByRole('button', { name: '扫描最近项目' });
  await waitFor(() => expect(scan).toBeEnabled());
  expect(posts()).toHaveLength(0);
  fireEvent.click(scan);
  await screen.findByText(/已统计 1.00 KiB/);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({});
  expect(screen.queryByRole('button', { name: /清理/ })).not.toBeInTheDocument();
});
it('selected known ids persist across selection pages and are frozen during a scan', async () => {
  const { state, posts } = setup(true);
  state.status = 'running';
  fireEvent.click(screen.getByLabelText('扫描全部最近项目'));
  fireEvent.click(await screen.findByRole('checkbox', { name: /项目甲/ }));
  fireEvent.click(screen.getByRole('button', { name: '更多可选项目' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: /项目乙/ }));
  fireEvent.click(screen.getByRole('button', { name: '扫描最近项目' }));
  await screen.findByText(/正在扫描项目/);
  expect(JSON.parse(posts()[0][1]!.body as string)).toEqual({ known_ids: ['known-a', 'known-b'] });
  expect(screen.getByRole('checkbox', { name: /项目乙/ })).toBeDisabled();
});
it('empty explicit selections cannot be scanned', async () => {
  setup(true);
  fireEvent.click(screen.getByLabelText('扫描全部最近项目'));
  await screen.findByRole('checkbox', { name: /项目甲/ });
  expect(screen.getByRole('button', { name: '扫描最近项目' })).toBeDisabled();
});
it('unavailable sizes are a dash and partial totals are clearly identified', async () => {
  setup(true);
  const scan = screen.getByRole('button', { name: '扫描最近项目' });
  await waitFor(() => expect(scan).toBeEnabled());
  fireEvent.click(scan);
  await screen.findByText('—');
  expect(screen.getByText(/不可访问的项目未按零占用/)).toBeVisible();
  expect(screen.queryByText('0 B')).not.toBeInTheDocument();
});
it('scan result paging and sort changes use distinct cursors without another POST', async () => {
  const { posts, fetcher } = setup(true);
  const scan = screen.getByRole('button', { name: '扫描最近项目' });
  await waitFor(() => expect(scan).toBeEnabled());
  fireEvent.click(scan);
  const more = await screen.findByRole('button', { name: '更多占用结果' });
  await waitFor(() => expect(more).toBeEnabled());
  fireEvent.click(more);
  await screen.findByText('b.slproj');
  fireEvent.change(screen.getByLabelText('项目占用排序'), { target: { value: 'known_id' } });
  await screen.findByText('a.slproj');
  expect(String(fetcher.mock.calls.at(-1)![0])).not.toContain('cursor=');
  expect(posts()).toHaveLength(1);
});
it('cancel uses the operation version and does not publish canceled results', async () => {
  const { state, posts } = setup(true);
  state.status = 'running';
  const scan = screen.getByRole('button', { name: '扫描最近项目' });
  await waitFor(() => expect(scan).toBeEnabled());
  fireEvent.click(scan);
  const cancel = await screen.findByRole('button', { name: '取消跨项目扫描' });
  await waitFor(() => expect(cancel).toBeEnabled());
  fireEvent.click(cancel);
  await screen.findByText('扫描已取消，未发布不完整结果。');
  expect((posts()[1][1]!.headers as Record<string, string>)['If-Match']).toBe('"op-version"');
  expect(screen.queryByText(/已统计/)).not.toBeInTheDocument();
});
it('unknown scan responses never retry automatically', async () => {
  const { state, posts } = setup(true);
  state.lost = true;
  const scan = screen.getByRole('button', { name: '扫描最近项目' });
  await waitFor(() => expect(scan).toBeEnabled());
  fireEvent.click(scan);
  await screen.findByText(/请求结果未知，已停止新扫描/);
  expect(scan).toBeDisabled();
  expect(posts()).toHaveLength(1);
});
it('an expired result page retains recovery controls without automatically rescanning', async () => {
  const { state, posts } = setup(true);
  const scan = screen.getByRole('button', { name: '扫描最近项目' });
  await waitFor(() => expect(scan).toBeEnabled());
  fireEvent.click(scan);
  const more = await screen.findByRole('button', { name: '更多占用结果' });
  await waitFor(() => expect(more).toBeEnabled());
  state.pageError = true;
  fireEvent.click(more);
  await screen.findByText(/快照可能已过期/);
  expect(screen.getByRole('button', { name: '占用结果首页' })).toBeEnabled();
  expect(scan).toBeEnabled();
  expect(posts()).toHaveLength(1);
});
