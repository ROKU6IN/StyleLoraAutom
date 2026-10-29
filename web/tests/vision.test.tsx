import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import VisionJobs from '../src/features/VisionJobs';
import { parseKeywords } from '../src/features/VisionPolicy';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { useSelection } from '../src/state/selection';
import { setSessionToken } from '../src/api/client';

afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});
function mount(available = true) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  useSelection.getState().reset('p');
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    if (init?.method === 'POST') throw new TypeError('unknown fixture submit');
    return new Response(
      JSON.stringify(
        String(path).includes('capabilities')
          ? { llm_vision: { available } }
          : { items: [], has_more: false },
      ),
    );
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <VisionJobs pid="p" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return fetcher;
}
it('requires a configured visual role and never submits on page load', async () => {
  const fetcher = mount(false);
  expect(await screen.findByText(/请先配置支持图片/)).toBeVisible();
  expect(screen.getByRole('button', { name: '开始视觉描述' })).toBeDisabled();
  expect(fetcher.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true);
});
it('preserves ambiguous batch identity and freezes explicit routing choices', async () => {
  const fetcher = mount();
  const start = screen.getByRole('button', { name: '开始视觉描述' });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.change(screen.getByLabelText('评级无法确定时'), { target: { value: 'nsfwfallback' } });
  fireEvent.click(screen.getByLabelText('关键词未命中时调用分级模型'));
  fireEvent.click(start);
  expect(await screen.findByRole('alert')).toHaveTextContent('本次提交结果未知');
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  await waitFor(() => expect(start).toBeEnabled());
  const bodies = fetcher.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([, init]) => JSON.parse(init!.body as string));
  expect(bodies).toHaveLength(2);
  expect(bodies[0].batch_id).toBe(bodies[1].batch_id);
  expect(bodies[0].config).toMatchObject({
    dataset_mode_policy: 'style',
    fallback: 'nsfwfallback',
    use_rating_model: true,
  });
});
it('validates keyword rows without accepting ambiguous delimiters or unknown ratings', () => {
  expect(parseKeywords('blue_hair\tsensitive\n')).toEqual([{ tag: 'blue_hair', rating: 'sensitive' }]);
  expect(parseKeywords('')).toEqual([]);
  for (const value of ['tag,general', 'tag\tunknown', '\tgeneral', 'tag\tgeneral\textra'])
    expect(() => parseKeywords(value)).toThrow();
});
