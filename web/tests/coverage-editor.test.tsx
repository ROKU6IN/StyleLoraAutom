import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import CoverageFormEditor from '../src/features/CoverageFormEditor';
import CoveragePlans from '../src/features/CoveragePlans';
import {
  editorCells,
  renameDimension,
  type CoverageForm,
  type EditorView,
} from '../src/features/coverageEditorData';
import { DraftProvider } from '../src/hooks/drafts';
import { queryClient } from '../src/state/queryClient';
import { useSession } from '../src/state/session';
import { setSessionToken } from '../src/api/client';

const base: CoverageForm = {
  schema_version: 1,
  name: '编辑样例',
  plan: {
    schema_version: 1,
    algorithm_version: 'coverage-v1',
    primary_dimensions: ['scene', 'lighting'],
    cell_values: { scene: ['forest', 'beach'], lighting: ['sunlight', 'night'] },
    cell_weights: {},
    exclude: [],
    no_humans_excludes: [],
    constraints: [],
    target_count: '12',
    random_seed: '18446744073709551615',
  },
};
const key = '[["scene","forest"],["lighting","sunlight"]]';
const button = (name: string | RegExp) => screen.getByRole('button', { name });
const click = (name: string | RegExp) => fireEvent.click(button(name));
const change = (name: string, value: string) =>
  fireEvent.change(screen.getByLabelText(name), { target: { value } });
afterEach(() => {
  queryClient.clear();
  vi.restoreAllMocks();
  setSessionToken(null);
});

function formMount(initial = base, disabled = false) {
  let value = initial;
  function Harness() {
    const [form, setForm] = useState(initial);
    value = form;
    return <CoverageFormEditor form={form} disabled={disabled} onChange={setForm} />;
  }
  render(<Harness />);
  return () => value;
}

it('edits exact numeric strings and preserves unfinished ratio input', () => {
  const value = formMount();
  expect(screen.getByLabelText('方案随机种子')).toHaveValue('18446744073709551615');
  change('方案随机种子', '18446744073709551614');
  click('新增整体约束');
  change('约束标签', 'no humans');
  change('最小比例', '0.');
  change('最多条数', '0');
  expect(value().plan.random_seed).toBe('18446744073709551614');
  expect(value().plan.constraints[0]).toMatchObject({
    minimum_ratio: '0.',
    maximum_count: '0',
    tag: 'no humans',
  });
  click('删除当前整体约束');
  expect(value().plan.constraints).toEqual([]);
});

it('edits weights, explicitly removes obsolete weights, and never silently loses them on value edits', () => {
  const value = formMount();
  click('编辑权重 scene: forest · lighting: sunlight');
  change('当前格子权重', '7');
  expect(value().plan.cell_weights).toEqual({ [key]: '7' });
  change('主维度 1 名称', 'location');
  expect(value().plan.cell_weights).toEqual({ [key.replace('scene', 'location')]: '7' });
  change('主维度 1 取值（每行一个）', 'beach');
  expect(screen.getByText(/有 1 项权重对应的格子已不存在/)).toBeVisible();
  expect(Object.values(value().plan.cell_weights)).toEqual(['7']);
  click('清理已失效格子权重');
  expect(value().plan.cell_weights).toEqual({});
});

it('adds and deletes the third dimension with confirmation while preserving stale weights', () => {
  const value = formMount({ ...base, plan: { ...base.plan, cell_weights: { [key]: '9' } } });
  click('新增第三维度');
  change('主维度 3 名称', 'view');
  change('主维度 3 取值（每行一个）', 'far\nnear');
  change('权重行维度', '2');
  expect(screen.getByLabelText('权重筛选 scene')).toBeVisible();
  click('删除主维度 3');
  expect(value().plan.primary_dimensions).toHaveLength(3);
  click('保留主维度');
  expect(value().plan.primary_dimensions).toHaveLength(3);
  click('删除主维度 3');
  click('确认删除主维度');
  expect(value().plan.primary_dimensions).toEqual(['scene', 'lighting']);
  expect(value().plan.cell_weights).toEqual({ [key]: '9' });
});

it('adds an explicit tag predicate from a cell and edits it without changing weights', () => {
  const value = formMount();
  click('编辑权重 scene: forest · lighting: sunlight');
  change('当前格子权重', '3');
  click('按当前格子标签新增排除条件');
  expect(screen.getByLabelText('排除：必须包含（每行一个）')).toHaveValue('forest\nsunlight');
  change('排除：不得包含（每行一个）', '1girl');
  expect(value().plan.exclude).toEqual([{ all_tags: ['forest', 'sunlight'], none_tags: ['1girl'] }]);
  expect(value().plan.cell_weights[key]).toBe('3');
  click('恢复当前格子默认权重');
  expect(value().plan.cell_weights).toEqual({});
  click('删除当前排除条件');
  click('新增排除条件');
  expect(screen.getByText(/两个标签列表都为空会排除全部/)).toBeVisible();
});

it('bounds the rendered matrix and edits the final page with canonical cell identity', () => {
  const value = formMount({
    ...base,
    plan: {
      ...base.plan,
      cell_values: {
        scene: Array.from({ length: 21 }, (_, i) => 'place ' + i),
        lighting: Array.from({ length: 12 }, (_, i) => 'light ' + i),
      },
    },
  });
  expect(screen.getAllByRole('button', { name: /^编辑权重/ })).toHaveLength(100);
  click('权重下一组行');
  click('权重下一组行');
  click('权重下一组列');
  expect(screen.getAllByRole('button', { name: /^编辑权重/ })).toHaveLength(2);
  click('编辑权重 scene: place 20 · lighting: light 11');
  change('当前格子权重', '99');
  expect(value().plan.cell_weights['[["scene","place 20"],["lighting","light 11"]]']).toBe('99');
});

it('preserves special dimension keys and rejects duplicate dimension names and normalized values', () => {
  const renamed = renameDimension(base.plan, 0, '__proto__');
  expect(renamed.cell_values['__proto__']).toEqual(['forest', 'beach']);
  expect(editorCells(renamed)?.keys[0]).toBe('[["__proto__","forest"],["lighting","sunlight"]]');
  expect(() => renameDimension(base.plan, 0, 'lighting')).toThrow('维度名称不能重复');
  expect(
    editorCells({ ...base.plan, cell_values: { scene: ['Forest', 'forest'], lighting: ['night'] } }),
  ).toBeNull();
});

it('disables form mutations while busy or read only', () => {
  formMount(base, true);
  expect(screen.getByLabelText('方案名称')).toBeDisabled();
  expect(button('新增整体约束')).toBeDisabled();
  expect(button('编辑权重 scene: forest · lighting: sunlight')).toBeDisabled();
});

// Server fixtures intentionally keep uint64 inside a string or raw template text.
function view(form = base): EditorView {
  const plan = form.plan;
  const template = JSON.stringify({
    ...form,
    plan: { ...plan, target_count: Number(plan.target_count), random_seed: '__SEED__' },
  }).replace('"__SEED__"', plan.random_seed || 'null');
  return {
    form,
    template,
    name: form.name,
    primary_dimensions: plan.primary_dimensions,
    cell_count: 4,
    target_count: Number(plan.target_count),
    random_seed: plan.random_seed || null,
    browser_compatible: false,
    layout: {
      active_cells: editorCells(plan)!.keys,
      cell_targets: Object.fromEntries(editorCells(plan)!.keys.map((key) => [key, 3])),
      issues: [],
      constraint_bounds: {},
      seed_required: false,
    },
  };
}

function parentMount(
  options: { failSave?: boolean; invalid?: boolean; conflict?: boolean; newerReadback?: boolean } = {},
) {
  setSessionToken('fixture');
  useSession.getState().setConnection('online');
  const writes: { body: Record<string, unknown>; headers: HeadersInit }[] = [];
  let saved = { ...view(), coverage_id: 'coverage', revision: 1, version: 'v1' };
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, init) => {
    const url = String(path);
    if (url.endsWith('/editor'))
      return Response.json(options.newerReadback && writes.length ? saved : view());
    if (url.endsWith('/editor/preview'))
      return options.invalid
        ? Response.json({ error: { code: 'coverage:invalid_editor' } }, { status: 422 })
        : Response.json(view(JSON.parse(init!.body as string)));
    if (init?.method === 'POST' || init?.method === 'PUT') {
      writes.push({ body: JSON.parse(init.body as string), headers: init.headers! });
      if (options.failSave) throw new TypeError('lost response');
      if (options.conflict)
        return Response.json({ error: { code: 'concurrency:version_conflict' } }, { status: 409 });
      saved = { ...saved, template: writes.at(-1)!.body.content as string, revision: 2, version: 'v2' };
      if (options.newerReadback && writes.length === 1)
        saved = {
          ...view({ ...base, name: '保存后另一窗口更新', plan: { ...base.plan, target_count: '30' } }),
          coverage_id: 'coverage',
          revision: 3,
          version: 'v3',
        };
      return Response.json(saved);
    }
    if (url.includes('?')) return Response.json({ items: [saved], total: 1, next_cursor: null });
    return Response.json(saved);
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DraftProvider>
          <CoveragePlans />
        </DraftProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return writes;
}

it('invalidates stale quotas on editing and switches back with the exact server template', async () => {
  parentMount();
  click('使用表单编辑');
  await screen.findByLabelText('方案名称');
  expect(screen.queryByLabelText('覆盖方案模板')).not.toBeInTheDocument();
  expect(screen.getAllByText('计划配额 3')).toHaveLength(4);
  change('方案随机种子', '18446744073709551614');
  expect(screen.queryByText('计划配额 3')).not.toBeInTheDocument();
  click('切换到模板文本');
  const text = await screen.findByLabelText('覆盖方案模板');
  expect((text as HTMLTextAreaElement).value).toContain('18446744073709551614');
});

it('retains the form and request identity on unknown save, rotating identity after an edit', async () => {
  const writes = parentMount({ failSave: true });
  click('使用表单编辑');
  await screen.findByLabelText('方案名称');
  change('方案名称', '精确种子草稿');
  for (let i = 1; i <= 2; i++) {
    click('保存覆盖方案');
    await waitFor(() => expect(writes).toHaveLength(i));
    await waitFor(() => expect(button('保存覆盖方案')).toBeEnabled());
  }
  expect(writes[0].body).toEqual(writes[1].body);
  expect(writes[0].body.content).toContain('18446744073709551615');
  expect(new Headers(writes[0].headers).get('Idempotency-Key')).toBe(
    new Headers(writes[1].headers).get('Idempotency-Key'),
  );
  expect(screen.getByLabelText('方案名称')).toHaveValue('精确种子草稿');
  change('方案目标数量', '14');
  click('保存覆盖方案');
  await waitFor(() => expect(writes).toHaveLength(3));
  expect(new Headers(writes[2].headers).get('Idempotency-Key')).not.toBe(
    new Headers(writes[1].headers).get('Idempotency-Key'),
  );
});

it('blocks save-and-continue and text switching for an invalid form without losing the draft', async () => {
  const writes = parentMount({ invalid: true });
  click('使用表单编辑');
  await screen.findByLabelText('方案名称');
  change('方案目标数量', '');
  click('切换到模板文本');
  await screen.findByText(/覆盖表单无效/);
  expect(screen.getByLabelText('方案目标数量')).toHaveValue('');
  click('新建覆盖方案');
  await screen.findByRole('dialog', { name: '有未保存的覆盖方案' });
  click('保存并继续');
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByLabelText('方案目标数量')).toHaveValue('');
  expect(writes).toHaveLength(0);
  click('新建覆盖方案');
  click('放弃草稿');
  expect(screen.getByLabelText('覆盖方案模板')).toBeVisible();
  expect(screen.queryByLabelText('方案目标数量')).not.toBeInTheDocument();
});

it('uses saved version on conflict and keeps form drafts when changing plans', async () => {
  const writes = parentMount({ conflict: true });
  fireEvent.click(await screen.findByRole('button', { name: '编辑 编辑样例' }));
  await screen.findByRole('heading', { name: '编辑 编辑样例 · r1' });
  click('使用表单编辑');
  await screen.findByLabelText('方案名称');
  expect(button('保存覆盖方案')).toBeDisabled();
  change('方案名称', '冲突草稿');
  click('保存覆盖方案');
  await waitFor(() => expect(writes).toHaveLength(1));
  await waitFor(() => expect(button('保存覆盖方案')).toBeEnabled());
  expect(new Headers(writes[0].headers).get('If-Match')).toBe('"v1"');
  expect(screen.getByLabelText('方案名称')).toHaveValue('冲突草稿');
  expect(button('导出 JSON 模板')).toBeDisabled();
  click('编辑 编辑样例');
  const dialog = await screen.findByRole('dialog', { name: '有未保存的覆盖方案' });
  fireEvent.click(within(dialog).getByRole('button', { name: '继续编辑' }));
  expect(screen.getByLabelText('方案名称')).toHaveValue('冲突草稿');
});

it('rebuilds the form from a newer readback revision instead of pairing stale fields with its version', async () => {
  const writes = parentMount({ newerReadback: true });
  click('使用表单编辑');
  await screen.findByLabelText('方案名称');
  change('方案名称', '本次保存');
  click('保存覆盖方案');
  await screen.findByRole('heading', { name: '编辑 保存后另一窗口更新 · r3' });
  expect(screen.getByLabelText('方案名称')).toHaveValue('保存后另一窗口更新');
  expect(screen.getByLabelText('方案目标数量')).toHaveValue('30');
  expect(button('保存覆盖方案')).toBeDisabled();
  change('方案名称', '继续编辑新版');
  click('保存覆盖方案');
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(new Headers(writes[1].headers).get('If-Match')).toBe('"v3"');
  expect(writes[1].body.content).toContain('"target_count":30');
});
