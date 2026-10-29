import { afterEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import {
  DeleteLibrary,
  PendingLibraryDeletions,
  type LibraryResource,
} from '../src/features/LibraryDeletion';
import CoveragePlans from '../src/features/CoveragePlans';
import PromptProduction from '../src/features/PromptProduction';
import { initialTemplate } from '../src/features/coveragePlanData';
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

const resource: LibraryResource = {
  kind: 'dictionary',
  resource_id: 'id',
  name: '本地词典',
  revision: 4,
  version: 'v4',
};
const button = (name: string | RegExp) => screen.getByRole('button', { name });
const click = (name: string | RegExp) => fireEvent.click(button(name));
afterEach(() => {
  vi.useRealTimers();
  queryClient.clear();
  setSessionToken(null);
  vi.restoreAllMocks();
});
function session() {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
}
async function hold(name: string | RegExp) {
  vi.useFakeTimers();
  fireEvent.pointerDown(button(name));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100);
  });
  vi.useRealTimers();
}
function mount(response: () => Promise<Response>, disabled = false) {
  session();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(response),
    onDeleted = vi.fn(),
    onBusy = vi.fn();
  const rendered = render(
    <DeleteLibrary resource={resource} disabled={disabled} onDeleted={onDeleted} onBusy={onBusy} />,
  );
  return {
    fetcher,
    onDeleted,
    onBusy,
    rerender: (next: LibraryResource) =>
      rendered.rerender(
        <DeleteLibrary resource={next} disabled={disabled} onDeleted={onDeleted} onBusy={onBusy} />,
      ),
  };
}
function acknowledge() {
  fireEvent.click(screen.getByRole('checkbox', { name: '我已保存需要的内容，确认删除整份词典' }));
}

it('requires acknowledgement and a full hold and shows the frozen revision before deletion', async () => {
  const { fetcher, onDeleted, onBusy } = mount(async () => Response.json({ deleted: true }));
  click('删除整份词典');
  expect(screen.getByText('本地词典 · r4')).toBeVisible();
  expect(button(/按住删除整份词典/)).toBeDisabled();
  acknowledge();
  vi.useFakeTimers();
  fireEvent.pointerDown(button(/按住删除整份词典/));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(400);
  });
  fireEvent.pointerUp(button(/继续按住/));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100);
  });
  vi.useRealTimers();
  expect(fetcher).not.toHaveBeenCalled();
  await hold(/按住删除整份词典/);
  await waitFor(() => expect(onDeleted).toHaveBeenCalledWith('id'));
  const [url, init] = fetcher.mock.calls[0];
  expect(url).toBe('/api/prompt-production/dictionaries/id');
  expect(init!.method).toBe('DELETE');
  expect(init!.body).toBeUndefined();
  expect(new Headers(init!.headers).get('If-Match')).toBe('"v4"');
  expect(onBusy.mock.calls).toEqual([[true], [false]]);
});

it('reuses the same request identity through unknown outcomes and dialog reopen', async () => {
  const { fetcher, onDeleted } = mount(async () => {
    throw new TypeError('lost response');
  });
  click('删除整份词典');
  acknowledge();
  await hold(/按住删除整份词典/);
  await screen.findByRole('alert');
  await hold(/重试删除/);
  await screen.findByRole('alert');
  click('取消删除');
  click('删除整份词典');
  acknowledge();
  await hold(/按住删除整份词典/);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
  const keys = fetcher.mock.calls.map(([, init]) => new Headers(init!.headers).get('Idempotency-Key'));
  expect(new Set(keys).size).toBe(1);
  expect(onDeleted).not.toHaveBeenCalled();
});

it('keeps a conflict visible and rotates identity only after the selected version changes', async () => {
  const { fetcher, onDeleted, rerender } = mount(async () =>
    Response.json({ error: { code: 'concurrency:version_conflict' } }, { status: 409 }),
  );
  click('删除整份词典');
  acknowledge();
  await hold(/按住删除整份词典/);
  await screen.findByText('内容已被另一次操作修改，请核对最新内容。');
  expect(onDeleted).not.toHaveBeenCalled();
  click('取消删除');
  rerender({ ...resource, revision: 5, version: 'v5' });
  click('删除整份词典');
  acknowledge();
  await hold(/按住删除整份词典/);
  const headers = fetcher.mock.calls.map(([, init]) => new Headers(init!.headers));
  expect(headers[1].get('If-Match')).toBe('"v5"');
  expect(headers[1].get('Idempotency-Key')).not.toBe(headers[0].get('Idempotency-Key'));
});

it('cannot open deletion while the parent has a draft or operation in progress', () => {
  const { fetcher } = mount(async () => Response.json({}), true);
  expect(button('删除整份词典')).toBeDisabled();
  click('删除整份词典');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});

it('only loads interrupted deletion inventory on explicit request and paginates the complete list', async () => {
  session();
  const rows = Array.from({ length: 23 }, (_, i) => ({
    ...resource,
    resource_id: 'id-' + i,
    name: '待清理 ' + i,
  }));
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(Response.json({ items: rows, total: rows.length, next_cursor: null }));
  render(
    <QueryClientProvider client={queryClient}>
      <PendingLibraryDeletions kind="dictionary" disabled={false} onDeleted={vi.fn()} onBusy={vi.fn()} />
    </QueryClientProvider>,
  );
  expect(fetcher).not.toHaveBeenCalled();
  click('检查未完成删除');
  await screen.findByText(/待完成文件清理：23 项/);
  expect(screen.getAllByRole('button', { name: /^继续清理/ })).toHaveLength(20);
  click('下一页未完成删除');
  expect(screen.getAllByRole('button', { name: /^继续清理/ })).toHaveLength(3);
  click('继续清理 待清理 22');
  expect(screen.getByText('待清理 22 · r4', { selector: 'p' })).toBeVisible();
});

function parent(kind: 'dictionary' | 'coverage') {
  session();
  let deleted = false;
  const row =
    kind === 'dictionary'
      ? {
          dictionary_id: 'id',
          name: '删除测试词典',
          kind: 'prompts',
          revision: 1,
          version: 'v1',
          entries: [{ tags: ['forest'], natural_language: null, aspect: null, rating: null }],
        }
      : {
          coverage_id: 'id',
          name: '删除测试方案',
          revision: 1,
          version: 'v1',
          template: initialTemplate,
          primary_dimensions: ['scene', 'lighting'],
          target_count: 100,
          random_seed: '42',
          cell_count: 6,
          browser_compatible: true,
        };
  const prefix = '/api/prompt-production/' + (kind === 'dictionary' ? 'dictionaries' : 'coverage-plans');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    const url = String(path);
    if (init?.method === 'DELETE') {
      deleted = true;
      return Response.json({ deleted: true });
    }
    if (url === prefix + '/id') return Response.json(row);
    if (url.startsWith(prefix + '?'))
      return Response.json({ items: deleted ? [] : [row], total: deleted ? 0 : 1, next_cursor: null });
    return Response.json({ items: [], total: 0, next_cursor: null });
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>{kind === 'dictionary' ? <PromptProduction /> : <CoveragePlans />}</DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return fetcher;
}

it('protects a coverage draft and resets the editor after confirmed deletion', async () => {
  parent('coverage');
  fireEvent.click(await screen.findByRole('button', { name: '编辑 删除测试方案' }));
  await screen.findByRole('heading', { name: '编辑 删除测试方案 · r1' });
  fireEvent.change(screen.getByLabelText('覆盖方案模板'), { target: { value: initialTemplate + '\n' } });
  expect(button('删除覆盖方案')).toBeDisabled();
  click('重新载入已保存版本');
  click('放弃草稿');
  await waitFor(() => expect(button('删除覆盖方案')).toBeEnabled());
  click('删除覆盖方案');
  fireEvent.click(screen.getByRole('checkbox', { name: '我已保存需要的内容，确认删除覆盖方案' }));
  await hold(/按住删除覆盖方案/);
  await screen.findByRole('heading', { name: '新方案' });
  expect(screen.getByLabelText('覆盖方案模板')).toHaveValue(initialTemplate);
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: '编辑 删除测试方案' })).not.toBeInTheDocument(),
  );
});

it('removes a deleted dictionary from selection and starts a fresh draft without reusing its id', async () => {
  const fetcher = parent('dictionary');
  await screen.findByRole('option', { name: '删除测试词典 · 提示词' });
  fireEvent.change(screen.getByLabelText('打开已有词典'), { target: { value: 'id' } });
  await waitFor(() => expect(screen.getByLabelText('词典名称')).toHaveValue('删除测试词典'));
  fireEvent.click(screen.getByRole('checkbox', { name: '删除测试词典' }));
  click('删除整份词典');
  acknowledge();
  await hold(/按住删除整份词典/);
  await waitFor(() => expect(screen.getByLabelText('词典名称')).toHaveValue(''));
  await waitFor(() =>
    expect(screen.queryByRole('checkbox', { name: '删除测试词典' })).not.toBeInTheDocument(),
  );
  const deletes = fetcher.mock.calls.filter(([, init]) => init?.method === 'DELETE');
  expect(deletes).toHaveLength(1);
  expect(screen.getByLabelText('打开已有词典')).toHaveValue('');
});
