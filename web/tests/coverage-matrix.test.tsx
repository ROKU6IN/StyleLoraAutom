import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import CoverageMatrix, {
  matrixCell,
  detailPath,
  type Matrix,
  type MatrixSource,
} from '../src/features/CoverageMatrix';
import { CoverageReport } from '../src/features/PromptProduction';
import { queryClient } from '../src/state/queryClient';
import { setSessionToken } from '../src/api/client';

const matrix: Matrix = {
  enabled: true,
  primary_dimensions: ['scene', 'lighting'],
  cell_values: { scene: ['forest', 'beach'], lighting: ['sunlight', 'night'] },
};
const cell = (scene: string, lighting: string) => matrixCell(matrix.primary_dimensions, { scene, lighting });
const targets = {
  [cell('forest', 'sunlight')]: 5,
  [cell('forest', 'night')]: 3,
  [cell('beach', 'sunlight')]: 2,
};
const counts = {
  cell_targets: targets,
  cell_counts: { [cell('forest', 'sunlight')]: 5, [cell('forest', 'night')]: 1 },
};
const source: MatrixSource = {
  kind: 'preview',
  preview_id: 'preview-id',
  digest: 'a'.repeat(64),
  expires_at: 'later',
};
afterEach(() => {
  queryClient.clear();
  setSessionToken(null);
  vi.restoreAllMocks();
});
function mount(meta = matrix, value = counts, ref: MatrixSource | undefined = source) {
  setSessionToken('fixture');
  return render(
    <QueryClientProvider client={queryClient}>
      <CoverageMatrix matrix={meta} counts={value} source={ref} />
    </QueryClientProvider>,
  );
}
const change = (name: string, value: string) =>
  fireEvent.change(screen.getByLabelText(name), { target: { value } });
const button = (name: string | RegExp) => screen.getByRole('button', { name });

it('shows explicit targets, actual counts, deficits and excluded cells without fetching on render', () => {
  const fetcher = vi.spyOn(globalThis, 'fetch');
  mount();
  expect(button(/scene: forest · lighting: sunlight：目标 5，实际 5，满足格子配额/)).toBeEnabled();
  expect(button(/scene: forest · lighting: night：目标 3，实际 1，缺口 2/)).toBeEnabled();
  expect(button(/scene: beach · lighting: night：已排除/)).toBeDisabled();
  expect(screen.getByText(/格子满足不代表整体比例约束/)).toBeVisible();
  expect(fetcher).not.toHaveBeenCalled();
});

it('swaps axes while preserving the original canonical cell identity', async () => {
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(Response.json({ items: [], total: 0, next_cursor: null }));
  mount();
  change('矩阵行维度', 'lighting');
  expect(screen.getByLabelText('矩阵列维度')).toHaveValue('scene');
  fireEvent.click(button(/scene: forest · lighting: night：/));
  await screen.findByText('本格尚未选入条目。');
  const url = new URL(String(fetcher.mock.calls[0][0]), 'http://localhost');
  expect(url.searchParams.get('cell')).toBe(cell('forest', 'night'));
});

it('filters the third dimension and closes the previous slice details', async () => {
  const meta = {
    ...matrix,
    primary_dimensions: [...matrix.primary_dimensions, 'view'],
    cell_values: { ...matrix.cell_values, view: ['wide shot', 'upper body'] },
  };
  const keys = meta.cell_values.view.map((view) =>
    matrixCell(meta.primary_dimensions, { scene: 'forest', lighting: 'sunlight', view }),
  );
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ items: [], total: 0, next_cursor: null }));
  mount(meta, { cell_targets: Object.fromEntries(keys.map((key) => [key, 1])), cell_counts: {} });
  fireEvent.click(button(/view: wide shot：目标/));
  await screen.findByRole('region', { name: '格子已选条目' });
  change('筛选维度 view', 'upper body');
  expect(screen.queryByRole('region', { name: '格子已选条目' })).not.toBeInTheDocument();
  expect(button(/view: upper body：目标/)).toBeEnabled();
  change('矩阵列维度', 'view');
  expect(screen.getByLabelText('筛选维度 lighting')).toHaveValue('sunlight');
});

it('bounds a large matrix to 100 rendered cells and pages rows and columns', () => {
  const values = Array.from({ length: 21 }, (_, i) => 'tag ' + i);
  mount(
    { enabled: true, primary_dimensions: ['row', 'col'], cell_values: { row: values, col: values } },
    { cell_targets: {}, cell_counts: {} },
  );
  expect(within(screen.getByRole('table')).getAllByRole('button')).toHaveLength(100);
  fireEvent.click(button('矩阵下一组行'));
  fireEvent.click(button('矩阵下一组列'));
  expect(screen.getByText('行 11–20 / 21；列 11–20 / 21')).toBeVisible();
  fireEvent.click(button('矩阵下一组行'));
  expect(within(screen.getByRole('table')).getAllByRole('button')).toHaveLength(10);
  expect(button('矩阵下一组行')).toBeDisabled();
});

it('pages complete selected entries and retains null rating and aspect semantics', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = new URL(String(input), 'http://localhost');
    const second = url.searchParams.has('cursor');
    return Response.json({
      items: [
        {
          index: second ? 101 : 0,
          cell: cell('forest', 'sunlight'),
          entry: {
            tags: ['forest'],
            natural_language: second ? 'Beyond the first hundred.' : null,
            aspect: null,
          },
          rating: null,
          verified: false,
        },
      ],
      total: 205,
      next_cursor: second ? null : 'cursor-one',
    });
  });
  mount();
  fireEvent.click(button(/scene: forest · lighting: sunlight：/));
  await screen.findByText('本格共 205 条 · 明细第 1 页');
  expect(screen.getByText('未知 · 未验证')).toBeVisible();
  fireEvent.click(button('格子明细下一页'));
  await screen.findByText('Beyond the first hundred.');
  expect(button('格子明细下一页')).toBeDisabled();
  expect(new URL(String(fetcher.mock.calls[1][0]), 'http://localhost').searchParams.get('cursor')).toBe(
    'cursor-one',
  );
  fireEvent.click(button('格子明细上一页'));
  await screen.findByText('本格共 205 条 · 明细第 1 页');
});

it('shows expired preview errors without silently regenerating or presenting a truncated fallback', async () => {
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(Response.json({ error: { code: 'preview:expired' } }, { status: 410 }));
  mount();
  fireEvent.click(button(/scene: forest · lighting: sunlight：/));
  expect(await screen.findByRole('alert')).toHaveTextContent('预览已过期');
  expect(screen.queryByText('本格尚未选入条目。')).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][1]?.method).toBe('GET');
});

it('releases only the preview detail view and keeps report counts visible', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ released: true }));
  mount();
  fireEvent.click(button('释放本次格子明细'));
  await screen.findByText('格子明细已释放；报告仍可查看，需要明细时请重新预览。');
  expect(button(/scene: forest · lighting: sunlight：目标 5/)).toBeDisabled();
  expect(fetcher.mock.calls[0][1]?.method).toBe('DELETE');
  expect(String(fetcher.mock.calls[0][0])).toContain('coverage/previews/preview-id?digest=');
});

it('an unknown release can be retried and does not claim success prematurely', async () => {
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockRejectedValueOnce(new TypeError('lost'))
    .mockResolvedValue(Response.json({ released: true }));
  mount();
  fireEvent.click(button('释放本次格子明细'));
  await screen.findByRole('alert');
  expect(button('释放本次格子明细')).toBeEnabled();
  fireEvent.click(button('释放本次格子明细'));
  await screen.findByText('格子明细已释放；报告仍可查看，需要明细时请重新预览。');
  expect(fetcher.mock.calls[0][0]).toBe(fetcher.mock.calls[1][0]);
});

it('random mode uses an explicit single pool and never offers meaningless dimensions', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ items: [], total: 0, next_cursor: null }));
  mount(
    { enabled: false, primary_dimensions: [], cell_values: {} },
    { cell_targets: { random: 2 }, cell_counts: {} },
  );
  expect(screen.queryByLabelText('矩阵行维度')).not.toBeInTheDocument();
  fireEvent.click(button('随机池：目标 2，实际 0，缺口 2'));
  await screen.findByRole('heading', { name: '随机池' });
});

it('task revision changes close old detail pages and require an explicit new cell selection', async () => {
  const fetcher = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(Response.json({ items: [], total: 0, next_cursor: null }));
  const value = {
    matrix,
    ...counts,
    status: 'unsatisfiable',
    ready: false,
    requested: 10,
    available: 6,
    count: 6,
    constraint_counts: {},
    constraint_bounds: {},
    diagnostics: {},
    warnings: {},
    source_counts: {},
    complete_pool: true,
    search_nodes: 0,
  };
  const wrapper = (revision: number) => (
    <QueryClientProvider client={queryClient}>
      <CoverageReport value={value} source={{ kind: 'task', task_id: 'task-id', revision }} />
    </QueryClientProvider>
  );
  setSessionToken('fixture');
  const view = render(wrapper(3));
  fireEvent.click(button(/scene: forest · lighting: sunlight：/));
  await screen.findByRole('region', { name: '格子已选条目' });
  view.rerender(wrapper(4));
  await waitFor(() => expect(screen.queryByRole('region', { name: '格子已选条目' })).not.toBeInTheDocument());
  fireEvent.click(button(/scene: forest · lighting: sunlight：/));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  expect(String(fetcher.mock.calls[1][0])).toContain('expected_revision=4');
  expect(screen.queryByRole('button', { name: '释放本次格子明细' })).not.toBeInTheDocument();
});

it('encodes special dimension and tag names without delimiter collisions or URL fragments', () => {
  const key = matrixCell(['场景#', 'a,b'], { '场景#': '海边&树林', 'a,b': 'quote"value' });
  const path = detailPath({ kind: 'task', task_id: 'id', revision: 7 }, key, null);
  expect(new URL(path, 'http://localhost').searchParams.get('cell')).toBe(key);
  expect(path).not.toContain('#');
  expect(JSON.parse(key)).toEqual([
    ['场景#', '海边&树林'],
    ['a,b', 'quote"value'],
  ]);
});

it('treats special third-dimension names as own data keys', () => {
  const meta: Matrix = JSON.parse(
    '{"enabled":true,"primary_dimensions":["scene","lighting","__proto__"],"cell_values":{"scene":["forest"],"lighting":["sunlight"],"__proto__":["wide shot","upper body"]}}',
  );
  mount(meta, { cell_targets: {}, cell_counts: {} });
  expect(screen.getByLabelText('筛选维度 __proto__')).toHaveValue('wide shot');
  change('筛选维度 __proto__', 'upper body');
  expect(button(/__proto__: upper body：已排除/)).toBeDisabled();
});
