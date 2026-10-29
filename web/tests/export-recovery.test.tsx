import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import ExportRecovery from '../src/features/ExportRecovery';
import { setSessionToken } from '../src/api/client';
import { useSession } from '../src/state/session';
import { queryClient } from '../src/state/queryClient';

afterEach(() => {
  vi.restoreAllMocks();
  setSessionToken(null);
  queryClient.clear();
});
function form(canPublish = true) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const state = { lost: false, moved: false };
  const preview = {
    preview_id: 'preview',
    digest: 'digest',
    version: 'version',
    decisions: [],
    candidates: [
      {
        candidate_id: 'candidate',
        directory: 'exports/1/.partial-fixture',
        files: 3,
        bytes: 2048,
        can_publish: canPublish,
        publish_directory: 'exports/1/fixture',
        reason: null,
      },
    ],
  };
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const path = String(url);
    if (path.endsWith('/actions') && options?.method === 'POST') {
      state.moved = true;
      if (state.lost) throw new TypeError('lost reply');
      return Response.json({ status_url: '/api/operations/action' }, { status: 202 });
    }
    if (path.endsWith('/scan') && options?.method === 'POST')
      return Response.json({ status_url: '/api/operations/scan' }, { status: 202 });
    return Response.json({
      status: 'done',
      result: path.endsWith('/action')
        ? { directory: 'exports/1/fixture' }
        : state.moved
          ? { ...preview, candidates: [] }
          : preview,
    });
  });
  render(<ExportRecovery pid="p" />);
  return {
    state,
    fetcher,
    writes: () => fetcher.mock.calls.filter(([url]) => String(url).endsWith('/actions')),
  };
}
async function select() {
  fireEvent.click(screen.getByRole('button', { name: '检查中断导出与处置记录' }));
  await screen.findByText(/exports\/1\/\.partial-fixture/);
  fireEvent.click(screen.getByRole('checkbox', { name: '我已核对这个目录及处置方式' }));
}
async function hold(name: string) {
  const button = screen.getByRole('button', { name: new RegExp(name) });
  fireEvent.keyDown(button, { key: ' ' });
  await waitFor(() => expect(screen.queryByRole('checkbox')).not.toBeInTheDocument(), { timeout: 2500 });
  fireEvent.keyUp(button, { key: ' ' });
}
it('does not scan on mount and publishes only after explicit check and hold', async () => {
  const { fetcher, writes } = form();
  expect(fetcher).not.toHaveBeenCalled();
  await select();
  await hold('发布完整导出');
  expect(await screen.findByText(/已发布完整导出/)).toBeVisible();
  expect(writes()).toHaveLength(1);
  const options = writes()[0][1]!;
  expect(new Headers(options.headers).get('If-Match')).toBe('"version"');
  expect(JSON.parse(options.body as string)).toMatchObject({
    candidate_id: 'candidate',
    action: 'publish',
    confirm: true,
  });
});
it('incomplete evidence offers archive only', async () => {
  const { writes } = form(false);
  await select();
  expect(screen.queryByRole('button', { name: '发布完整导出' })).not.toBeInTheDocument();
  await hold('归档保留目录');
  expect(JSON.parse(writes()[0][1]!.body as string).action).toBe('archive');
});
it('lost write response blocks another action until a fresh explicit scan', async () => {
  const { state, writes } = form();
  state.lost = true;
  await select();
  const button = screen.getByRole('button', { name: /发布完整导出/ });
  fireEvent.keyDown(button, { key: ' ' });
  await screen.findByText(/处置结果未知/, {}, { timeout: 2500 });
  fireEvent.keyUp(button, { key: ' ' });
  expect(button).toBeDisabled();
  expect(writes()).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '检查中断导出与处置记录' }));
  await screen.findByText('未发现待处置目录。');
  expect(writes()).toHaveLength(1);
});
