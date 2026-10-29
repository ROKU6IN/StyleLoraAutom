import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import GenerationSources, { generationDraft } from '../src/features/GenerationSources';
import formCapabilities from './fixtures/nai-direct-capabilities.json';
const initialGenerationConfig = formCapabilities.form.default_config;
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
const source = {
  source_id: 1,
  name: '生成样例',
  type: 'nai',
  status: 'draft',
  locked: false,
  version: 'v1',
  config_json: initialGenerationConfig,
  style_prompt: 'geometric style',
  negative_prompt: 'unwanted',
  target_count: 205,
  cost_policy: 'ask',
  counts: { pending: 0, sent: 0, done: 0, failed: 0, skipped: 0 },
  runtime: { pause_reason: null },
};
const preview = {
  preview_id: 'preview-1',
  digest: 'd'.repeat(64),
  source_digest: 'a'.repeat(64),
  seeds_digest: 'b'.repeat(64),
  count: 205,
  source_id: 1,
  source_version: 'v1',
  can_start: true,
  blockers: [],
  trigger_validation: { status: 'valid', issues: [] },
  cost_policy: 'ask',
  aspect_counts: { portrait: 103, landscape: 102 },
  examples: [{ seq: 1, positive: 'geometric style, forest', negative: 'unwanted', width: 512, height: 768 }],
  examples_truncated: true,
};
function mount({
  enabled = false,
  unknown = false,
  conflict = false,
  invalid = false,
  legacy = false,
  oversize = false,
} = {}) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    const path = String(url);
    if (path === '/api/backends/nai_direct/capabilities') return Response.json(formCapabilities);
    if (path === '/api/backends/nai_launcher/capabilities')
      return Response.json({ error: { code: 'backend:generation_unavailable' } }, { status: 503 });
    if (path.endsWith('/unknown-disposition'))
      return Response.json({
        source_id: 1,
        source_status: 'draft',
        unknown_count: 0,
        pending_count: 0,
        unknown_items: [],
        items_truncated: false,
        busy: false,
        reason: 'nai:no_unknown_attempts',
        can_stop: false,
        decision: null,
        upstream_outcome: 'see_item_records',
        version: 'a'.repeat(64),
      });
    if (path.includes('/recovery?'))
      return Response.json({
        items: [],
        total: 0,
        limit: 20,
        upper_id: 0,
        has_more: false,
        next_cursor: null,
      });
    if (path.startsWith('/api/presets')) {
      const preset = {
        preset_id: 'oversize',
        name: '超范围预设',
        revision: 1,
        version: 'v1',
        builtin: false,
        sizes: { ...initialGenerationConfig.sizes, landscape: { width: 1600, height: 1024 } },
      };
      return Response.json(
        path.endsWith('/oversize') ? preset : { items: oversize ? [preset] : [], next_cursor: null },
      );
    }
    if (path.endsWith('/capabilities'))
      return Response.json({ generation: enabled, size_presets: { small: initialGenerationConfig.sizes } });
    if (path.includes('/tasks?'))
      return Response.json({
        items: [{ task_id: 'task', revision: 7, ready: true, requested: 205 }],
        next_cursor: null,
      });
    if (path.includes('/sources?')) return Response.json({ items: [source], next_cursor: null });
    if (path.endsWith('/handoff-preview'))
      return Response.json({
        ...preview,
        can_start: enabled,
        blockers: enabled ? [] : ['generation:adapter_unavailable'],
      });
    if (path.endsWith('/reconcile-start'))
      return Response.json({ ...source, locked: true, status: 'paused', version: 'v2', reconciled: true });
    if (path.endsWith('/start')) {
      if (unknown) throw new TypeError('lost reply');
      return Response.json({ ...source, locked: true, status: 'running', version: 'v2', reconciled: false });
    }
    if (init?.method === 'PUT')
      return Response.json(
        {
          error: invalid
            ? {
                code: 'generation:unsupported_configuration',
                issues: [{ field: 'config.profile.steps', code: 'schema:maximum' }],
              }
            : { code: 'concurrency:version_conflict' },
        },
        { status: invalid ? 422 : conflict ? 409 : 400 },
      );
    return Response.json(
      legacy ? { ...source, config_json: { ...source.config_json, backend: 'nai_launcher' } } : source,
    );
  });
  render(
    <MemoryRouter initialEntries={['/?task=task']}>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          <GenerationSources pid="project" />
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return fetcher;
}
async function openDraft() {
  await screen.findByRole('option', { name: '生成样例 · #1' });
  fireEvent.change(screen.getByLabelText('打开生成源'), { target: { value: '1' } });
  await waitFor(() => expect(screen.getByLabelText('生成源名称')).toHaveValue('生成样例'));
}
async function getPreview() {
  await openDraft();
  await waitFor(() => expect(screen.getByRole('button', { name: '预览提示词交接' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '预览提示词交接' }));
  return screen.findByLabelText('提示词交接预览');
}
it('requires a named bounded source and uses the reviewed direct backend defaults', () => {
  expect(() => generationDraft('', '5', '', '', 'ask', '{}')).toThrow('来源名称');
  expect(() => generationDraft('a', '1.5', '', '', 'ask', '{}')).toThrow('目标数量');
  expect(() => generationDraft('a', '2', '', '', 'ask', '[]')).toThrow('对象');
  expect(() => generationDraft('a', '2', '', '', 'ask', '{')).toThrow('JSON');
  expect(initialGenerationConfig.profile).toMatchObject({
    quality_toggle: false,
    uc_preset: null,
    steps: 23,
    sampler: 'k_euler_ancestral',
  });
});
it('previews the complete count but blocks start when no reviewed adapter exists', async () => {
  const fetcher = mount();
  const report = await getPreview();
  expect(report).toHaveTextContent('完整交接 205 条');
  expect(report).toHaveTextContent('真实生成适配尚未开放');
  expect(screen.getByRole('button', { name: '确认交接并启动' })).toBeDisabled();
  const request = fetcher.mock.calls.find(([url]) => String(url).endsWith('/handoff-preview'))!;
  expect(JSON.parse(request[1]!.body as string)).toEqual({ task_id: 'task', expected_task_revision: 7 });
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/start'))).toBe(false);
});
it('invalidates the visible preview on edits and preserves a conflicting draft', async () => {
  const fetcher = mount({ conflict: true });
  await getPreview();
  fireEvent.change(screen.getByLabelText('画风提示词'), { target: { value: 'updated local style' } });
  expect(screen.queryByLabelText('提示词交接预览')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '预览提示词交接' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '保存生成源草稿' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('画风提示词')).toHaveValue('updated local style');
  const saving = fetcher.mock.calls.find(([, init]) => init?.method === 'PUT')!;
  expect(new Headers(saving[1]!.headers).get('If-Match')).toBe('"v1"');
});
it('checks persisted projection after an unknown start without submitting a second start', async () => {
  const fetcher = mount({ enabled: true, unknown: true });
  await getPreview();
  expect(screen.getByRole('button', { name: '确认交接并启动' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: /确认锁定来源并启动生成/ }));
  fireEvent.click(screen.getByRole('button', { name: '确认交接并启动' }));
  await screen.findByText('已核对完整锁定内容，没有重复派发。');
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/start'))).toHaveLength(1);
  const calls = fetcher.mock.calls.filter(([url]) => String(url).endsWith('/reconcile-start'));
  expect(calls).toHaveLength(1);
  expect(JSON.parse(calls[0][1]!.body as string)).toEqual({
    source_digest: preview.source_digest,
    seeds_digest: preview.seeds_digest,
    count: 205,
  });
  expect(screen.getByRole('button', { name: '保存生成源草稿' })).toBeDisabled();
});
it('warns before abandoning unsaved source changes', async () => {
  mount();
  await openDraft();
  fireEvent.change(screen.getByLabelText('生成源名称'), { target: { value: '未保存' } });
  fireEvent.change(screen.getByLabelText('打开生成源'), { target: { value: '' } });
  await screen.findByRole('dialog');
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
  expect(screen.getByLabelText('生成源名称')).toHaveValue('未保存');
});
it('saves form values from backend defaults and does not call a generation or account endpoint', async () => {
  const fetcher = mount();
  await screen.findByLabelText('步数');
  fireEvent.change(screen.getByLabelText('生成源名称'), { target: { value: '表单草稿' } });
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '29' } });
  fireEvent.click(screen.getByRole('button', { name: '保存生成源草稿' }));
  await screen.findByText('生成源草稿已保存；尚未锁定或提交生成。');
  const saving = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(JSON.parse(saving[1]!.body as string).config.profile.steps).toBe(29);
  expect(fetcher.mock.calls.some(([url]) => /\/start$|\/probe$|\/generate-image$/.test(String(url)))).toBe(
    false,
  );
});
it('blocks unsupported input locally and shows backend issues on a valid submitted field', async () => {
  const fetcher = mount({ invalid: true });
  await openDraft();
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '51' } });
  fireEvent.click(screen.getByRole('button', { name: '保存生成源草稿' }));
  await screen.findByText('请先修正生成参数表单中的错误。');
  expect(fetcher.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '30' } });
  fireEvent.click(screen.getByRole('button', { name: '保存生成源草稿' }));
  await screen.findByText(/后端拒绝此字段/);
  expect(screen.getByLabelText('步数')).toHaveValue('30');
  expect(screen.getByLabelText('步数')).toHaveAttribute('aria-invalid', 'true');
});
it('keeps unsaved parameters when capability defaults refresh', async () => {
  mount();
  await screen.findByLabelText('步数');
  fireEvent.change(screen.getByLabelText('生成源名称'), { target: { value: '已有输入' } });
  const changed = structuredClone(formCapabilities);
  changed.form.default_config.profile.steps = 40;
  queryClient.setQueryData(['backend', 'nai_direct', 'form-capabilities'], changed);
  await waitFor(() => expect(screen.getByLabelText('步数')).toHaveValue('23'));
  expect(screen.getByLabelText('生成源名称')).toHaveValue('已有输入');
});
it('preserves legacy Launcher configurations while capabilities are unavailable', async () => {
  mount({ legacy: true });
  await openDraft();
  await screen.findByText(/参数表单暂不可用/);
  expect(screen.queryByLabelText('步数')).not.toBeInTheDocument();
  const config = JSON.parse((screen.getByLabelText('生成配置（JSON）') as HTMLTextAreaElement).value);
  expect(config.backend).toBe('nai_launcher');
  expect(config.profile).toEqual(initialGenerationConfig.profile);
});
it('rejects a size preset beyond backend limits without replacing the edited configuration', async () => {
  mount({ oversize: true });
  await openDraft();
  fireEvent.change(screen.getByLabelText('步数'), { target: { value: '32' } });
  fireEvent.change(screen.getByLabelText('填入尺寸预设'), { target: { value: 'oversize' } });
  await screen.findByText('此尺寸预设超出当前后端能力范围，原配置已保留。');
  expect(screen.getByLabelText('步数')).toHaveValue('32');
  expect(screen.getByLabelText('横图宽度')).toHaveValue('768');
});
