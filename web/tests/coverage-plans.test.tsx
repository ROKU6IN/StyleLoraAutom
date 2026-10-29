import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import CoveragePlans, { CoveragePlanPicker } from '../src/features/CoveragePlans';
import { coverageApplication, initialTemplate, type SavedCoverage } from '../src/features/coveragePlanData';
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

const saved: SavedCoverage = {
  coverage_id: 'coverage',
  version: 'v1',
  revision: 1,
  template: initialTemplate,
  name: '场景与光线覆盖',
  primary_dimensions: ['scene', 'lighting'],
  cell_count: 6,
  target_count: 100,
  random_seed: '42',
  browser_compatible: true,
};
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function mount(
  options: {
    unknown?: boolean;
    conflict?: boolean;
    picker?: boolean;
    unsafe?: boolean;
    loading?: Promise<void>;
  } = {},
) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const writes: { body: Record<string, unknown>; headers: HeadersInit }[] = [];
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    const url = String(path);
    if (url.endsWith('/preview')) return Response.json(saved);
    if (init?.method === 'POST' || init?.method === 'PUT') {
      writes.push({ body: JSON.parse(init.body as string), headers: init.headers! });
      if (options.unknown) throw new TypeError('lost reply');
      if (options.conflict)
        return Response.json({ error: { code: 'concurrency:version_conflict' } }, { status: 409 });
      return Response.json(saved);
    }
    if (url.includes('?')) return Response.json({ items: [saved], next_cursor: null, total: 1 });
    if (options.loading) {
      await options.loading;
      return Response.json({ error: { code: 'coverage:deleted' } }, { status: 404 });
    }
    return Response.json(
      options.unsafe ? { ...saved, random_seed: '18446744073709551615', browser_compatible: false } : saved,
    );
  });
  const onApply = vi.fn();
  const onBusy = vi.fn();
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          {options.picker ? (
            <CoveragePlanPicker disabled={false} onApply={onApply} onBusy={onBusy} />
          ) : (
            <CoveragePlans />
          )}
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { fetcher, writes, onApply, onBusy };
}

it('loads a saved plan and preserves the draft and If-Match on conflict', async () => {
  const { writes } = mount({ conflict: true });
  fireEvent.click(await screen.findByRole('button', { name: '编辑 场景与光线覆盖' }));
  await screen.findByRole('heading', { name: '编辑 场景与光线覆盖 · r1' });
  const content = initialTemplate.replace('"target_count": 100', '"target_count": 12');
  fireEvent.change(screen.getByLabelText('覆盖方案模板'), { target: { value: content } });
  fireEvent.click(screen.getByRole('button', { name: '保存覆盖方案' }));
  await waitFor(() => expect(writes).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存覆盖方案' })).toBeEnabled());
  expect(screen.getByLabelText('覆盖方案模板')).toHaveValue(content);
  expect(new Headers(writes[0].headers).get('If-Match')).toBe('"v1"');
  expect(writes[0].body).toEqual({ format: 'json', content });
});

it('reuses the same identity after an unknown save result and rotates it only after editing', async () => {
  const { writes } = mount({ unknown: true });
  const save = screen.getByRole('button', { name: '保存覆盖方案' });
  fireEvent.click(save);
  await waitFor(() => expect(save).toBeEnabled());
  fireEvent.click(save);
  await waitFor(() => expect(writes).toHaveLength(2));
  await waitFor(() => expect(save).toBeEnabled());
  expect(writes[0].body.coverage_id).toBe(writes[1].body.coverage_id);
  expect(new Headers(writes[0].headers).get('Idempotency-Key')).toBe(
    new Headers(writes[1].headers).get('Idempotency-Key'),
  );
  fireEvent.change(screen.getByLabelText('覆盖方案模板'), { target: { value: initialTemplate + '\n' } });
  fireEvent.click(save);
  await waitFor(() => expect(writes).toHaveLength(3));
  expect(new Headers(writes[2].headers).get('Idempotency-Key')).not.toBe(
    new Headers(writes[1].headers).get('Idempotency-Key'),
  );
});

it('protects edited templates when changing plans and invalidates the preview on edits', async () => {
  mount();
  fireEvent.click(screen.getByRole('button', { name: '校验模板' }));
  await screen.findByText('模板结构校验通过，尚未保存。');
  fireEvent.change(screen.getByLabelText('覆盖方案模板'), { target: { value: initialTemplate + '\n' } });
  expect(screen.queryByText('模板结构校验通过，尚未保存。')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '编辑 场景与光线覆盖' }));
  await screen.findByRole('dialog', { name: '有未保存的覆盖方案' });
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
  expect(screen.getByLabelText('覆盖方案模板')).toHaveValue(initialTemplate + '\n');
});

it('sends YAML text unchanged for server validation and disables export with unsaved changes', async () => {
  const { writes } = mount({ unknown: true });
  fireEvent.click(await screen.findByRole('button', { name: '编辑 场景与光线覆盖' }));
  await screen.findByRole('heading', { name: '编辑 场景与光线覆盖 · r1' });
  const content = 'name: 精确种子\nplan:\n  random_seed: 18446744073709551615\n';
  fireEvent.change(screen.getByLabelText('模板格式'), { target: { value: 'yaml' } });
  fireEvent.change(screen.getByLabelText('覆盖方案模板'), { target: { value: content } });
  expect(screen.getByRole('button', { name: '导出 YAML 模板' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '保存覆盖方案' }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0].body).toEqual({ format: 'yaml', content });
});

it('applies rules, count and seed only after explicitly loading a selected plan', async () => {
  const { onApply, onBusy } = mount({ picker: true });
  await screen.findByRole('option', { name: '场景与光线覆盖 · r1' });
  fireEvent.change(screen.getByLabelText('已保存的覆盖方案'), { target: { value: 'coverage' } });
  expect(onApply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '载入并替换覆盖设置' }));
  await waitFor(() => expect(onApply).toHaveBeenCalledOnce());
  await waitFor(() => expect(onBusy.mock.calls).toEqual([[true], [false]]));
  const value = onApply.mock.calls[0][0];
  expect(value.count).toBe('100');
  expect(value.seed).toBe('42');
  expect(JSON.parse(value.rules)).not.toHaveProperty('random_seed');
  expect(JSON.parse(value.rules).primary_dimensions).toEqual(['scene', 'lighting']);
});

it('locks the parent while a plan loads and releases it on failure without applying stale settings', async () => {
  let release!: () => void;
  const loading = new Promise<void>((resolve) => {
    release = resolve;
  });
  const { onApply, onBusy } = mount({ picker: true, loading });
  await screen.findByRole('option', { name: '场景与光线覆盖 · r1' });
  fireEvent.change(screen.getByLabelText('已保存的覆盖方案'), { target: { value: 'coverage' } });
  const load = screen.getByRole('button', { name: '载入并替换覆盖设置' });
  fireEvent.click(load);
  expect(load).toBeDisabled();
  expect(onBusy.mock.calls).toEqual([[true]]);
  expect(onApply).not.toHaveBeenCalled();
  release();
  await waitFor(() => expect(onBusy.mock.calls).toEqual([[true], [false]]));
  expect(load).toBeEnabled();
  expect(onApply).not.toHaveBeenCalled();
});

it('rejects unsafe browser seeds without applying a rounded plan and preserves automatic seeds', async () => {
  const { onApply } = mount({ picker: true, unsafe: true });
  await screen.findByRole('option', { name: '场景与光线覆盖 · r1' });
  fireEvent.change(screen.getByLabelText('已保存的覆盖方案'), { target: { value: 'coverage' } });
  fireEvent.click(screen.getByRole('button', { name: '载入并替换覆盖设置' }));
  await screen.findByText(/此方案的种子超过浏览器可精确表示的范围/);
  expect(onApply).not.toHaveBeenCalled();
  const template = JSON.parse(initialTemplate);
  template.plan.random_seed = null;
  expect(coverageApplication({ ...saved, random_seed: null, template: JSON.stringify(template) }).seed).toBe(
    '',
  );
});
