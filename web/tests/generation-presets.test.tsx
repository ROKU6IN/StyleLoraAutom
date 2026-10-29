import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import GenerationPresets, {
  PresetPicker,
  SMALL_SIZES,
  presetPayload,
  type Preset,
} from '../src/features/GenerationPresets';
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient } from '../src/state/queryClient';
import { setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
const builtin: Preset = {
  preset_id: 'small',
  name: '小',
  sizes: SMALL_SIZES,
  builtin: true,
  builtin_key: 'small',
  revision: 1,
  version: 'v1',
};
const custom: Preset = { ...builtin, preset_id: 'custom', name: '自定义', builtin: false, builtin_key: null };
const change = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));
const wrap = (child: React.ReactNode) => (
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>
      <DraftProvider>{child}</DraftProvider>
    </QueryClientProvider>
  </MemoryRouter>
);

function mount({ unknown = false, conflict = false } = {}) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  let creates = 0;
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (String(url).includes('?')) return Response.json({ items: [builtin, custom], next_cursor: null });
    if (init?.method === 'POST') {
      creates++;
      if (unknown && creates === 1) throw new TypeError('lost reply');
      return Response.json({ ...custom, ...JSON.parse(String(init.body)), preset_id: 'new' });
    }
    if (init?.method === 'PUT')
      return conflict
        ? Response.json({ error: { code: 'concurrency:version_conflict' } }, { status: 409 })
        : Response.json({ ...custom, ...JSON.parse(String(init.body)), revision: 2, version: 'v2' });
    if (init?.method === 'DELETE') return new Response(null, { status: 204 });
    return Response.json(String(url).endsWith('small') ? builtin : custom);
  });
  render(wrap(<GenerationPresets />));
  return fetcher;
}

it('validates integer multiples and all three orientations without rounding', () => {
  const raw = {
    landscape: { width: '768', height: '512' },
    portrait: { width: '512', height: '768' },
    square: { width: '640', height: '640' },
  };
  expect(presetPayload('  new  ', raw)).toEqual({ name: 'new', sizes: SMALL_SIZES });
  for (const value of ['', '65', '768.5', '1e3', '-64', '16448'])
    expect(() => presetPayload('new', { ...raw, landscape: { width: value, height: '512' } })).toThrow();
  expect(() => presetPayload('new', { ...raw, square: { width: '768', height: '640' } })).toThrow();
  expect(() => presetPayload('new', { ...raw, portrait: raw.landscape })).toThrow();
});

it('keeps builtins read-only and allows copying their actual dimensions', async () => {
  const fetcher = mount();
  await screen.findByRole('option', { name: '小（内置，只读）' });
  change('打开尺寸预设', 'small');
  await waitFor(() => expect(screen.getByLabelText('预设名称')).toHaveValue('小'));
  expect(screen.getByLabelText('横向宽度')).toBeDisabled();
  expect(screen.getByRole('button', { name: '删除尺寸预设' })).toBeDisabled();
  click('复制为自定义预设');
  expect(screen.getByLabelText('预设名称')).toHaveValue('小 副本');
  expect(screen.getByLabelText('横向宽度')).toBeEnabled();
  click('保存尺寸预设');
  await screen.findByText(/尺寸预设已保存/);
  const sent = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(JSON.parse(String(sent[1]?.body))).toEqual({ name: '小 副本', sizes: SMALL_SIZES });
});

it('retains a dirty form after response loss and reuses the creation request key', async () => {
  const fetcher = mount({ unknown: true });
  change('预设名称', '保留草稿');
  click('保存尺寸预设');
  await screen.findByRole('alert');
  expect(screen.getByLabelText('预设名称')).toHaveValue('保留草稿');
  click('保存尺寸预设');
  await screen.findByText(/尺寸预设已保存/);
  const calls = fetcher.mock.calls.filter(([, init]) => init?.method === 'POST');
  expect(calls).toHaveLength(2);
  expect(calls[0][1]?.headers).toEqual(calls[1][1]?.headers);
});

it('preserves fields after a version conflict and guards navigation away from a dirty form', async () => {
  mount({ conflict: true });
  await screen.findByRole('option', { name: /自定义 · 版本/ });
  change('打开尺寸预设', 'custom');
  await waitFor(() => expect(screen.getByLabelText('预设名称')).toHaveValue('自定义'));
  change('预设名称', '冲突草稿');
  click('保存尺寸预设');
  await screen.findByRole('alert');
  expect(screen.getByLabelText('预设名称')).toHaveValue('冲突草稿');
  click('新建尺寸预设');
  expect(screen.getByRole('dialog')).toHaveTextContent('有未保存的尺寸预设');
  click('继续编辑');
  expect(screen.getByLabelText('预设名称')).toHaveValue('冲突草稿');
});

it('requires explicit acknowledgement before held deletion and sends the observed version', async () => {
  const fetcher = mount();
  await screen.findByRole('option', { name: /自定义 · 版本/ });
  change('打开尺寸预设', 'custom');
  await waitFor(() => expect(screen.getByLabelText('预设名称')).toHaveValue('自定义'));
  click('删除尺寸预设');
  const modal = screen.getByRole('dialog');
  expect(within(modal).getByRole('button', { name: /按住删除预设/ })).toBeDisabled();
  fireEvent.click(screen.getByLabelText('确认删除这份自定义尺寸预设'));
  vi.useFakeTimers();
  fireEvent.pointerDown(within(modal).getByRole('button', { name: /按住删除预设/ }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100);
  });
  vi.useRealTimers();
  await screen.findByText(/尺寸预设已删除/);
  const sent = fetcher.mock.calls.find(([, init]) => init?.method === 'DELETE')!;
  expect(sent[1]?.headers).toMatchObject({ 'If-Match': '"v1"' });
});

it('refuses a changed picker entry and leaves the current source draft untouched', async () => {
  setSessionToken('fixture');
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (path) =>
    Response.json(
      String(path).includes('?') ? { items: [custom], next_cursor: null } : { ...custom, version: 'v2' },
    ),
  );
  const selected = vi.fn(),
    failure = vi.fn();
  render(
    wrap(
      <PresetPicker
        disabled={false}
        onSelect={selected}
        action={async (work) => {
          try {
            await work();
          } catch (error) {
            failure(error);
          }
        }}
      />,
    ),
  );
  await screen.findByRole('option', { name: /自定义 · 横向/ });
  change('填入尺寸预设', 'custom');
  await waitFor(() => expect(failure).toHaveBeenCalledOnce());
  expect(selected).not.toHaveBeenCalled();
});
