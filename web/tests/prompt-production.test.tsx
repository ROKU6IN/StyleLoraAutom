import { afterEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import PromptProduction, {
  CoverageReport,
  PromptTask,
  defaultCoverage,
  dictionaryPayload,
  samplingPayload,
} from '../src/features/PromptProduction';
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
const dictionary = {
  dictionary_id: 'd',
  name: '样例特征',
  kind: 'features' as const,
  revision: 1,
  version: 'v1',
  dimensions: { scene: [{ tag: 'forest', rating: 'general' }] },
};
const report = {
  status: 'satisfied',
  ready: true,
  requested: 100,
  available: 120,
  count: 100,
  cell_targets: { random: 100 },
  cell_counts: { random: 100 },
  constraint_counts: {},
  constraint_bounds: {},
  diagnostics: {},
  warnings: {},
  source_counts: {},
  complete_pool: true,
  search_nodes: 100,
};
function mount(component = <PromptProduction />, saveConflict = false, ready = false) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    const url = String(path);
    if (url.endsWith('/coverage/preview'))
      return Response.json({
        ...report,
        request: JSON.parse(init!.body as string),
        preview_digest: 'a'.repeat(64),
        examples: [],
        examples_truncated: false,
        refinement: JSON.parse(init!.body as string).optimize
          ? {
              roles: { prompt: { model: 'fixture-model', protocol: 'openai_chat' }, rating: null },
              max_candidates: 300,
              max_semantic_calls_per_candidate: 3,
            }
          : undefined,
      });
    if (init?.method === 'POST') throw new TypeError('unknown local result');
    if (init?.method === 'PUT')
      return Response.json(
        { error: { code: 'concurrency:version_conflict' } },
        { status: saveConflict ? 409 : 200 },
      );
    if (url.endsWith('/dictionaries/d')) return Response.json(dictionary);
    if (url.includes('/dictionaries?')) return Response.json({ items: [dictionary], next_cursor: null });
    if (url.endsWith('/tasks/t'))
      return Response.json({
        task_id: 't',
        revision: 3,
        status: 'done',
        ready,
        random_seed: 42,
        requested: 100,
        coverage: { ...report, status: 'unsatisfiable', ready: false, count: 0 },
      });
    return Response.json({ items: [], total: 0, next_cursor: null });
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>{component}</DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return fetcher;
}
async function preview() {
  fireEvent.click(await screen.findByRole('checkbox', { name: '样例特征' }));
  fireEvent.click(screen.getByRole('button', { name: '预览覆盖与样例' }));
  await screen.findByRole('button', { name: '创建本地采样任务' });
}
it('validates structured dictionary input without interpreting arbitrary text', () => {
  expect(() => dictionaryPayload('features', 'a', '[]')).toThrow('维度对象');
  expect(() => dictionaryPayload('prompts', 'a', '{}')).toThrow('条目数组');
  expect(() => dictionaryPayload('prompts', 'a', 'not json')).toThrow('有效的 JSON');
  expect(dictionaryPayload('prompts', '内容', '[{"tags":["forest"]}]')).toMatchObject({
    kind: 'prompts',
    entries: [{ tags: ['forest'] }],
  });
});
it('keeps safe seeds, explicit coverage toggle and both source kinds', () => {
  const dictionaries = [dictionary, { ...dictionary, dictionary_id: 'p', kind: 'prompts' as const }];
  expect(
    samplingPayload(['p', 'd'], dictionaries, '100', '', false, JSON.stringify(defaultCoverage)),
  ).toMatchObject({
    prompt_dictionary_ids: ['p'],
    feature_dictionary_ids: ['d'],
    coverage_enabled: false,
    plan: { random_seed: null, target_count: 100 },
    optimize: false,
  });
  expect(() => samplingPayload(['d'], dictionaries, '100', '9007199254740993', true, '{}')).toThrow('整数');
  expect(() => samplingPayload(['d'], dictionaries, '0', '42', true, '{}')).toThrow('整数');
});
it('invalidates coverage confirmation immediately when sampling conditions change', async () => {
  mount();
  await preview();
  expect(screen.getByRole('button', { name: '创建本地采样任务' })).toBeEnabled();
  fireEvent.change(screen.getByLabelText('目标数量'), { target: { value: '12' } });
  expect(screen.queryByRole('button', { name: '创建本地采样任务' })).not.toBeInTheDocument();
  expect(screen.getByText('采样条件已改变，请重新预览。')).toBeInTheDocument();
});
it('reuses task ID and request key after an unknown create result', async () => {
  const fetcher = mount();
  await preview();
  const button = screen.getByRole('button', { name: '创建本地采样任务' });
  fireEvent.click(button);
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() => expect(button).toBeEnabled());
  const calls = fetcher.mock.calls.filter(
    ([path, init]) => String(path).endsWith('/tasks') && init?.method === 'POST',
  );
  expect(calls).toHaveLength(2);
  expect(calls[0][1]?.body).toEqual(calls[1][1]?.body);
  expect(calls[0][1]?.headers).toEqual(calls[1][1]?.headers);
});
it('retains dictionary edits on a revision conflict', async () => {
  mount(<PromptProduction />, true);
  await screen.findByRole('option', { name: '样例特征 · 特征' });
  fireEvent.change(screen.getByLabelText('打开已有词典'), { target: { value: 'd' } });
  await waitFor(() => expect(screen.getByLabelText('词典名称')).toHaveValue('样例特征'));
  fireEvent.change(screen.getByLabelText('词典名称'), { target: { value: '保留我的修改' } });
  fireEvent.click(screen.getByRole('button', { name: '保存词典' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('词典名称')).toHaveValue('保留我的修改');
  expect(screen.getByRole('button', { name: '保存词典' })).toBeEnabled();
});
it('blocks incomplete form drafts before writing and protects them during a dictionary switch', async () => {
  const fetcher = mount();
  fireEvent.click(screen.getByRole('button', { name: '新增候选标签' }));
  fireEvent.change(screen.getByLabelText('词典名称'), { target: { value: '未完成词典' } });
  fireEvent.click(screen.getByRole('button', { name: '保存词典' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('标签需 1–200 字符');
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '新建提示词词典' }));
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '保存并继续' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByLabelText('词典名称')).toHaveValue('未完成词典');
  expect(screen.getByLabelText('候选标签')).toHaveValue('');
});

it('retries an unknown form save with the original key and reports persisted duplicate merges', async () => {
  const fetcher = mount();
  await screen.findByRole('option', { name: '样例特征 · 特征' });
  fireEvent.change(screen.getByLabelText('打开已有词典'), { target: { value: 'd' } });
  await waitFor(() => expect(screen.getByLabelText('词典名称')).toHaveValue('样例特征'));
  fireEvent.click(screen.getByRole('button', { name: '复制当前条目' }));
  fireEvent.change(screen.getByLabelText('条目评级'), { target: { value: 'sensitive' } });
  let attempts = 0;
  fetcher.mockImplementation(async (_path, init) => {
    if (init?.method === 'PUT') {
      attempts++;
      if (attempts === 1) throw new TypeError('response lost');
      return Response.json({
        revision: 2,
        duplicates: 1,
        issues: [{ code: 'dictionary:rating_merged', field: 'scene' }],
      });
    }
    return Response.json({
      ...dictionary,
      version: 'v2',
      revision: 2,
      dimensions: { scene: [{ tag: 'forest', rating: 'sensitive' }] },
    });
  });
  fireEvent.click(screen.getByRole('button', { name: '保存词典' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('条目评级')).toHaveValue('sensitive');
  fireEvent.click(screen.getByRole('button', { name: '保存词典' }));
  await screen.findByText('已保存版本 2；合并重复条目 1 条。');
  const calls = fetcher.mock.calls.filter(([, init]) => init?.method === 'PUT');
  expect(calls).toHaveLength(2);
  expect(calls[0][1]?.body).toEqual(calls[1][1]?.body);
  expect(calls[0][1]?.headers).toEqual(calls[1][1]?.headers);
  expect(screen.getByText(/已按规则取较高评级/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存词典' })).toBeDisabled();
});
it('reports incomplete search honestly and blocks frozen-seed export', async () => {
  const fetcher = mount(<PromptTask identifier="t" />);
  const button = await screen.findByRole('button', { name: '导出冻结提示词 JSON' });
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(fetcher.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
  expect(screen.getByText('尚未就绪', { exact: false })).toBeInTheDocument();
});
it('handoff link follows the current project after project switches', async () => {
  queryClient.setQueryData(['current'], { project_id: 'first' });
  mount(<PromptTask identifier="t" />, false, true);
  const link = await screen.findByRole('link', { name: '交接到当前项目的生成源' });
  expect(link).toHaveAttribute('href', '/p/first/sources?task=t');
  act(() => {
    queryClient.setQueryData(['current'], { project_id: 'second' });
  });
  await waitFor(() => expect(link).toHaveAttribute('href', '/p/second/sources?task=t'));
  act(() => {
    queryClient.setQueryData(['current'], null);
  });
  await waitFor(() =>
    expect(screen.queryByRole('link', { name: '交接到当前项目的生成源' })).not.toBeInTheDocument(),
  );
});
it('distinguishes exhausted candidate search from a proven impossible plan', () => {
  render(
    <CoverageReport
      value={{
        ...report,
        status: 'search_exhausted',
        ready: false,
        complete_pool: false,
        count: 0,
        warnings: { rating_unverified: 3 },
      }}
    />,
  );
  expect(screen.getByText('搜索预算耗尽')).toBeInTheDocument();
  expect(screen.getByText(/缺口不代表数学无解/)).toBeInTheDocument();
  expect(screen.getByText('评级未验证：3 条')).toBeInTheDocument();
});

it('requires explicit model-cost confirmation and discards it after a changed preview', async () => {
  const fetcher = mount();
  fireEvent.click(await screen.findByRole('checkbox', { name: '样例特征' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '启用 LLM 精修' }));
  fireEvent.click(screen.getByRole('button', { name: '预览覆盖与样例' }));
  const create = await screen.findByRole('button', { name: '确认创建精修任务' });
  expect(create).toBeDisabled();
  expect(screen.getByText(/fixture-model/)).toBeVisible();
  fireEvent.click(screen.getByRole('checkbox', { name: /确认创建任务后调用/ }));
  expect(create).toBeEnabled();
  fireEvent.change(screen.getByLabelText('精修选项（JSON）'), { target: { value: '{"tag_range":[1,10]}' } });
  expect(screen.queryByRole('button', { name: '确认创建精修任务' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '预览覆盖与样例' }));
  expect(await screen.findByRole('button', { name: '确认创建精修任务' })).toBeDisabled();
  expect(
    fetcher.mock.calls.filter(([path, init]) => String(path).endsWith('/tasks') && init?.method === 'POST'),
  ).toHaveLength(0);
});
