import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { executorScope } from '../src/features/executors/scope';
import RetryFailed from '../src/features/executors/RetryFailed';
import { FloatingPanel } from '../src/ui';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';
import { JobDetails } from '../src/features/Jobs';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
it('carries all workbench filters with explicit missing state and default reject exclusion', () => {
  const query = {
    source_ids: [4],
    rating: ['sensitive'],
    review_statuses: [],
    q: 'blue',
    file_missing: false,
  };
  const scope = executorScope('all', { kind: 'ids', ids: new Set([1]) }, query);
  expect(scope).toMatchObject({
    filter: {
      source_ids: [4],
      rating: ['sensitive'],
      review_statuses: null,
      q: 'blue',
      file_missing: false,
      include_missing: true,
    },
  });
});
it('select all retains its captured filter independently of the current page and filter', () => {
  const selection = { kind: 'filter' as const, query: { q: 'saved', review_statuses: [] }, total: 800 };
  const scope = executorScope('selected', selection, { q: 'changed' });
  expect(scope).toMatchObject({ filter: { q: 'saved', review_statuses: ['keep', 'unreviewed', 'reject'] } });
  expect(scope).not.toHaveProperty('selected_ids');
  expect(executorScope('selected', { kind: 'ids', ids: new Set([9, 2]) })).toEqual({ selected_ids: [2, 9] });
});
it('keeps the grid interactive in a nonmodal panel and restores focus after Escape', () => {
  function Fixture() {
    const [open, setOpen] = useState(false),
      [count, setCount] = useState(0);
    return (
      <>
        <button onClick={() => setOpen(true)}>打开</button>
        <button onClick={() => setCount(count + 1)}>网格 {count}</button>
        <FloatingPanel open={open} onClose={() => setOpen(false)} title="执行器">
          <input aria-label="阈值" />
        </FloatingPanel>
      </>
    );
  }
  render(<Fixture />);
  const trigger = screen.getByRole('button', { name: '打开' });
  trigger.focus();
  fireEvent.click(trigger);
  expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'false');
  fireEvent.click(screen.getByRole('button', { name: '网格 0' }));
  expect(screen.getByRole('button', { name: '网格 1' })).toBeEnabled();
  fireEvent.keyDown(screen.getByLabelText('阈值'), { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});
function setupRetry(available = true) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const onCreated = vi.fn();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_path, init) => {
    if (init?.method === 'POST') throw new TypeError('fixture unknown');
    return Response.json({
      source_job_id: 7,
      executor: 'llm_vision',
      image_count: 2,
      model: 'original-model',
      preview_digest: 'a'.repeat(64),
      available,
      reason: available ? null : 'job:retry_not_ready',
    });
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <RetryFailed pid="p" id={7} onCreated={onCreated} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { fetcher, onCreated };
}
it('failure retry previews only on request and requires explicit confirmation', async () => {
  const { fetcher } = setupRetry();
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '仅重跑失败项' }));
  expect(await screen.findByText('original-model')).toBeVisible();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true);
  const button = screen.getByRole('button', { name: '确认重跑失败项' });
  fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('本次提交结果未知');
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() => expect(fetcher.mock.calls.filter(([, i]) => i?.method === 'POST')).toHaveLength(2));
  const bodies = fetcher.mock.calls
    .filter(([, i]) => i?.method === 'POST')
    .map(([, i]) => JSON.parse(i!.body as string));
  expect(bodies[0]).toEqual(bodies[1]);
  expect(Object.keys(bodies[0]).sort()).toEqual(['batch_id', 'preview_digest']);
});
it('cannot retry while the original combined batch is unfinished', async () => {
  setupRetry(false);
  fireEvent.click(screen.getByRole('button', { name: '仅重跑失败项' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('原批次尚未结束');
  expect(screen.getByRole('button', { name: '确认重跑失败项' })).toBeDisabled();
});
it('failure count switches to the server failure ledger with independent pagination', async () => {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation(async (path) =>
      Response.json(
        String(path).endsWith('/jobs/7')
          ? {
              job_id: 7,
              executor: 'wd',
              status: 'done',
              total: 2,
              done_count: 1,
              failed_count: 1,
              skipped_count: 0,
              config_snapshot_json: {},
            }
          : { items: [], has_more: false },
      ),
    );
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <JobDetails pid="p" id={7} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole('button', { name: '查看失败图片（1）' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([p]) => String(p).includes('kind=failed'))).toBe(true));
  expect(
    within(screen.getByRole('region', { name: '标注任务 7' })).getByRole('button', { name: '查看全部结果' }),
  ).toHaveAttribute('aria-pressed', 'true');
});
