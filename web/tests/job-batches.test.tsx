import { afterEach, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import JobBatches, { needsBatchConfirmation } from '../src/features/JobBatches';
import type { JobPreviewView } from '../src/api/types';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { useSelection } from '../src/state/selection';
import { setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function estimate(count = 3): JobPreviewView {
  return {
    scope_digest: 'a'.repeat(64),
    image_count: count,
    overlapping_jobs: [],
    items: ['wd', 'llm_vision'].map((executor) => ({
      executor,
      total: count,
      cache_hit: 0,
      needs_execution: executor === 'wd' ? count : 0,
      waiting_for_wd: executor === 'wd' ? 0 : count,
      file_missing: 0,
      role_empty: 0,
      rating_unresolved: 0,
    })),
  };
}
function mount(value = estimate(), stale = false, component = <JobBatches pid="p" />) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  useSelection.getState().reset('p');
  let previews = 0;
  let submitted = false;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    const url = String(path);
    if (url.endsWith('/jobs/preview')) {
      previews++;
      return Response.json({ ...value, scope_digest: (previews > 1 && stale ? 'b' : 'a').repeat(64) });
    }
    if (init?.method === 'POST') {
      if (stale && !submitted) {
        submitted = true;
        return Response.json({ error: { code: 'job:preview_stale' } }, { status: 409 });
      }
      throw new TypeError('unknown fixture submit');
    }
    if (url.includes('/tagger/models'))
      return Response.json({
        items: [
          {
            manifest_id: 'fixture',
            model_id: 'synthetic-tagger',
            revision: 'a'.repeat(40),
            state: 'ready',
          },
        ],
      });
    if (url.includes('/executors/capabilities'))
      return Response.json({ wd: { available: true, default_enabled: true }, llm_vision: { available: true } });
    if (url.includes('/tagger/capabilities')) return Response.json({ available: true });
    return Response.json({ items: [], has_more: false });
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>{component}</QueryClientProvider>
    </MemoryRouter>,
  );
  return fetcher;
}
async function configure() {
  await screen.findByRole('option', { name: 'synthetic-tagger · aaaaaaaa' });
  fireEvent.change(screen.getByLabelText('批次 WD 模型'), { target: { value: 'fixture' } });
  fireEvent.change(screen.getByLabelText('批次通用标签阈值'), { target: { value: '0' } });
  fireEvent.change(screen.getByLabelText('批次角色标签阈值'), { target: { value: '0.2' } });
  await waitFor(() => expect(screen.getByRole('button', { name: '开始标注批次' })).toBeEnabled());
}
function submissions(fetcher: ReturnType<typeof mount>) {
  return fetcher.mock.calls
    .filter(([path, init]) => String(path).endsWith('/jobs/batches') && init?.method === 'POST')
    .map(([, init]) => JSON.parse(init!.body as string));
}
it('requires confirmation at batch boundaries including unresolved downstream work and overlaps', () => {
  expect(needsBatchConfirmation(estimate(50))).toBe(false);
  expect(needsBatchConfirmation(estimate(51))).toBe(true);
  const wdOnly = estimate(500);
  wdOnly.items = [wdOnly.items[0]];
  expect(needsBatchConfirmation(wdOnly)).toBe(false);
  wdOnly.image_count = 501;
  expect(needsBatchConfirmation(wdOnly)).toBe(true);
  const overlap = estimate(1);
  overlap.overlapping_jobs.push({ job_id: 1, batch_id: 'old', executor: 'wd', status: 'paused' });
  expect(needsBatchConfirmation(overlap)).toBe(true);
});
it('only previews on configuration and preserves batch identity after an unknown submission', async () => {
  const fetcher = mount();
  expect(screen.getByRole('button', { name: '开始标注批次' })).toBeDisabled();
  await configure();
  expect(submissions(fetcher)).toEqual([]);
  const start = screen.getByRole('button', { name: '开始标注批次' });
  fireEvent.click(start);
  expect(await screen.findByRole('alert')).toHaveTextContent('本次提交结果未知');
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  await waitFor(() => expect(submissions(fetcher)).toHaveLength(2));
  const bodies = submissions(fetcher);
  expect(bodies[0].batch_id).toBe(bodies[1].batch_id);
  expect(bodies[0]).toMatchObject({
    executors: ['wd', 'llm_vision'],
    mode: 'uncovered',
    preview_scope_digest: 'a'.repeat(64),
    config: {
      wd: {
        manifest_id: 'fixture',
        general_threshold: 0,
        character_threshold: 0.2,
        provider: 'cpu',
        batch_size: 1,
      },
      llm_vision: { dataset_mode_policy: 'style', use_rating_model: false, fallback: 'normal' },
    },
  });
});
it('all mode requires the hold confirmation and a click alone cannot start work', async () => {
  const fetcher = mount();
  await configure();
  fireEvent.change(screen.getByLabelText('批次范围'), { target: { value: 'all' } });
  await waitFor(() => expect(screen.getByRole('button', { name: '开始标注批次' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '开始标注批次' }));
  const modal = await screen.findByRole('dialog');
  fireEvent.click(within(modal).getByRole('button', { name: /按住执行全部/ }));
  expect(submissions(fetcher)).toEqual([]);
  fireEvent.click(within(modal).getByRole('button', { name: '返回检查' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('large batches wait for explicit confirmation before creating jobs', async () => {
  const fetcher = mount(estimate(51));
  await configure();
  fireEvent.click(screen.getByRole('button', { name: '开始标注批次' }));
  const modal = await screen.findByRole('dialog');
  expect(submissions(fetcher)).toEqual([]);
  fireEvent.click(within(modal).getByRole('button', { name: '确认启动批次' }));
  await waitFor(() => expect(submissions(fetcher)).toHaveLength(1));
});
it('refreshes a stale scope and requires another user action before resubmitting', async () => {
  const fetcher = mount(estimate(), true);
  await configure();
  const start = screen.getByRole('button', { name: '开始标注批次' });
  fireEvent.click(start);
  expect(await screen.findByRole('alert')).toHaveTextContent('范围');
  await waitFor(() => expect(start).toBeEnabled());
  expect(submissions(fetcher)).toHaveLength(1);
  fireEvent.click(start);
  await waitFor(() => expect(submissions(fetcher)).toHaveLength(2));
  const bodies = submissions(fetcher);
  expect(bodies[1].preview_scope_digest).toBe('b'.repeat(64));
  expect(bodies[0].batch_id).not.toBe(bodies[1].batch_id);
});

it('blocks execution while a workbench search is waiting to apply', async () => {
  function ScopeFixture() {
    const [pending, setPending] = useState(false);
    return (
      <>
        <button onClick={() => setPending(!pending)}>切换搜索应用状态</button>
        <JobBatches pid="p" scopePending={pending} />
      </>
    );
  }
  const fetcher = mount(estimate(), false, <ScopeFixture />);
  await configure();
  const start = screen.getByRole('button', { name: '开始标注批次' });
  fireEvent.click(screen.getByRole('button', { name: '切换搜索应用状态' }));
  expect(start).toBeDisabled();
  fireEvent.click(start);
  expect(submissions(fetcher)).toEqual([]);
  fireEvent.click(screen.getByRole('button', { name: '切换搜索应用状态' }));
  await waitFor(() => expect(start).toBeEnabled());
});
