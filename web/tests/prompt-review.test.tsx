import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { ReviewEditor, type ReviewDetail } from '../src/features/PromptReview';
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

const entry = {
  tags: ['no humans', 'forest', 'sunlight'],
  natural_language: 'A dreamy forest.',
  aspect: 'landscape',
  rating: 'general',
};
const initial: ReviewDetail = {
  task_id: 't',
  index: 7,
  revision: 12,
  status: 'review_required',
  reason: 'review_required',
  can_review: true,
  blocked_reason: null,
  entry,
  original_entry: { ...entry, natural_language: null },
  model_entry: entry,
  out_of_frame: [],
  rating_evidence: ['general'],
  options: { tag_range: [1, 20], nl_enabled: true, aspect_hint: 'landscape' },
  review: null,
};
const good = {
  preview_id: 'p',
  digest: 'a'.repeat(64),
  expected_revision: 12,
  valid: true,
  issues: [],
  entry: { ...entry, natural_language: 'Trees stand beside a river.' },
  ready: true,
  previous_ready: false,
  selected: true,
  count: 1,
  selection_changes: 1,
  rating_lower_bound: 'general',
  coverage: {
    ready: true,
    available: 1,
    count: 1,
    diagnostics: {},
    warnings: {},
    source_counts: {},
    complete_pool: true,
    search_nodes: 1,
    requested: 1,
    status: 'satisfied',
    cell_targets: { random: 1 },
    cell_counts: { random: 1 },
    constraint_counts: {},
    constraint_bounds: {},
  },
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function mount(
  options: { invalid?: boolean; conflict?: boolean; unknown?: boolean; detail?: ReviewDetail } = {},
) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const writes: { url: string; body: Record<string, unknown>; key: string | null }[] = [];
  let saved = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    const url = String(path);
    if (init?.method === 'POST') {
      writes.push({
        url,
        body: JSON.parse(init.body as string),
        key: new Headers(init.headers).get('Idempotency-Key'),
      });
      if (url.endsWith('/preview'))
        return Response.json(
          options.invalid ? { ...good, valid: false, issues: ['review_required'], coverage: null } : good,
        );
      if (options.unknown) throw new TypeError('reply lost');
      if (options.conflict)
        return Response.json({ error: { code: 'prompt:revision_conflict' } }, { status: 409 });
      saved = true;
      return Response.json({ revision: 13, ready: true });
    }
    return Response.json(
      saved
        ? {
            ...initial,
            entry: good.entry,
            revision: 13,
            status: 'ok',
            can_review: false,
            blocked_reason: 'prompt:already_reviewed',
            review: {
              reviewed_at: '2026-10-26T00:00:00Z',
              source: 'model_output',
              previous_reason: 'review_required',
            },
          }
        : initial,
    );
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          <ReviewEditor initial={options.detail ?? initial} />
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return writes;
}
async function preview() {
  fireEvent.change(screen.getByLabelText('自然语言描述'), {
    target: { value: 'Trees stand beside a river.' },
  });
  fireEvent.click(screen.getByRole('button', { name: '复验修改并预览结果' }));
  await screen.findByLabelText('人工复验预览');
}
const ack = () => fireEvent.click(screen.getByRole('checkbox', { name: /我已检查内容与评级/ }));

it('requires a valid preview and explicit confirmation, then shows the saved review as read only', async () => {
  const writes = mount();
  expect(screen.queryByRole('button', { name: '确认保存复验结果' })).not.toBeInTheDocument();
  await preview();
  const save = screen.getByRole('button', { name: '确认保存复验结果' });
  expect(save).toBeDisabled();
  ack();
  expect(save).toBeEnabled();
  fireEvent.click(save);
  await screen.findByText('此条目已完成人工复验，当前为只读记录。');
  expect(screen.getByLabelText('自然语言描述')).toBeDisabled();
  expect(writes[0].body).toMatchObject({
    expected_revision: 12,
    index: 7,
    entry: { rating: 'general', natural_language: 'Trees stand beside a river.' },
  });
  expect(writes[1].body).toEqual({ preview_id: 'p', digest: 'a'.repeat(64), acknowledge: true });
});

it('keeps invalid drafts and offers no approval bypass', async () => {
  const writes = mount({ invalid: true });
  await preview();
  await screen.findByText('复验未通过，原记录保持不变。');
  expect(screen.getByText('仍含待审核表达，请修改后再次复验。')).toBeVisible();
  expect(screen.queryByRole('checkbox', { name: /我已检查内容与评级/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '确认保存复验结果' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('自然语言描述')).toHaveValue('Trees stand beside a river.');
  expect(writes).toHaveLength(1);
});

it('editing after confirmation invalidates both the preview and acknowledgement', async () => {
  mount();
  await preview();
  ack();
  fireEvent.change(screen.getByLabelText('人工确认评级'), { target: { value: 'sensitive' } });
  expect(screen.queryByLabelText('人工复验预览')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '复验修改并预览结果' }));
  await screen.findByLabelText('人工复验预览');
  expect(screen.getByRole('checkbox', { name: /我已检查内容与评级/ })).not.toBeChecked();
});

it('keeps edits on task revision conflict', async () => {
  mount({ conflict: true });
  await preview();
  ack();
  fireEvent.click(screen.getByRole('button', { name: '确认保存复验结果' }));
  await screen.findByText('任务状态已变化，请刷新后重试。');
  expect(screen.getByLabelText('自然语言描述')).toHaveValue('Trees stand beside a river.');
  expect(screen.getByRole('button', { name: '确认保存复验结果' })).toBeEnabled();
});

it('reuses the durable request key when a review commit reply is lost', async () => {
  const writes = mount({ unknown: true });
  await preview();
  ack();
  const save = screen.getByRole('button', { name: '确认保存复验结果' });
  fireEvent.click(save);
  await screen.findByText('本次提交结果未知，请先查询任务和数据，不要重复提交。');
  await waitFor(() => expect(save).toBeEnabled());
  fireEvent.click(save);
  await waitFor(() => expect(writes).toHaveLength(3));
  expect(writes[1].key).toBe(writes[2].key);
  expect(writes[1].body).toEqual(writes[2].body);
});

it('protects a draft before reloading and refuses guard-save without review confirmation', async () => {
  mount();
  fireEvent.change(screen.getByLabelText('自然语言描述'), { target: { value: 'Local draft' } });
  fireEvent.click(screen.getByRole('button', { name: '重新载入当前记录' }));
  await screen.findByRole('dialog', { name: '有未保存的提示词复验草稿' });
  fireEvent.click(screen.getByRole('button', { name: '保存并继续' }));
  await screen.findByText('请先通过复验，并确认内容与评级后再保存。');
  expect(screen.getByLabelText('自然语言描述')).toHaveValue('Local draft');
  fireEvent.click(screen.getByRole('button', { name: '重新载入当前记录' }));
  fireEvent.click(await screen.findByRole('button', { name: '放弃草稿' }));
  await waitFor(() => expect(screen.getByLabelText('自然语言描述')).toHaveValue('A dreamy forest.'));
});

it('explains missing historical model output and requires choosing a rating and aspect', async () => {
  const writes = mount({
    detail: { ...initial, model_entry: null, entry: { ...entry, rating: null, aspect: null } },
  });
  expect(screen.getByText(/此条目没有保留可供审核的模型输出/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '复验修改并预览结果' }));
  await screen.findByText('请明确选择图幅和人工确认评级。');
  expect(writes).toHaveLength(0);
});

it('shows why active tasks cannot be reviewed', () => {
  mount({ detail: { ...initial, can_review: false, blocked_reason: 'prompt:review_requires_done' } });
  expect(screen.getByText('请等待任务完成且后台退出后再进行人工复验。')).toBeVisible();
  expect(screen.getByLabelText('自然语言描述')).toBeDisabled();
  expect(screen.getByRole('button', { name: '复验修改并预览结果' })).toBeDisabled();
});
