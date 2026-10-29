import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import type { ReactNode } from 'react';
import PromptTaskManagement, { fileSize, TaskResultsExport } from '../src/features/PromptTaskManagement';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
const task = {
  task_id: 'a',
  revision: 7,
  status: 'done',
  ready: false,
  requested: 4,
  storage_bytes: 4096,
  can_cleanup: true,
  cleanup_reason: null,
};
const inventory = {
  items: [
    task,
    {
      ...task,
      task_id: 'running',
      status: 'running',
      can_cleanup: false,
      cleanup_reason: 'prompt:stop_before_cleanup',
    },
  ],
  total: 2,
  storage_bytes: 8192,
  receipt_count: 1,
  receipt_bytes: 900,
  cleanup_limit: 200,
};
const report = {
  task_id: 'a',
  revision: 7,
  preview_id: 'preview',
  digest: 'd'.repeat(64),
  count: 3,
  requested: 4,
  source_ready: false,
  can_export: true,
  bytes: 512,
  excluded: { failed: 1 },
  examples: [],
  lossy_fields: [],
  requires_lossy_ack: false,
};
function wrapper(children: ReactNode) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
}
function mount(component: ReactNode, unknown = false) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  let cleanupCalls = 0;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    const url = String(path);
    if (url.endsWith('/task-storage')) return Response.json(inventory);
    if (url.endsWith('/task-cleanup')) {
      cleanupCalls += 1;
      return unknown && cleanupCalls === 1
        ? Response.json({ error: { code: 'prompt:cleanup_incomplete' } }, { status: 503 })
        : Response.json({ count: 1, task_ids: ['a'] });
    }
    if (url.endsWith('/results-preview')) {
      const format = JSON.parse(init!.body as string).format;
      return Response.json({
        ...report,
        format,
        lossy_fields: format === 'txt' ? ['aspect', 'rating', 'structure'] : [],
        requires_lossy_ack: format === 'txt',
      });
    }
    if (url.endsWith('/results-export'))
      return Response.json({
        filename: 'results.txt',
        media_type: 'text/plain',
        content: 'no humans, forest\n',
      });
    throw new Error('Unexpected request');
  });
  const rendered = render(wrapper(component));
  return { fetcher, rerender: (component: ReactNode) => rendered.rerender(wrapper(component)) };
}
async function hold(name: string) {
  vi.useFakeTimers();
  fireEvent.pointerDown(screen.getByRole('button', { name: new RegExp(name) }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100);
  });
  vi.useRealTimers();
}
async function selectTask() {
  fireEvent.click(await screen.findByRole('checkbox', { name: '清理任务 a' }));
  fireEvent.click(screen.getByRole('button', { name: '清理所选任务（1）' }));
  await screen.findByRole('dialog');
}
it('shows task and receipt sizes separately and protects running tasks', async () => {
  mount(<PromptTaskManagement onCleaned={vi.fn()} />);
  expect(await screen.findByRole('checkbox', { name: '清理任务 running' })).toBeDisabled();
  expect(screen.getByLabelText('提示词任务存储与清理')).toHaveTextContent('快照与结果 8.0 KiB');
  expect(screen.getByLabelText('提示词任务存储与清理')).toHaveTextContent('去重记录 900 B');
  expect(fileSize(1024 * 1024)).toBe('1.0 MiB');
});
it('requires explicit acknowledgement and sends only selected task revisions', async () => {
  const onCleaned = vi.fn();
  const { fetcher } = mount(<PromptTaskManagement onCleaned={onCleaned} />);
  await selectTask();
  expect(screen.getByRole('button', { name: /按住清理任务/ })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: '我已保存需要的结果，确认清理这些任务' }));
  await hold('按住清理任务');
  await waitFor(() => expect(onCleaned).toHaveBeenCalledWith(['a']));
  const call = fetcher.mock.calls.find(([path]) => String(path).endsWith('/task-cleanup'))!;
  expect(JSON.parse(call[1]!.body as string)).toEqual({ tasks: [{ task_id: 'a', expected_revision: 7 }] });
});
it('keeps the same cleanup request after unknown outcome without an automatic retry', async () => {
  const { fetcher } = mount(<PromptTaskManagement onCleaned={vi.fn()} />, true);
  await selectTask();
  fireEvent.click(screen.getByRole('checkbox', { name: '我已保存需要的结果，确认清理这些任务' }));
  await hold('按住清理任务');
  expect(await screen.findByRole('alert')).toHaveTextContent('部分任务可能已清理');
  const cleanup = () => fetcher.mock.calls.filter(([path]) => String(path).endsWith('/task-cleanup'));
  expect(cleanup()).toHaveLength(1);
  await hold('继续核对并清理');
  await screen.findByText('已清理 1 个任务，保留最小去重记录。');
  expect(cleanup()).toHaveLength(2);
  expect(cleanup()[0][1]!.body).toEqual(cleanup()[1][1]!.body);
  expect(new Headers(cleanup()[0][1]!.headers).get('Idempotency-Key')).toEqual(
    new Headers(cleanup()[1][1]!.headers).get('Idempotency-Key'),
  );
});
it('does not silently replace confirmed revisions when a background refresh changes the task', async () => {
  const { fetcher } = mount(<PromptTaskManagement onCleaned={vi.fn()} />);
  await selectTask();
  act(() => {
    queryClient.setQueryData(['prompt', 'storage'], { ...inventory, items: [{ ...task, revision: 8 }] });
  });
  fireEvent.click(screen.getByRole('checkbox', { name: '我已保存需要的结果，确认清理这些任务' }));
  await hold('按住清理任务');
  const call = fetcher.mock.calls.find(([path]) => String(path).endsWith('/task-cleanup'))!;
  expect(JSON.parse(call[1]!.body as string).tasks[0].expected_revision).toBe(7);
});
it('exports partial results only after TXT loss acknowledgement and never requests seeds', async () => {
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  const { fetcher } = mount(<TaskResultsExport task={{ ...task, can_export_results: true }} />);
  fireEvent.change(screen.getByLabelText('结果文件格式'), { target: { value: 'txt' } });
  fireEvent.click(screen.getByRole('button', { name: '预览合格结果导出' }));
  expect(await screen.findByLabelText('结果导出预览')).toHaveTextContent(
    '合格结果 3 条 · 源任务目标 4 条 · 源任务尚未就绪',
  );
  expect(screen.getByRole('button', { name: '下载合格结果文件' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: '确认接受 TXT 导出的信息损失' }));
  fireEvent.click(screen.getByRole('button', { name: '下载合格结果文件' }));
  await waitFor(() =>
    expect(fetcher.mock.calls.some(([path]) => String(path).endsWith('/results-export'))).toBe(true),
  );
  expect(fetcher.mock.calls.some(([path]) => String(path).endsWith('/seeds'))).toBe(false);
});
it('invalidates export preview when the task revision changes and blocks active tasks', async () => {
  const { rerender } = mount(<TaskResultsExport task={{ ...task, can_export_results: true }} />);
  fireEvent.click(screen.getByRole('button', { name: '预览合格结果导出' }));
  await screen.findByLabelText('结果导出预览');
  rerender(<TaskResultsExport task={{ ...task, revision: 8, can_export_results: false }} />);
  expect(screen.queryByLabelText('结果导出预览')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '预览合格结果导出' })).toBeDisabled();
});
