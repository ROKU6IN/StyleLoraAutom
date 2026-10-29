import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { request, setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';
import { BatchDialog } from '../src/features/cleaning/BatchDialog';
import { TagSuggestions } from '../src/features/cleaning/TagSuggestions';

afterEach(() => {
  vi.restoreAllMocks();
  setSessionToken(null);
});
function host(node: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

it('uploads FormData without forcing a JSON content type and keeps the supplied retry key', async () => {
  setSessionToken('test-token');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}'));
  const body = new FormData();
  body.append('file', new Blob(['{}']), 'catalog.json');
  await request('POST', '/api/tags/imports/preview', body, { key: 'stable-key' });
  const options = fetcher.mock.calls[0][1]!;
  expect(options.body).toBe(body);
  expect(options.headers).toEqual({ Authorization: 'Bearer test-token', 'Idempotency-Key': 'stable-key' });
});

it('does not retry an ambiguous product write automatically', async () => {
  setSessionToken('test-token');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('network'));
  await expect(
    request('PUT', '/api/translations', { tag: 'cat', value: '猫' }, { key: 'stable', version: 'old' }),
  ).rejects.toMatchObject({ code: 'request:unknown' });
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][1]?.headers).toMatchObject({
    'Idempotency-Key': 'stable',
    'If-Match': '"old"',
  });
});

it('invalidates the entire batch preview when input changes and cancel never applies', async () => {
  setSessionToken('test-token');
  useSession.getState().setConnection('online');
  const close = vi.fn();
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(
      JSON.stringify({
        preview_id: 'id',
        digest: 'digest',
        version: 'version',
        total: 1000,
        changed_count: 999,
        examples: [],
        issues: [],
      }),
    ),
  );
  host(<BatchDialog pid="project" kind="bulk" scope={{ filter: {} }} onClose={close} />);
  fireEvent.change(screen.getByLabelText('批量新增标签'), { target: { value: 'one' } });
  fireEvent.click(screen.getByRole('button', { name: '生成变更预览' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '确认应用' })).toBeEnabled());
  expect(screen.getByRole('region', { name: '变更预览' })).toHaveTextContent('范围 1000 张');
  fireEvent.change(screen.getByLabelText('批量新增标签'), { target: { value: 'two' } });
  expect(screen.getByRole('button', { name: '确认应用' })).toBeDisabled();
  expect(screen.queryByRole('region', { name: '变更预览' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(close).toHaveBeenCalledOnce();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('selecting an alias suggestion adds only its canonical tag, never the translation', async () => {
  setSessionToken('test-token');
  const add = vi.fn();
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(
      JSON.stringify({
        available: true,
        items: [{ canonical: 'blue hair', translation: '蓝发', match: 'alias' }],
      }),
    ),
  );
  host(<TagSuggestions onAdd={add} />);
  fireEvent.change(screen.getByLabelText('从本地标签库补全'), { target: { value: 'azure' } });
  fireEvent.click(await screen.findByRole('button', { name: 'blue hair · 蓝发' }));
  expect(add).toHaveBeenCalledWith('blue hair');
  expect(screen.getByLabelText('从本地标签库补全')).toHaveValue('');
});
