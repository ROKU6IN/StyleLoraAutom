import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import { DictionaryDownload, ImportRows, type ImportPreview } from '../src/features/DictionaryExchange';
import { queryClient } from '../src/state/queryClient';
import { request, setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
const base = {
  dictionary_id: 'fixture',
  name: '词典',
  revision: 2,
  version: 'v2',
  kind: 'features' as const,
};
const action = async (work: () => Promise<void>) => {
  try {
    await work();
  } catch {
    /* Parent displays errors. */
  }
};
const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));
const preview: ImportPreview = {
  preview_id: 'preview',
  digest: 'd'.repeat(64),
  valid: false,
  count: 2,
  row_count: 3,
  selectable_count: 2,
  selection_available: true,
  selection: null,
  issue_count: 1,
  issues: [],
  examples: [],
};

it('requires TXT loss acknowledgement, downloads a binary archive and resets acknowledgement on reopen', async () => {
  setSessionToken('fixture');
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(
      new Response(new Uint8Array([80, 75, 3, 4]), { headers: { 'Content-Type': 'application/zip' } }),
    );
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  const objectUrl = vi.spyOn(URL, 'createObjectURL');
  render(<DictionaryDownload base={base} disabled={false} busy={false} action={action} />);
  click('导出词典 TXT');
  expect(screen.getByRole('dialog')).toHaveTextContent('评级');
  expect(screen.getByRole('button', { name: '下载维度 TXT 压缩包' })).toBeDisabled();
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText('确认接受词典 TXT 导出的信息损失'));
  click('取消');
  click('导出词典 TXT');
  expect(screen.getByLabelText('确认接受词典 TXT 导出的信息损失')).not.toBeChecked();
  fireEvent.click(screen.getByLabelText('确认接受词典 TXT 导出的信息损失'));
  click('下载维度 TXT 压缩包');
  await waitFor(() => expect(download).toHaveBeenCalledOnce());
  expect(fetcher.mock.calls[0][0]).toContain('format=txt&acknowledge_lossy=true');
  expect(fetcher.mock.calls[0][1]?.headers).toMatchObject({
    Authorization: 'Bearer fixture',
    'If-Match': '"v2"',
  });
  expect(objectUrl.mock.calls.at(-1)?.[0]).toMatchObject({ size: 4, type: 'application/zip' });
  expect((download.mock.instances[0] as HTMLAnchorElement).download).toBe('dictionary-fixture.zip');
});

it('shows a version conflict inside the export modal without downloading', async () => {
  setSessionToken('fixture');
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ error: { code: 'concurrency:version_conflict' } }), { status: 409 }),
  );
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  render(
    <DictionaryDownload base={{ ...base, kind: 'prompts' }} disabled={false} busy={false} action={action} />,
  );
  click('导出词典 TXT');
  expect(screen.getByRole('dialog')).toHaveTextContent('图幅、评级');
  fireEvent.click(screen.getByLabelText('确认接受词典 TXT 导出的信息损失'));
  click('下载提示词 TXT');
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert')).toBeVisible());
  expect(download).not.toHaveBeenCalled();
});

it('keeps selections across pages and never includes invalid rows in a new preview', async () => {
  setSessionToken('fixture');
  const onPreview = vi.fn();
  const valid = (row: number) => ({
    row,
    original: null,
    value: { tag: 'item ' + row, rating: null },
    issues: [],
  });
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    if (init?.method === 'POST')
      return new Response(JSON.stringify({ ...preview, preview_id: 'new', valid: true }));
    const next = String(path).includes('cursor=next');
    return new Response(
      JSON.stringify({
        items: next
          ? [valid(52)]
          : [valid(2), { row: 3, original: 'bad', value: null, issues: [{ code: 'prompt:invalid_row' }] }],
        total: 3,
        limit: 50,
        has_more: !next,
        next_cursor: next ? null : 'next',
      }),
    );
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ImportRows preview={preview} disabled={false} action={action} onPreview={onPreview} />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByText('选择有效行并重新预览'));
  expect(await screen.findByLabelText('选择导入记录 3')).toBeDisabled();
  click('选择本页有效行');
  click('下一页导入记录');
  expect(await screen.findByLabelText('选择导入记录 52')).toBeEnabled();
  fireEvent.click(screen.getByLabelText('选择导入记录 52'));
  click('上一页导入记录');
  expect(await screen.findByLabelText('选择导入记录 2')).toBeChecked();
  click('生成所选行新预览');
  await waitFor(() => expect(onPreview).toHaveBeenCalledOnce());
  const submitted = fetcher.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(JSON.parse(submitted[1]?.body as string)).toEqual({
    preview_id: preview.preview_id,
    digest: preview.digest,
    rows: [2, 52],
  });
});

it('binary downloads still use authentication and the structured API error path', async () => {
  setSessionToken('fixture');
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(new Response(JSON.stringify({ error: { code: 'auth:required' } }), { status: 401 }));
  await expect(request<Blob>('GET', '/api/file', undefined, { responseType: 'blob' })).rejects.toMatchObject({
    status: 401,
  });
  await expect(request<Blob>('GET', '/api/file', undefined, { responseType: 'blob' })).rejects.toMatchObject({
    status: 401,
  });
  expect(fetcher).toHaveBeenCalledOnce();
});
