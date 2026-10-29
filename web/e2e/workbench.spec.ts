import { test, expect, type Page } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  existsSync,
  writeFileSync,
  unlinkSync,
  statSync,
} from 'node:fs';
import { resolve, join, delimiter } from 'node:path';
const repo = resolve('..'),
  temporary = join(repo, '.test-tmp');
mkdirSync(temporary, { recursive: true });
const base = mkdtempSync(join(temporary, 'f14-browser-'));
let child: ChildProcess,
  python: string,
  environment: NodeJS.ProcessEnv,
  credentials: { pid: number; token: string; authority: string; instance_id: string };
const errors: string[] = [];
const bootstrapTimes: number[] = [];
test.describe.configure({ mode: 'serial' });
async function api(method: string, path: string, body?: unknown, version?: string) {
  const response = await fetch('http://' + credentials.authority + path, {
    method,
    headers: {
      Authorization: 'Bearer ' + credentials.token,
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
      ...(version ? { 'If-Match': `"${version}"` } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  expect(response.ok, `${method} ${path}: HTTP ${response.status}`).toBeTruthy();
  return response.json();
}
function privatePipe(bootstrap = false) {
  return JSON.parse(
    execFileSync(python, ['-m', 'tests.web_fixture', 'credentials'], {
      cwd: repo,
      env: environment,
      input: JSON.stringify({ pid: child.pid, bootstrap }),
      encoding: 'utf8',
      windowsHide: true,
      timeout: 10_000,
    }),
  );
}
async function connect(page: Page) {
  page.on('pageerror', (error) => errors.push(error.message));
  // The serial suite opens many fresh browser contexts. Respect the real
  // 30 exchanges/minute limit instead of disabling authentication in fixtures.
  while (bootstrapTimes.length && Date.now() - bootstrapTimes[0] >= 60_250) bootstrapTimes.shift();
  if (bootstrapTimes.length >= 29) {
    await page.waitForTimeout(60_250 - (Date.now() - bootstrapTimes[0]));
    while (bootstrapTimes.length && Date.now() - bootstrapTimes[0] >= 60_250) bootstrapTimes.shift();
  }
  const { url } = privatePipe(true);
  const exchange = page.waitForResponse(
    (response) => response.url().endsWith('/api/session/bootstrap') && response.request().method() === 'POST',
  );
  await page.goto(url);
  expect((await exchange).status(), 'Local bootstrap exchange').toBe(200);
  bootstrapTimes.push(Date.now());
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible();
  await expect(page).not.toHaveURL(/bootstrap/);
}
test.beforeAll(async () => {
  const info = JSON.parse(
    execFileSync(
      join(repo, '.venv', 'Scripts', 'python.exe'),
      [
        '-c',
        'import sys,json; print(json.dumps({"python":sys._base_executable,"paths":[p for p in sys.path if p.endswith("site-packages")]}))',
      ],
      { encoding: 'utf8', windowsHide: true },
    ),
  );
  python = info.python;
  environment = {
    ...process.env,
    PYTHONPATH: [join(repo, 'src'), repo, ...info.paths].join(delimiter),
    PYTHONIOENCODING: 'utf-8',
  };
  child = spawn(python, ['-m', 'tests.web_fixture', 'serve', base], {
    cwd: repo,
    env: environment,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  child.stderr!.on('data', (chunk) => {
    log += chunk.toString();
  });
  await new Promise<void>((done, reject) => {
    const timer = setTimeout(() => reject(new Error('Server timeout: ' + log)), 15_000);
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error('Fixture exited ' + code + ': ' + log));
    });
    child.stdout!.on('data', (chunk) => {
      if (chunk.toString().includes('"ready": true')) {
        clearTimeout(timer);
        done();
      }
    });
  });
  credentials = privatePipe();
  expect(credentials.pid).toBe(child.pid);
});
test.afterAll(async () => {
  if (child?.exitCode === null) {
    try {
      if (credentials?.pid === child.pid)
        await api('POST', '/api/session/shutdown', {}, credentials.instance_id);
      await new Promise<void>((done) => {
        if (child.exitCode !== null) return done();
        const timer = setTimeout(() => {
          child.kill();
          done();
        }, 8000);
        child.once('exit', () => {
          clearTimeout(timer);
          done();
        });
      });
    } finally {
      if (child.exitCode === null) child.kill();
    }
  }
});
test('创建、导入、编辑草稿、审核、导出和实际文件闭环', async ({ page }) => {
  await connect(page);
  await page.getByLabel('项目名称', { exact: true }).fill('几何风格 · 本地验证');
  await page.getByLabel('触发词', { exact: true }).fill('geometric_style');
  await page.getByRole('button', { name: '选择保存位置并创建' }).click();
  await expect(page.getByRole('heading', { name: /数据清洗工作台/ })).toBeVisible();
  await page.getByRole('link', { name: '来源与导入' }).click();
  await page.getByRole('button', { name: '选择图片目录', exact: true }).click();
  await expect(page.getByRole('button', { name: '扫描并预览' })).toBeEnabled();
  await page.getByRole('button', { name: '扫描并预览' }).click();
  await expect(page.getByRole('heading', { name: '导入预览 · 6 张' })).toBeVisible();
  await page.getByRole('button', { name: '确认导入', exact: true }).click();
  await expect(page.getByText('导入结束：成功 6', { exact: false })).toBeVisible();
  await page.getByRole('link', { name: '数据工作台' }).click();
  const grid = page.getByRole('grid', { name: '图片列表' });
  await expect(grid.getByRole('gridcell')).toHaveCount(6);
  await grid.getByRole('gridcell').first().click();
  const tags = page.getByLabel('标签 · 2', { exact: true });
  await expect(tags).toHaveValue('geometric, still life');
  await tags.fill('geometric, studio_light, k, x, u');
  await tags.press('k');
  await expect(grid.getByRole('gridcell').first()).toHaveAccessibleName(/未审/);
  await tags.fill('geometric, studio_light');
  await grid.getByRole('gridcell').nth(1).click();
  await expect(page.getByRole('dialog', { name: '有未保存的标注' })).toBeVisible();
  await page.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByLabel('标签 · 2', { exact: true })).toHaveValue('geometric, studio_light');
  await page.getByRole('button', { name: '保存标注', exact: true }).click();
  await expect(page.getByLabel('标签 · 2', { exact: true })).toHaveValue('geometric, studio light');
  await expect(page.getByRole('button', { name: '保存标注', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '原图预览', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '1.png', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('img')).toHaveAttribute('src', /^blob:/);
  await page.getByRole('button', { name: '关闭预览' }).click();
  await expect(grid.getByRole('gridcell').first()).toBeFocused();
  await page.getByRole('button', { name: '全选筛选结果 6 张' }).click();
  await page.getByRole('button', { name: '✓ 保留 K', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '确认批量审核' })).toBeVisible();
  await page.getByRole('button', { name: '确认审核', exact: true }).click();
  await expect(grid.getByRole('gridcell', { name: /保留/ })).toHaveCount(6);
  await page.getByRole('button', { name: '撤销审核', exact: true }).click();
  await expect(grid.getByRole('gridcell', { name: /未审/ })).toHaveCount(6);
  await page.getByRole('button', { name: '全选筛选结果 6 张' }).click();
  await page.getByRole('button', { name: '✓ 保留 K', exact: true }).click();
  await page.getByRole('button', { name: '确认审核', exact: true }).click();
  await expect(grid.getByRole('gridcell', { name: /保留/ })).toHaveCount(6);
  await page.screenshot({ path: join(temporary, 'f14-workbench-light.png') });
  await page.getByRole('link', { name: '导出数据集', exact: true }).click();
  await page.getByRole('button', { name: '生成导出预览' }).click();
  await expect(page.getByRole('button', { name: '确认导出 6 张' })).toBeVisible();
  await page.getByRole('button', { name: '确认导出 6 张' }).click();
  await expect(page.getByRole('heading', { name: '导出完成', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看报告', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '导出报告' })).toContainText('成功 6');
  const project = await api('GET', '/api/projects/current');
  const listing = await api('GET', `/api/projects/${project.project_id}/exports`);
  const report = await api('GET', listing.items[0].report_url);
  expect(report.counts.exported).toBe(6);
  expect(existsSync(join(base, 'browser.slproj', report.directory, 'export_report.json'))).toBeTruthy();
  const exported = report.images.find((item: { image_id: number }) => item.image_id === 1);
  const caption = exported.files.find((file: { path: string }) => file.path.endsWith('.txt')).path;
  expect(readFileSync(join(base, 'browser.slproj', report.directory, caption), 'utf8')).toContain(
    'geometric style, geometric, studio light',
  );
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(
    credentials.token,
  );
  expect(errors).toEqual([]);
});

test('冲突保留草稿、导航保护、外观偏好和无障碍', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '数据工作台' }).click();
  await page.getByRole('gridcell').first().click();
  await page.getByLabel('标签 · 2', { exact: true }).fill('geometric, studio light, local draft');
  const project = await api('GET', '/api/projects/current'),
    path = `/api/projects/${project.project_id}/annotations/1`,
    current = await api('GET', path);
  await api(
    'PUT',
    path + '/manual',
    {
      tags: [{ tag: 'geometric' }, { tag: 'studio light' }, { tag: 'external edit' }],
      read_set: current.refs,
    },
    current.version,
  );
  await page.getByRole('button', { name: '保存标注', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '标注已被修改' })).toContainText('local draft');
  await expect(page.getByRole('dialog')).toContainText('external edit');
  await page.getByRole('button', { name: '合并到最新版本' }).click();
  await page.getByRole('button', { name: '保存标注', exact: true }).click();
  await expect(page.getByLabel('标签 · 4', { exact: true })).toHaveValue(
    'geometric, studio light, external edit, local draft',
  );
  await page.getByLabel('标签 · 4', { exact: true }).fill('unsaved');
  await page.getByRole('link', { name: '界面设置' }).click();
  await expect(page.getByRole('dialog', { name: '有未保存的标注' })).toBeVisible();
  await page.getByRole('button', { name: '放弃草稿' }).click();
  await page.getByLabel('外观', { exact: true }).selectOption('dark');
  await page.getByLabel('工作台密度', { exact: true }).selectOption('comfortable');
  await page.getByRole('link', { name: '数据工作台' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.screenshot({ path: join(temporary, 'f14-workbench-dark.png') });
  await page.evaluate(readFileSync(resolve('node_modules/axe-core/axe.min.js'), 'utf8'));
  const findings = await page.evaluate(async () => {
    const result = await (
      window as unknown as { axe: { run: () => Promise<{ violations: { id: string; impact: string }[] }> } }
    ).axe.run();
    return result.violations.filter((v) => ['serious', 'critical'].includes(v.impact));
  });
  expect(findings).toEqual([]);
  expect(errors).toEqual([]);
});
test('刷新后需重新授权且无凭据持久化', async ({ page }) => {
  await connect(page);
  await page.reload();
  await expect(page.getByRole('heading', { name: '重新连接本地服务' })).toBeVisible();
  await expect(page.getByText('python -m stylelora ui', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(
    credentials.token,
  );
});
test('外部审核移出筛选、断线和 401 均保留正在编辑的草稿', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '数据工作台' }).click();
  await page.getByLabel('审核筛选', { exact: true }).selectOption('keep');
  await page.getByRole('gridcell').first().click();
  await page.getByLabel('标签 · 4', { exact: true }).fill('preserved draft');
  const project = await api('GET', '/api/projects/current'),
    path = `/api/projects/${project.project_id}`,
    image = await api('GET', path + '/images/1');
  await api('POST', path + '/images/review', {
    ids: [1],
    target: 'reject',
    expected_versions: { '1': image.version },
  });
  await expect(page.getByRole('gridcell')).toHaveCount(5);
  await expect(page.getByLabel('标签 · 4', { exact: true })).toHaveValue('preserved draft');
  await page.context().setOffline(true);
  await expect(
    page.getByText('本地服务暂时无法连接，写入已暂停。草稿保留在当前页面。', { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: '保存标注', exact: true })).toBeDisabled();
  await expect(page.getByLabel('标签 · 4', { exact: true })).toHaveValue('preserved draft');
  await page.context().setOffline(false);
  await expect(page.getByRole('button', { name: '保存标注', exact: true })).toBeEnabled();
  await page.route('**/api/session', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'auth:invalid', outcome: 'rejected' } }),
    }),
  );
  await expect(page.getByText(/授权已失效，草稿保留/)).toBeVisible();
  await expect(page.getByRole('button', { name: '保存标注', exact: true })).toBeDisabled();
  await expect(page.getByLabel('标签 · 4', { exact: true })).toHaveValue('preserved draft');
});

test('标签资料导入、别名补全、批量编辑、规则库、应用撤销与导出', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '本地标签库', exact: true }).click();
  await page.getByLabel('本地资料文件').setInputFiles({
    name: 'catalog.json',
    mimeType: 'application/json',
    buffer: Buffer.from(
      JSON.stringify({
        schema_version: 1,
        kind: 'tag_catalog',
        tags: [{ tag: 'canonical_tag', aliases: ['alias_tag'] }, { tag: 'geometric' }],
      }),
    ),
  });
  await page.getByRole('button', { name: '预览资料导入' }).click();
  await expect(page.getByRole('region', { name: '资料导入预览' })).toContainText('整体替换 2 项');
  await page.getByRole('button', { name: '确认导入资料' }).click();
  await expect(page.getByText('2 个规范标签 · 1 个别名 · 0 项翻译')).toBeVisible();
  await page.getByLabel('资料类型').selectOption('translations');
  await page.getByLabel('本地资料文件').setInputFiles({
    name: 'translations.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify({ canonical_tag: '规范标签' })),
  });
  await page.getByRole('button', { name: '预览资料导入' }).click();
  await page.getByRole('button', { name: '确认导入资料' }).click();
  await expect(page.getByText('2 个规范标签 · 1 个别名 · 1 项翻译')).toBeVisible();
  await page.getByRole('link', { name: '数据工作台' }).click();
  await page.getByRole('gridcell').first().click();
  await page.getByLabel('从本地标签库补全').fill('alias_tag');
  await page.getByRole('button', { name: 'canonical tag · 规范标签', exact: true }).click();
  await page.getByRole('button', { name: '保存标注', exact: true }).click();
  await expect(page.locator('#tags')).toHaveValue(/canonical tag/);
  await expect(page.locator('#tags')).not.toHaveValue(/alias|规范标签/);
  await page.getByRole('button', { name: '全选筛选结果 6 张' }).click();
  await page.getByRole('button', { name: '批量标签', exact: true }).click();
  await page.getByLabel('批量新增标签').fill('batch_tag');
  await page.getByRole('button', { name: '生成变更预览' }).click();
  await expect(page.getByRole('region', { name: '变更预览' })).toContainText('将更改 6 张');
  await page.getByLabel('批量新增标签').fill('batch_tag, discard_me');
  await expect(page.getByRole('button', { name: '确认应用' })).toBeDisabled();
  await page.getByLabel('批量新增标签').fill('batch_tag');
  await page.getByRole('button', { name: '生成变更预览' }).click();
  await page.getByRole('button', { name: '确认应用' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const project = await api('GET', '/api/projects/current'),
    p = `/api/projects/${project.project_id}`;
  for (let id = 1; id <= 6; id++)
    expect((await api('GET', `${p}/annotations/${id}`)).tags.map((t: { tag: string }) => t.tag)).toContain(
      'batch tag',
    );
  await page.getByRole('link', { name: '替换规则库', exact: true }).click();
  await page.getByRole('button', { name: '新建规则' }).click();
  await page.getByLabel('规则名称').fill('批量清洗验收');
  await page.getByLabel('条件标签', { exact: true }).fill('geometric');
  await page.getByLabel('删除标签', { exact: true }).fill('batch_tag');
  await page.getByLabel('写入标签', { exact: true }).fill('curated');
  await page.getByRole('button', { name: '保存规则', exact: true }).click();
  await expect(page.getByRole('button', { name: '编辑 批量清洗验收' })).toBeVisible();
  await page.getByRole('link', { name: '数据工作台' }).click();
  await page.getByRole('button', { name: '全选筛选结果 6 张' }).click();
  await page.getByRole('button', { name: '替换规则', exact: true }).click();
  await page.getByLabel('使用已保存规则').selectOption({ label: '批量清洗验收' });
  await page.getByRole('button', { name: '生成变更预览' }).click();
  await expect(page.getByRole('region', { name: '变更预览' })).toContainText('将更改 5 张');
  await page.screenshot({ path: join(temporary, 'f09-rule-preview.png') });
  await page.getByRole('button', { name: '确认应用' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await api('GET', `${p}/annotations/2`)).tags.map((t: { tag: string }) => t.tag)).toContain(
    'curated',
  );
  await page.getByRole('link', { name: '规则操作日志', exact: true }).click();
  await expect(page.getByRole('heading', { name: '操作 #1 · 已应用' })).toBeVisible();
  await page.getByRole('button', { name: '查看变更 #1' }).click();
  await expect(page.getByRole('cell', { name: '#2', exact: true })).toBeVisible();
  const undo = page.getByRole('button', { name: /撤销操作 #1/ });
  await undo.focus();
  await page.keyboard.down('Enter');
  await page.waitForTimeout(1150);
  await page.keyboard.up('Enter');
  await expect(page.getByRole('heading', { name: '操作 #1 · 已撤销' })).toBeVisible();
  const tags = (await api('GET', `${p}/annotations/2`)).tags.map((t: { tag: string }) => t.tag);
  expect(tags).toContain('batch tag');
  expect(tags).not.toContain('curated');
  await page.getByRole('link', { name: '数据工作台' }).click();
  await page.getByRole('button', { name: '全选筛选结果 6 张' }).click();
  await page.getByRole('button', { name: '标签统计' }).click();
  await expect(page.getByRole('dialog')).toContainText('6 张图片');
  await expect(page.getByRole('row').filter({ hasText: 'batch tag' })).toContainText('6');
  await page.getByRole('button', { name: '关闭', exact: true }).last().click();
  await page.getByRole('link', { name: '导出数据集', exact: true }).click();
  await page.getByRole('button', { name: '生成导出预览' }).click();
  await page.getByRole('button', { name: '确认导出 5 张' }).click();
  await expect(page.getByRole('heading', { name: '导出完成', exact: true })).toHaveCount(2);
  const exports = await api('GET', p + '/exports');
  const report = await api('GET', exports.items.at(-1).report_url);
  expect(report.counts.exported).toBe(5);
  const entry = report.images.find((row: { image_id: number }) => row.image_id === 2);
  const file = entry.files.find((f: { path: string }) => f.path.endsWith('.txt')).path;
  const caption = readFileSync(join(base, 'browser.slproj', report.directory, file), 'utf8');
  expect(caption).toContain('batch tag');
  expect(caption).not.toMatch(/curated|规范标签/);
  expect(errors).toEqual([]);
});

test('规则预览后的并发标注使整批拒绝且保留表单', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: '全选筛选结果 6 张' }).click();
  await page.getByRole('button', { name: '替换规则', exact: true }).click();
  await page.getByLabel('使用已保存规则').selectOption({ label: '批量清洗验收' });
  await page.getByRole('button', { name: '生成变更预览' }).click();
  await expect(page.getByRole('button', { name: '确认应用' })).toBeEnabled();
  const project = await api('GET', '/api/projects/current'),
    p = `/api/projects/${project.project_id}`;
  const v = await api('GET', p + '/annotations/6');
  await api(
    'PUT',
    p + '/annotations/6/manual',
    { tags: [...v.tags, { tag: 'concurrent' }], read_set: v.refs },
    v.version,
  );
  await page.getByRole('button', { name: '确认应用' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('内容已被另一次操作修改');
  await expect(page.getByLabel('写入标签', { exact: true })).toHaveValue('curated');
  expect(
    (await api('GET', p + '/operation-log')).items.filter(
      (row: { status: string }) => row.status === 'applied',
    ),
  ).toHaveLength(0);
  expect((await api('GET', p + '/annotations/2')).tags.map((t: { tag: string }) => t.tag)).not.toContain(
    'curated',
  );
  await page.getByRole('button', { name: '取消', exact: true }).click();
  expect(errors).toEqual([]);
});

test('词库方案解析诊断、默认选择及带触发词确认的定制导出', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '本地标签库', exact: true }).click();
  await page.getByLabel('本地资料文件').setInputFiles({
    name: 'exchange-catalog.json',
    mimeType: 'application/json',
    buffer: Buffer.from(
      JSON.stringify({
        schema_version: 1,
        kind: 'tag_catalog',
        complete: true,
        tags: [
          { tag: 'canonical_tag', aliases: ['alias_tag'] },
          { tag: 'geometric' },
          { tag: 'still_life' },
          { tag: 'geometric_style' },
        ],
      }),
    ),
  });
  await page.getByRole('button', { name: '预览资料导入', exact: true }).click();
  await page.getByRole('button', { name: '确认导入资料', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认导入资料', exact: true })).toBeDisabled();
  await page.getByRole('link', { name: '导入导出方案', exact: true }).click();
  await page.getByRole('button', { name: '新建方案', exact: true }).click();
  await page.getByLabel('方案名称').fill('词库导入验收');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '设为默认 词库导入验收', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '设为默认 词库导入验收', exact: true }).click();
  await expect(page.getByRole('button', { name: '设为默认 词库导入验收', exact: true })).toHaveCount(0);
  execFileSync(python, ['-m', 'tests.web_fixture', 'extra-input', base, 'invalid'], {
    cwd: repo,
    env: environment,
    windowsHide: true,
  });
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await expect(page.getByLabel('导入方案')).toContainText('词库导入验收（默认）');
  await page.getByRole('button', { name: '选择图片目录', exact: true }).click();
  await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
  await expect(page.getByRole('heading', { name: '导入预览 · 7 张' })).toBeVisible();
  await expect(page.getByRole('button', { name: '确认导入', exact: true })).toBeDisabled();
  await expect(page.getByRole('alert')).toContainText('1 项解析或文件错误');
  execFileSync(python, ['-m', 'tests.web_fixture', 'extra-input', base, 'valid'], {
    cwd: repo,
    env: environment,
    windowsHide: true,
  });
  await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
  await expect(page.getByRole('cell', { name: /canonical tag/ })).toBeVisible();
  await page.getByRole('button', { name: '确认导入', exact: true }).click();
  await expect(page.getByText('导入结束：成功 1，跳过 6', { exact: false })).toBeVisible();
  const project = await api('GET', '/api/projects/current'),
    p = `/api/projects/${project.project_id}`;
  const sources = await api('GET', p + '/sources'),
    sourceId = sources.items.at(-1).source_id;
  const original = await api('GET', p + '/annotations/7');
  expect(original.tags.map((t: { tag: string }) => t.tag)).toEqual(['canonical tag']);
  await page.getByRole('link', { name: '导入导出方案', exact: true }).click();
  await page.getByRole('button', { name: '导出方案', exact: true }).click();
  await page.getByRole('button', { name: '新建方案', exact: true }).click();
  await page.getByLabel('方案名称').fill('训练导出验收');
  await page.getByLabel('未审核图片', { exact: true }).check();
  await page.getByLabel('来源范围（按 Ctrl 多选，空选表示全部）').selectOption(String(sourceId));
  await page.getByLabel('文件命名').selectOption('{stem}-{image_id}{ext}');
  await page.getByLabel('导出子目录').fill('training/data');
  await page.getByLabel('追加自然语言（手动 → 语言模型 → 原始）').check();
  await page.getByLabel('导出 PNG 原始元数据 JSON').check();
  await page.getByLabel('要求存在触发词之外的标签').check();
  await page.screenshot({ path: join(temporary, 'f10-scheme-editor.png'), fullPage: true });
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '编辑 训练导出验收', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '导出数据集', exact: true }).click();
  await page.getByLabel('导出方案', { exact: true }).selectOption({ label: '训练导出验收' });
  await page.getByRole('button', { name: '生成导出预览', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认导出 1 张', exact: true })).toBeDisabled();
  await page.getByLabel('已了解触发词与现有标签重合，继续导出').check();
  await page.getByRole('button', { name: '确认导出 1 张', exact: true }).click();
  await expect(page.getByRole('heading', { name: '导出完成', exact: true })).toHaveCount(3);
  const reports = await api('GET', p + '/exports'),
    report = await api('GET', reports.items.at(-1).report_url);
  expect(report.counts.exported).toBe(1);
  expect(report.trigger_override_digest).toBe(report.trigger_validation.digest);
  const entry = report.images.find((r: { status: string }) => r.status === 'exported');
  expect(entry.files.map((f: { path: string }) => f.path)).toEqual([
    'training/data/7-7.png',
    'training/data/7-7.txt',
    'training/data/7-7.json',
  ]);
  const caption = readFileSync(
    join(base, 'browser.slproj', report.directory, 'training/data/7-7.txt'),
    'utf8',
  );
  expect(caption).toBe('geometric style, canonical tag\n\n新图说明\n新图段落。\n');
  await page.screenshot({ path: join(temporary, 'f10-custom-export.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('方案并发修改保留草稿，删除默认方案原子切换替代项', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '导入导出方案', exact: true }).click();
  await page.getByRole('button', { name: '编辑 词库导入验收', exact: true }).click();
  await page.getByLabel('方案名称').fill('我的未保存名称');
  const project = await api('GET', '/api/projects/current'),
    p = `/api/projects/${project.project_id}`;
  const rows = await api('GET', p + '/import-schemes'),
    row = rows.items.find((s: { is_default: boolean }) => s.is_default);
  await api(
    'PUT',
    `${p}/import-schemes/${row.scheme_id}`,
    { ...row.configuration, name: '并发修改名称' },
    row.version,
  );
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('内容已被另一次操作修改');
  await expect(page.getByLabel('方案名称')).toHaveValue('我的未保存名称');
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await page.getByRole('button', { name: '删除 并发修改名称', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认删除方案', exact: true })).toBeDisabled();
  const replacement = rows.items.find((s: { is_default: boolean }) => !s.is_default);
  await page.getByLabel('替换默认方案').selectOption(String(replacement.scheme_id));
  await page.getByRole('button', { name: '确认删除方案', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '删除方案' })).toHaveCount(0);
  const remaining = await api('GET', p + '/import-schemes');
  expect(remaining.items).toHaveLength(1);
  expect(remaining.items[0].is_default).toBeTruthy();
  expect(errors).toEqual([]);
});

test('Launcher 本地握手诊断可达但不启用未经核验的生成', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: 'Launcher / MCP', exact: true }).click();
  await expect(page.getByRole('heading', { name: '尚未检查', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '检查连接', exact: true }).click();
  await expect(page.getByRole('heading', { name: '连接检查通过', exact: true })).toBeVisible();
  await expect(
    page.getByText('已连接到 Launcher，但此版本的工具结构尚未核验，暂不能启用生成。'),
  ).toBeVisible();
  await expect(page.getByRole('heading', { name: '生成尚未启用', exact: true })).toBeVisible();
  await page.getByText('查看兼容性诊断', { exact: true }).click();
  await expect(page.getByLabel('核心工具结构摘要')).toHaveValue(/^[a-f0-9]{64}$/);
  const registry = await api('GET', '/api/backends');
  expect(registry.items[0].tool_count).toBe(8);
  expect(registry.items[0].features.generation).toBe(false);
  await page.screenshot({ path: join(temporary, 'f02-launcher-diagnostics.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('Launcher 发现文件异常可重新检查且不影响已有数据', async ({ page }) => {
  const path = join(base, 'launcher', 'nai-launcher', 'mcp-server.json');
  const original = readFileSync(path);
  try {
    writeFileSync(path, '{');
    await connect(page);
    await page.getByRole('link', { name: 'Launcher / MCP', exact: true }).click();
    await page.getByRole('button', { name: '检查连接', exact: true }).click();
    await expect(page.getByRole('heading', { name: '暂时无法连接', exact: true })).toBeVisible();
    await expect(
      page.getByText('Launcher 发现文件不完整或格式不正确，请在 Launcher 就绪后重新检查。'),
    ).toBeVisible();
    await expect(page.getByLabel('核心工具结构摘要')).toHaveCount(0);
    writeFileSync(path, original);
    await page.getByRole('button', { name: '检查连接', exact: true }).click();
    await expect(page.getByRole('heading', { name: '连接检查通过', exact: true })).toBeVisible();
    await page.getByRole('link', { name: '数据工作台', exact: true }).click();
    await expect(page.getByRole('heading', { name: /数据清洗工作台/ })).toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    writeFileSync(path, original);
  }
});

test('近似重复扫描要求显式阈值，候选分页稳定且不自动审核图片', async ({ page }) => {
  await connect(page);
  const project = await api('GET', '/api/projects/current'),
    p = `/api/projects/${project.project_id}`;
  const before = await api('GET', p + '/images?limit=200');
  await page.getByRole('link', { name: '近似重复扫描', exact: true }).click();
  await expect(page.getByRole('button', { name: '开始扫描', exact: true })).toBeDisabled();
  await page.getByLabel('最大汉明距离（必填，0–64）').fill('65');
  await expect(page.getByRole('button', { name: '开始扫描', exact: true })).toBeDisabled();
  await page.getByLabel('最大汉明距离（必填，0–64）').fill('64');
  await page.getByRole('button', { name: '开始扫描', exact: true }).click();
  await expect(page.getByRole('heading', { name: '扫描完成', exact: true })).toBeVisible();
  const scans = await api('GET', p + '/duplicates/scans'),
    scan = await api('GET', p + '/duplicates/scans/' + scans.items[0].scan_id + '?limit=10');
  const eligible = before.items.filter(
    (row: { review_status: string; file_missing: boolean }) =>
      row.review_status !== 'reject' && !row.file_missing,
  ).length;
  expect(scan.upper_id).toBe((eligible * (eligible - 1)) / 2);
  await expect(page.getByRole('region', { name: /^候选对 / })).toHaveCount(Math.min(10, scan.upper_id));
  const first = scan.items[0];
  await expect(
    page.getByRole('region', { name: `候选对 ${first.left_id} 与 ${first.right_id}`, exact: true }),
  ).toBeVisible();
  if (scan.has_more) {
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await expect(page.getByText(`第 2 页 · 共 ${scan.upper_id} 对`, { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '上一页', exact: true }).click();
  }
  const after = await api('GET', p + '/images?limit=200');
  expect(
    after.items.map((row: { image_id: number; review_status: string }) => [row.image_id, row.review_status]),
  ).toEqual(
    before.items.map((row: { image_id: number; review_status: string }) => [row.image_id, row.review_status]),
  );
  await page
    .getByRole('region', { name: /^候选对 / })
    .first()
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f08-duplicate-candidates.png') });
  await page
    .getByRole('button', { name: `在工作台审核图片 ${first.left_id}`, exact: true })
    .first()
    .click();
  await expect(page.getByRole('heading', { name: /数据清洗工作台/ })).toBeVisible();
  await page.getByRole('link', { name: '近似重复扫描', exact: true }).click();
  await expect(page.getByRole('heading', { name: '扫描完成', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('扫描结果可从任务恢复，释放后图片仍在且旧查询失效', async ({ page }) => {
  await connect(page);
  const project = await api('GET', '/api/projects/current'),
    p = `/api/projects/${project.project_id}`;
  const before = await api('GET', p + '/images?limit=200');
  await page.getByRole('link', { name: '任务进度', exact: true }).click();
  await page.getByRole('button', { name: '查看重复候选', exact: true }).click();
  await expect(page.getByRole('heading', { name: '扫描完成', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '释放扫描结果', exact: true }).click();
  await expect(page.getByRole('heading', { name: '尚未扫描', exact: true })).toBeVisible();
  expect((await api('GET', p + '/duplicates/scans')).items).toEqual([]);
  expect(
    (await api('GET', p + '/images?limit=200')).items.map((r: { image_id: number }) => r.image_id),
  ).toEqual(before.items.map((r: { image_id: number }) => r.image_id));
  await page.getByRole('link', { name: '任务进度', exact: true }).click();
  await page.getByRole('button', { name: '查看重复候选', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('会话结果已过期或释放');
  await page.getByRole('button', { name: '释放扫描结果', exact: true }).click();
  await expect(page.getByRole('heading', { name: '尚未扫描', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('真实模型清单展示来源依据，查看页面不自动下载安装', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: 'WD 模型管理', exact: true }).click();
  const model = page.getByRole('region', { name: '模型 wd-eva02-large-tagger-v3', exact: true });
  await expect(model.getByText('未安装', { exact: true })).toBeVisible();
  await model.getByText('文件与校验信息', { exact: true }).click();
  await expect(model.getByText(/标签表先核验官方固定版本的 Git 文件摘要/)).toBeVisible();
  await expect(
    model.getByText('298633d94d0031d2081c0893f29c82eab7f0df00b08483ba8f29d1e979441217', { exact: true }),
  ).toBeVisible();
  await expect(model.getByRole('button', { name: '下载并安装', exact: true })).toBeEnabled();
  await model.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f12-real-model-provenance.png'), fullPage: true });
  const operations = await api('GET', '/api/operations?limit=100');
  expect(operations.items.some((item: { kind: string }) => item.kind === 'tagger.install')).toBe(false);
  expect(errors).toEqual([]);
});

test('模型安装经真实 ONNX 校验后可恢复状态，项目图片保持不变', async ({ page }) => {
  await connect(page);
  const project = await api('GET', '/api/projects/current');
  const before = await api('GET', `/api/projects/${project.project_id}/images?limit=200`);
  await page.getByRole('link', { name: 'WD 模型管理', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'WD 模型管理', exact: true })).toBeVisible();
  const model = page.getByRole('region', { name: '模型 synthetic-tagger', exact: true });
  await expect(model.getByText('未安装', { exact: true })).toBeVisible();
  await model.getByRole('button', { name: '下载并安装', exact: true }).click();
  await expect(model.getByText('文件已校验', { exact: true })).toBeVisible();
  await expect(model.getByRole('status')).toContainText('WD 标注任务');
  await model.getByRole('button', { name: '重新校验文件', exact: true }).click();
  await expect(model.getByText('文件已校验', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '任务进度', exact: true }).click();
  await page.getByRole('button', { name: '查看模型状态', exact: true }).first().click();
  await expect(model.getByText('文件已校验', { exact: true })).toBeVisible();
  const after = await api('GET', `/api/projects/${project.project_id}/images?limit=200`);
  expect(after.items).toEqual(before.items);
  expect((await api('GET', '/api/tagger/capabilities')).available).toBe(true);
  await page.screenshot({ path: join(temporary, 'f12-model-manager.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('模型安装可取消并重试，已完成版本仍保留', async ({ page }) => {
  const hold = join(base, 'hold-model-download');
  writeFileSync(hold, 'hold synthetic fixture');
  try {
    await connect(page);
    await page.getByRole('link', { name: 'WD 模型管理', exact: true }).click();
    const model = page.getByRole('region', { name: '模型 synthetic-slow', exact: true });
    await model.getByRole('button', { name: '下载并安装', exact: true }).click();
    await expect(model.getByRole('button', { name: '取消安装', exact: true })).toBeVisible();
    await model.getByRole('button', { name: '取消安装', exact: true }).click();
    await expect(model.getByText('安装已取消，可以重新开始。', { exact: true })).toBeVisible();
    await expect(model.getByRole('button', { name: '下载并安装', exact: true })).toBeEnabled();
    unlinkSync(hold);
    await model.getByRole('button', { name: '下载并安装', exact: true }).click();
    await expect(model.getByText('文件已校验', { exact: true })).toBeVisible();
    await expect(
      page
        .getByRole('region', { name: '模型 synthetic-tagger', exact: true })
        .getByText('文件已校验', { exact: true }),
    ).toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    if (existsSync(hold)) unlinkSync(hold);
  }
});

test('WD 标注任务完成推理、缓存复用并保留逐图结果', async ({ page }) => {
  await connect(page);
  const project = await api('GET', '/api/projects/current');
  const p = `/api/projects/${project.project_id}`;
  await page.getByRole('link', { name: 'WD 标注任务', exact: true }).click();
  await expect(page.getByRole('button', { name: '开始标注', exact: true })).toBeDisabled();
  await page.getByLabel('已校验的模型').selectOption({ label: 'synthetic-tagger · aaaaaaaa' });
  await page.getByLabel('通用标签阈值（0–1，必填）').fill('0');
  await page.getByLabel('角色标签阈值（0–1，必填）').fill('0');
  await expect(page.getByLabel('批大小上限')).toBeDisabled();
  await page.getByLabel('设备策略').selectOption('auto');
  await page.getByLabel('批大小上限').selectOption('4');
  await page.getByRole('button', { name: '开始标注', exact: true }).click();
  await expect(page.getByRole('heading', { name: /任务 #\d+ · 已完成/ })).toBeVisible();
  let jobs = await api('GET', p + '/jobs');
  const first = jobs.items.at(-1);
  expect(first.done_count).toBeGreaterThan(0);
  expect(first.failed_count).toBe(0);
  expect(first.config_snapshot_json.params.config.batch_size).toBe(4);
  expect(first.config_snapshot_json.params.config.provider).toBe('auto');
  const results = await api('GET', first.outcomes_url);
  expect(
    results.items.every(
      (item: { kind: string; annotation_ids: number[] }) =>
        item.kind === 'done' && item.annotation_ids.length === 1,
    ),
  ).toBe(true);
  await page.getByRole('button', { name: '开始标注', exact: true }).click();
  await expect.poll(async () => (await api('GET', p + '/jobs')).items.at(-1).job_id).not.toBe(first.job_id);
  jobs = await api('GET', p + '/jobs');
  const cached = jobs.items.at(-1);
  await expect(
    page.getByRole('heading', { name: `任务 #${cached.job_id} · 已完成`, exact: true }),
  ).toBeVisible();
  const completed = await api('GET', cached.status_url);
  expect(completed.done_count).toBe(0);
  expect(completed.skipped_count).toBe(first.total);
  await expect(page.getByText('当前标注缓存命中').first()).toBeVisible();
  await page.screenshot({ path: join(temporary, 'f12-wd-jobs.png'), fullPage: true });
  await page.getByRole('button', { name: `查看图片 ${results.items[0].image_id}`, exact: true }).click();
  await expect(page.getByRole('heading', { name: /数据清洗工作台/ })).toBeVisible();
  await page.getByRole('link', { name: 'WD 标注任务', exact: true }).click();
  await page.getByRole('button', { name: `查看任务 ${first.job_id}`, exact: true }).click();
  await expect(
    page.getByRole('heading', { name: `任务 #${first.job_id} · 已完成`, exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('LLM 连接、密钥、角色及固定图片诊断', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: 'LLM 连接与角色', exact: true }).click();
  await page.getByRole('button', { name: '新增连接', exact: true }).click();
  await page.getByLabel('API 基础地址', { exact: true }).fill('http://127.0.0.1:19333/v1');
  await page.getByRole('button', { name: '保存连接', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'http://127.0.0.1:19333/v1', exact: true })).toBeVisible();
  await page.getByLabel('新密钥', { exact: true }).fill('bad-browser-secret');
  await page.getByRole('button', { name: '添加密钥', exact: true }).click();
  await expect(page.getByLabel('新密钥', { exact: true })).toHaveValue('');
  await page.getByRole('button', { name: '查询模型列表', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('LLM 服务拒绝了凭据');
  await expect(page.getByRole('heading', { name: 'LLM 连接与角色', exact: true })).toBeVisible();
  await page.getByLabel('新密钥', { exact: true }).fill('browser-fixture-secret');
  await page.getByRole('button', { name: '替换密钥 1', exact: true }).click();
  await expect(page.getByLabel('新密钥', { exact: true })).toHaveValue('');
  await page.getByRole('button', { name: '查询模型列表', exact: true }).click();
  await expect(page.getByText('模型列表已更新，已有手动设置已保留。')).toBeVisible();
  await page.getByRole('button', { name: '检查图片能力：fixture/vision', exact: true }).click();
  await expect(page.getByText('固定图片识别通过，图片能力已更新。')).toBeVisible();
  const options = await page.getByLabel('图片描述', { exact: true }).locator('option').allTextContents();
  await page.getByLabel('图片描述', { exact: true }).selectOption({ label: options[1] });
  await expect
    .poll(async () => (await api('GET', '/api/llm/roles')).roles.vision?.model)
    .toBe('fixture/vision');
  await expect(page.getByText('R2 · 已装载并校验')).toBeVisible();
  expect(await page.locator('body').innerText()).not.toContain('browser-fixture-secret');
  await page.screenshot({ path: join(temporary, 'llm-settings.png'), fullPage: true });
  await page.getByRole('button', { name: '编辑连接', exact: true }).click();
  await page.getByLabel('API 基础地址', { exact: true }).fill('http://127.0.0.1:19333/changed');
  await page.getByRole('link', { name: '界面设置', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '放弃草稿', exact: true }).click();
  await expect(page.getByRole('heading', { name: '产品与界面设置', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('视觉关键词、成组描述、缓存和人工评级形成闭环', async ({ page }) => {
  await connect(page);
  const project = await api('GET', '/api/projects/current'),
    p = `/api/projects/${project.project_id}`;
  await page.getByRole('link', { name: '视觉关键词', exact: true }).click();
  await page.getByLabel('分级关键词（每行：标签、制表符、评级）').fill('geometric\tgeneral');
  await page.getByRole('button', { name: '保存视觉关键词', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存视觉关键词', exact: true })).toBeDisabled();
  await expect
    .poll(async () => (await api('GET', '/api/llm/vision-policy')).keywords)
    .toEqual([{ tag: 'geometric', rating: 'general' }]);
  await page.getByLabel('分级关键词（每行：标签、制表符、评级）').fill('unsaved\tsensitive');
  await page.getByRole('link', { name: '视觉描述任务', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '放弃草稿', exact: true }).click();
  await page.getByRole('button', { name: '开始视觉描述', exact: true }).click();
  await expect(page.getByRole('heading', { name: /任务 #\d+ · 已完成/ })).toBeVisible();
  const first = (await api('GET', p + '/jobs?executor=llm_vision')).items.at(-1);
  expect(first.done_count).toBeGreaterThan(0);
  expect(first.failed_count).toBe(0);
  const outcomes = await api('GET', first.outcomes_url);
  expect(outcomes.items.every((v: { annotation_ids: number[] }) => v.annotation_ids.length === 2)).toBe(true);
  await page.getByRole('button', { name: '开始视觉描述', exact: true }).click();
  await expect
    .poll(async () => (await api('GET', p + '/jobs?executor=llm_vision')).items.at(-1).job_id)
    .not.toBe(first.job_id);
  const cached = (await api('GET', p + '/jobs?executor=llm_vision')).items.at(-1);
  await expect(
    page.getByRole('heading', { name: `任务 #${cached.job_id} · 已完成`, exact: true }),
  ).toBeVisible();
  expect((await api('GET', cached.status_url)).skipped_count).toBe(first.total);
  await page.screenshot({ path: join(temporary, 'f15-vision-jobs.png'), fullPage: true });
  const id = outcomes.items[0].image_id;
  await page.getByRole('button', { name: `查看图片 ${id}`, exact: true }).click();
  await expect(page.getByLabel('图片评级', { exact: true })).toBeEnabled();
  await page.getByLabel('图片评级', { exact: true }).selectOption('sensitive');
  await expect.poll(async () => (await api('GET', p + `/images/${id}`)).rating_source).toBe('manual');
  await expect(page.getByLabel('图片评级', { exact: true })).toHaveValue('sensitive');
  const annotation = await api('GET', p + `/annotations/${id}`);
  expect(annotation.layers['llm/nl'].content).toBe('Geometric objects sit on a table.');
  expect(errors).toEqual([]);
});

test('组合批次预览、确认、暂停接续、缓存复用与整批取消', async ({ page }) => {
  const currentBatch = () => new URL(page.url().split('#')[1], 'http://localhost').searchParams.get('batch');
  const hold = join(base, 'hold-wd-inference'),
    entered = join(base, 'wd-inference-entered');
  writeFileSync(hold, 'hold combined batch fixture');
  if (existsSync(entered)) unlinkSync(entered);
  try {
    await connect(page);
    const project = await api('GET', '/api/projects/current'),
      p = `/api/projects/${project.project_id}`;
    await page.getByRole('link', { name: '标注批次', exact: true }).click();
    await page.getByRole('checkbox', { name: 'WD 标签阶段', exact: true }).check();
    await page.getByLabel('批次 WD 模型').selectOption({ label: 'synthetic-tagger · aaaaaaaa' });
    await page.getByLabel('批次通用标签阈值').fill('0');
    await page.getByLabel('批次角色标签阈值').fill('0');
    await page.getByLabel('批次范围').selectOption('all');
    const previous = (await api('GET', p + '/jobs')).items.length;
    await expect(page.getByRole('table', { name: '阶段与预计处理数量' })).toBeVisible();
    expect((await api('GET', p + '/jobs')).items.length).toBe(previous);
    async function startAll() {
      await page.getByRole('button', { name: '开始标注批次', exact: true }).click();
      const dialog = page.getByRole('dialog');
      await expect(dialog).toBeVisible();
      const button = dialog.getByRole('button', { name: /按住执行全部/ });
      await button.focus();
      await page.keyboard.down('Space');
      await expect(dialog).not.toBeVisible();
      await page.keyboard.up('Space');
    }
    await startAll();
    await expect.poll(() => existsSync(entered)).toBe(true);
    const batchId = currentBatch()!;
    const batchUrl = p + '/jobs/batches/' + batchId;
    let batch = await api('GET', batchUrl);
    expect(batch.items.map((v: { executor: string }) => v.executor)).toEqual(['wd', 'llm_vision']);
    expect(batch.items[1].status).toBe('pending');
    await page.getByRole('button', { name: '暂停批次', exact: true }).click();
    await expect(page.getByRole('heading', { name: '批次 · 已暂停', exact: true })).toBeVisible();
    unlinkSync(hold);
    await expect.poll(async () => (await api('GET', batchUrl)).items[0].done_count).toBe(1);
    expect((await api('GET', batchUrl)).items[1].status).toBe('pending');
    await page.getByRole('button', { name: '继续批次', exact: true }).click();
    await expect(page.getByRole('heading', { name: '批次 · 已完成', exact: true })).toBeVisible();
    batch = await api('GET', batchUrl);
    expect(batch.failed_count).toBe(0);
    expect(batch.done_count).toBe(batch.total);
    const outcomes = await api('GET', batch.items[1].outcomes_url);
    expect(outcomes.items.every((v: { annotation_ids: number[] }) => v.annotation_ids.length === 2)).toBe(
      true,
    );
    await page.getByLabel('批次范围').selectOption('uncovered');
    const table = page.getByRole('table', { name: '阶段与预计处理数量' });
    await expect(table).toBeVisible();
    await expect(table.locator('tbody tr').first().locator('td').nth(2)).toHaveText(
      String(batch.image_count),
    );
    await expect(table.locator('tbody tr').nth(1).locator('td').nth(2)).toHaveText(String(batch.image_count));
    await page.getByRole('button', { name: '开始标注批次', exact: true }).click();
    await expect.poll(currentBatch).not.toBe(batchId);
    await expect(page.getByRole('heading', { name: '批次 · 已完成', exact: true })).toBeVisible();
    const cachedUrl = p + '/jobs/batches/' + currentBatch();
    const cached = await api('GET', cachedUrl);
    expect(cached.skipped_count).toBe(cached.total);
    expect(cached.done_count).toBe(0);
    await page.screenshot({ path: join(temporary, 'f15-combined-batches.png'), fullPage: true });
    writeFileSync(hold, 'hold cancellation fixture');
    unlinkSync(entered);
    await page.getByLabel('批次范围').selectOption('all');
    await startAll();
    await expect.poll(() => existsSync(entered)).toBe(true);
    const canceledUrl = p + '/jobs/batches/' + currentBatch();
    await page.getByRole('button', { name: '取消批次', exact: true }).click();
    await expect(page.getByRole('heading', { name: '批次 · 已取消', exact: true })).toBeVisible();
    unlinkSync(hold);
    const canceled = await api('GET', canceledUrl);
    expect(canceled.items.map((v: { status: string }) => v.status)).toEqual(['canceled', 'canceled']);
    expect(canceled.done_count).toBe(0);
    expect(errors).toEqual([]);
  } finally {
    if (existsSync(hold)) unlinkSync(hold);
    if (existsSync(entered)) unlinkSync(entered);
  }
});

test('工作台执行器保留筛选全集、网格交互和关闭后的任务指示', async ({ page }) => {
  const hold = join(base, 'hold-wd-inference'),
    entered = join(base, 'wd-inference-entered');
  writeFileSync(hold, 'workbench scope fixture');
  if (existsSync(entered)) unlinkSync(entered);
  try {
    await connect(page);
    const project = await api('GET', '/api/projects/current'),
      p = `/api/projects/${project.project_id}`;
    const target = (await api('GET', p + '/images?file_missing=false&limit=100')).items[0];
    const rated = await api(
      'PATCH',
      p + `/images/${target.image_id}`,
      { rating: 'explicit' },
      target.version,
    );
    await api('POST', p + '/images/review', {
      ids: [target.image_id],
      target: 'keep',
      expected_versions: { [target.image_id]: rated.version },
    });
    await page.getByRole('link', { name: '数据工作台', exact: true }).click();
    await page.getByLabel('来源筛选').selectOption(String(target.source_id));
    await page.getByLabel('审核筛选').selectOption('keep');
    await page.getByLabel('评级筛选').selectOption('explicit');
    await page.getByLabel('搜索标签').fill('geometric');
    await expect(page.getByRole('gridcell')).toHaveCount(1);
    await page.getByRole('button', { name: /全选筛选结果/ }).click();
    await expect
      .poll(() => new URL(page.url().split('#')[1], 'http://localhost').searchParams.get('q'))
      .toBe('geometric');
    await page.getByRole('button', { name: '执行器批次', exact: true }).click();
    const panel = page.getByRole('dialog', { name: '工作台执行器', exact: true });
    await expect(panel).toHaveAttribute('aria-modal', 'false');
    const gridBounds = await page.getByRole('grid', { name: '图片列表' }).boundingBox();
    const panelBounds = await panel.boundingBox();
    expect(Math.round(gridBounds!.x + gridBounds!.width)).toBeLessThanOrEqual(Math.round(panelBounds!.x));
    await page.getByRole('gridcell').first().click();
    await expect(page.getByRole('gridcell').first()).toBeFocused();
    await expect(panel).toBeVisible();
    await page.getByRole('button', { name: /全选筛选结果/ }).click();
    await panel.getByLabel('LLM 视觉阶段').uncheck();
    await panel.getByRole('checkbox', { name: 'WD 标签阶段', exact: true }).check();
    await panel.getByLabel('批次 WD 模型').selectOption({ label: 'synthetic-tagger · aaaaaaaa' });
    await panel.getByLabel('批次通用标签阈值').fill('0.1');
    await panel.getByLabel('批次角色标签阈值').fill('0.1');
    await panel.getByLabel('批次范围').selectOption('selected');
    await page.getByLabel('搜索标签').fill('no-matching-tag');
    await expect(page.getByRole('heading', { name: '当前筛选下没有图片' })).toBeVisible();
    await expect(panel.getByRole('table').locator('tbody tr').first().locator('td').nth(1)).toHaveText('1');
    await panel.getByRole('button', { name: '开始标注批次', exact: true }).click();
    await expect.poll(() => existsSync(entered)).toBe(true);
    const current = new URL(page.url().split('#')[1], 'http://localhost');
    expect(current.searchParams.get('q')).toBe('no-matching-tag');
    const batchUrl = p + '/jobs/batches/' + current.searchParams.get('executor_batch');
    const batch = await api('GET', batchUrl);
    expect(batch.image_count).toBe(1);
    expect(batch.items[0].scope_json.filter).toMatchObject({
      q: 'geometric',
      rating: ['explicit'],
      review_statuses: ['keep'],
      source_ids: [target.source_id],
    });
    await panel.getByRole('button', { name: '关闭执行器面板' }).click();
    await expect(panel).not.toBeVisible();
    await expect(page.getByRole('button', { name: '查看当前标注批次' })).toContainText('运行中');
    unlinkSync(hold);
    await expect(page.getByRole('button', { name: '查看当前标注批次' })).toContainText('已完成');
    await page.getByRole('button', { name: '查看当前标注批次' }).click();
    await expect(panel.getByRole('heading', { name: '批次 · 已完成', exact: true })).toBeVisible();
    await page.screenshot({ path: join(temporary, 'f14-executor-panel.png'), fullPage: true });
    await panel.getByRole('button', { name: '关闭执行器面板' }).focus();
    await page.keyboard.press('Escape');
    await expect(panel).not.toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    if (existsSync(hold)) unlinkSync(hold);
    if (existsSync(entered)) unlinkSync(entered);
  }
});

test('失败列表仅重跑失败图片并保留原批次记录', async ({ page }) => {
  const failure = join(base, 'fail-next-wd-inference');
  writeFileSync(failure, 'one synthetic inference failure');
  try {
    await connect(page);
    const project = await api('GET', '/api/projects/current'),
      p = `/api/projects/${project.project_id}`;
    const id = (await api('GET', p + '/images?file_missing=false&limit=1')).items[0].image_id;
    const model = (await api('GET', '/api/tagger/models')).items.find(
      (m: { model_id: string }) => m.model_id === 'synthetic-tagger',
    );
    const accepted = await api('POST', p + '/jobs/batches', {
      batch_id: crypto.randomUUID(),
      executors: ['wd'],
      mode: 'all',
      scope: { selected_ids: [id] },
      config: { manifest_id: model.manifest_id, general_threshold: 0, character_threshold: 0 },
    });
    await expect.poll(async () => (await api('GET', accepted.status_url)).status).toBe('done');
    const original = await api('GET', accepted.status_url);
    expect(original.failed_count).toBe(1);
    await page.getByRole('link', { name: 'WD 标注任务', exact: true }).click();
    await page.getByRole('button', { name: `查看任务 ${original.job_id}`, exact: true }).click();
    await page.getByRole('button', { name: '查看失败图片（1）' }).click();
    await expect(
      page.getByRole('cell', { name: '本机模型推理失败，该图片未写入成功标注。', exact: true }),
    ).toBeVisible();
    await page.getByRole('button', { name: '仅重跑失败项', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('1 张失败图片');
    await dialog.getByRole('button', { name: '确认重跑失败项' }).click();
    await expect(page.getByRole('heading', { name: '批次 · 已完成', exact: true })).toBeVisible();
    const retryId = new URL(page.url().split('#')[1], 'http://localhost').searchParams.get('batch');
    const retried = await api('GET', p + '/jobs/batches/' + retryId);
    expect(retried.total).toBe(1);
    expect(retried.done_count).toBe(1);
    expect(retried.items[0].job_id).not.toBe(original.job_id);
    expect(retried.items[0].config_snapshot_json.params.retry_origin.job_id).toBe(original.job_id);
    expect((await api('GET', accepted.status_url)).failed_count).toBe(1);
    await page.screenshot({ path: join(temporary, 'f14-failed-retry.png'), fullPage: true });
    expect(errors).toEqual([]);
  } finally {
    if (existsSync(failure)) unlinkSync(failure);
  }
});

test('本地提示词词典、覆盖预览、持久任务与冻结 JSON 导出', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await expect(page.getByRole('heading', { name: '提示词生产', exact: true })).toBeVisible();
  await page.getByLabel('词典名称', { exact: true }).fill('浏览器覆盖词典');
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '浏览器覆盖词典', exact: true })).toBeVisible();
  await page.getByRole('checkbox', { name: '浏览器覆盖词典', exact: true }).check();
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  await expect(page.getByRole('button', { name: '创建本地采样任务', exact: true })).toBeEnabled();
  await expect(page.getByLabel('覆盖报告')).toContainText('已选 100 / 100 条');
  await page.getByRole('button', { name: '创建本地采样任务', exact: true }).click();
  const detail = page.getByLabel('提示词任务详情');
  await expect(detail.getByRole('heading', { name: '任务：已完成' })).toBeVisible();
  const exported = page.waitForEvent('download');
  await detail.getByRole('button', { name: '导出冻结提示词 JSON' }).click();
  const file = await exported;
  const seeds = JSON.parse(readFileSync((await file.path())!, 'utf8'));
  expect(seeds.count).toBe(100);
  expect(seeds.items.map((row: { seq: number }) => row.seq)).toEqual(
    Array.from({ length: 100 }, (_, i) => i + 1),
  );
  expect(Object.keys(seeds.items[0]).sort()).toEqual(['aspect', 'prompt', 'seq']);
  expect(
    seeds.items.every((row: { prompt: string }) => !row.prompt.includes('geometric style')),
  ).toBeTruthy();
  await expect(detail.getByRole('button', { name: '下一页结果' })).toBeEnabled();
  await detail.getByRole('button', { name: '下一页结果' }).click();
  await expect(detail.getByRole('cell', { name: '51', exact: true })).toBeVisible();
  await detail.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f06-local-prompt-task.png') });
  expect(errors).toEqual([]);
});
test('就绪提示词交接到生成源草稿，未适配后端保持只读预览', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  const tasks = await api('GET', '/api/prompt-production/tasks');
  const task = tasks.items.find(
    (row: { ready: boolean; requested: number }) => row.ready && row.requested === 100,
  );
  expect(task).toBeTruthy();
  await page.getByLabel('查看任务', { exact: true }).selectOption(task.task_id);
  await page.getByRole('link', { name: '交接到当前项目的生成源' }).click();
  await expect(page.getByLabel('已就绪的提示词任务')).toHaveValue(task.task_id);
  const capabilities = await api('GET', '/api/generation/capabilities');
  expect(capabilities.generation).toBe(false);
  // Explicit test-only parameters exercise saving and preview; they never enable generation.
  const config = {
    schema_version: 1,
    backend: 'nai_launcher',
    default_aspect: 'portrait',
    sizes: capabilities.size_presets.small,
    profile: {
      model: 'nai-diffusion-5-full',
      effort: 'high',
      sampler: 'fixture-sampler',
      steps: 17,
      scale: 4.5,
      cfg_rescale: 0,
      noise_schedule: 'fixture-schedule',
      quality_toggle: false,
      uc_preset: null,
      smea: false,
      smea_dyn: false,
      variety_plus: false,
      decrisp: false,
    },
  };
  await page.getByLabel('生成源名称').fill('浏览器生成源草稿');
  await page.getByLabel('画风提示词').fill('geometric style');
  await page.getByLabel('生成负面词').fill('unwanted objects');
  await page.getByText('高级 JSON 配置', { exact: true }).click();
  await page.getByLabel('生成配置（JSON）').fill(JSON.stringify(config, null, 2));
  await page.getByRole('button', { name: '保存生成源草稿', exact: true }).click();
  await expect(page.getByText('生成源草稿已保存；尚未锁定或提交生成。')).toBeVisible();
  await page.getByRole('button', { name: '预览提示词交接', exact: true }).click();
  const preview = page.getByLabel('提示词交接预览');
  await expect(preview).toContainText('完整交接 100 条');
  await expect(preview).toContainText('真实生成适配尚未开放');
  await expect(page.getByRole('button', { name: '确认交接并启动' })).toBeDisabled();
  await preview.getByText('查看发送内容和尺寸样例（最多 100 条）').click();
  await expect(preview.getByRole('row').nth(1)).toContainText('geometric style,');
  await expect(preview.getByRole('row').nth(1)).toContainText('unwanted objects');
  const project = await api('GET', '/api/projects/current');
  const p = `/api/projects/${project.project_id}`;
  const sources = await api('GET', p + '/sources');
  const source = sources.items.find((row: { name: string }) => row.name === '浏览器生成源草稿');
  expect(source.status).toBe('draft');
  expect(source.locked).toBe(false);
  expect((await api('GET', p + `/sources/${source.source_id}/items`)).items).toEqual([]);
  expect((await api('GET', '/api/prompt-production/tasks/' + task.task_id)).revision).toBe(task.revision);
  await page.getByLabel('画风提示词').fill('edited style');
  await expect(preview).not.toBeVisible();
  await expect(page.getByRole('button', { name: '预览提示词交接' })).toBeDisabled();
  const latest = await api('GET', p + `/sources/${source.source_id}`);
  await api(
    'PUT',
    p + `/sources/${source.source_id}`,
    {
      name: latest.name,
      target_count: latest.target_count,
      config: latest.config_json,
      style_prompt: 'external style',
      negative_prompt: latest.negative_prompt,
      cost_policy: latest.cost_policy,
    },
    latest.version,
  );
  await page.getByRole('button', { name: '保存生成源草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('另一次操作修改');
  await expect(page.getByLabel('画风提示词')).toHaveValue('edited style');
  await page.screenshot({ path: join(temporary, 'f06-generation-handoff.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('词典变化阻止旧覆盖预览建任务，导入错误保留词典', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  const dictionaries = await api('GET', '/api/prompt-production/dictionaries');
  const summary = dictionaries.items.find((row: { name: string }) => row.name === '浏览器覆盖词典');
  expect(summary).toBeTruthy();
  const path = '/api/prompt-production/dictionaries/' + summary.dictionary_id;
  const before = await api('GET', path);
  const tasks = await api('GET', '/api/prompt-production/tasks');
  await page.getByRole('checkbox', { name: '浏览器覆盖词典', exact: true }).check();
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  await expect(page.getByRole('button', { name: '创建本地采样任务', exact: true })).toBeEnabled();
  await api(
    'PUT',
    path,
    { schema_version: 1, kind: before.kind, name: '浏览器覆盖词典新版', dimensions: before.dimensions },
    before.version,
  );
  await page.getByRole('button', { name: '创建本地采样任务', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('词典或规则在预览后发生变化');
  expect((await api('GET', '/api/prompt-production/tasks')).total).toBe(tasks.total);
  await page.getByLabel('打开已有词典').selectOption(summary.dictionary_id);
  await expect(page.getByLabel('词典名称', { exact: true })).toHaveValue('浏览器覆盖词典新版');
  await page.getByText('从文件追加内容', { exact: true }).click();
  await page.getByLabel('导入格式', { exact: true }).selectOption('csv');
  await page.getByLabel('选择本地词典文件').setInputFiles({
    name: 'invalid.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('dimension,tag,rating\nscene,forest,invalid\n'),
  });
  await page.getByRole('button', { name: '预览导入', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认追加到词典', exact: true })).toBeDisabled();
  await expect(page.getByText(/存在错误，未写入词典/)).toBeVisible();
  expect((await api('GET', path)).revision).toBe(before.revision + 1);
  expect(errors).toEqual([]);
});
test('提示词精修确认、结果对照及词典追加闭环', async ({ page }) => {
  const roles = await api('GET', '/api/llm/roles');
  await api(
    'PUT',
    '/api/llm/roles',
    { roles: { ...roles.roles, prompt: roles.roles.vision } },
    roles.version,
  );
  const target = crypto.randomUUID();
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: target,
    kind: 'prompts',
    name: '精修结果词典',
    entries: [],
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('checkbox', { name: '浏览器覆盖词典新版', exact: true }).check();
  await page.getByLabel('目标数量').fill('12');
  await page.getByLabel('启用 LLM 精修').check();
  await page.getByLabel('精修选项（JSON）').fill(JSON.stringify({ nl_enabled: true, tag_range: [1, 20] }));
  await page.getByRole('button', { name: '预览覆盖与样例' }).click();
  const create = page.getByRole('button', { name: '确认创建精修任务' });
  await expect(create).toBeDisabled();
  await page.getByRole('checkbox', { name: /确认创建任务后调用/ }).check();
  await create.click();
  const detail = page.getByLabel('提示词任务详情');
  await expect(detail.getByRole('heading', { name: '任务：已完成' })).toBeVisible();
  await expect(detail.getByText('Trees stand beside a river.', { exact: true }).first()).toBeVisible();
  await detail.getByText('查看原值', { exact: true }).first().click();
  await detail.getByText('将结果追加到提示词词典', { exact: true }).click();
  await detail.getByLabel('追加目标词典').selectOption(target);
  await detail.getByRole('button', { name: '选择全部合格结果' }).click();
  await detail.getByRole('button', { name: '预览追加结果' }).click();
  await expect(detail.getByText('选中 12 条；预计新增 12 条，去重 0 条。')).toBeVisible();
  await detail.getByRole('button', { name: '确认追加任务结果' }).click();
  await expect(detail.getByText('词典追加已完成：新增 12 条，重复 0 条。')).toBeVisible();
  expect((await api('GET', '/api/prompt-production/dictionaries/' + target)).entries.length).toBe(12);
  await detail.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f06-composer-append.png') });
  expect(errors).toEqual([]);
});
test('WD 标注暂停恢复、取消迟到结果及项目重开后的记录', async ({ page }) => {
  const hold = join(base, 'hold-wd-inference'),
    entered = join(base, 'wd-inference-entered');
  writeFileSync(hold, 'hold current fixture inference');
  if (existsSync(entered)) unlinkSync(entered);
  try {
    await connect(page);
    const project = await api('GET', '/api/projects/current'),
      p = `/api/projects/${project.project_id}`;
    await page.getByRole('link', { name: 'WD 标注任务', exact: true }).click();
    await page.getByLabel('已校验的模型').selectOption({ label: 'synthetic-tagger · aaaaaaaa' });
    await page.getByLabel('执行范围').selectOption('all');
    await page.getByLabel('通用标签阈值（0–1，必填）').fill('0');
    await page.getByLabel('角色标签阈值（0–1，必填）').fill('0');
    await page.getByRole('button', { name: '开始标注', exact: true }).click();
    await expect.poll(() => existsSync(entered)).toBe(true);
    const job = (await api('GET', p + '/jobs')).items.at(-1);
    await page.getByRole('button', { name: '暂停标注', exact: true }).click();
    unlinkSync(hold);
    await expect.poll(async () => (await api('GET', job.status_url)).done_count).toBe(1);
    await expect(
      page.getByRole('heading', { name: `任务 #${job.job_id} · 已暂停`, exact: true }),
    ).toBeVisible();
    await page.getByRole('button', { name: '恢复标注', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: `任务 #${job.job_id} · 已完成`, exact: true }),
    ).toBeVisible();
    writeFileSync(hold, 'hold cancel fixture');
    unlinkSync(entered);
    await page.getByRole('button', { name: '开始标注', exact: true }).click();
    await expect.poll(() => existsSync(entered)).toBe(true);
    const cancelJob = (await api('GET', p + '/jobs')).items.at(-1);
    await page.getByRole('button', { name: '取消标注', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: `任务 #${cancelJob.job_id} · 已取消`, exact: true }),
    ).toBeVisible();
    unlinkSync(hold);
    expect((await api('GET', cancelJob.status_url)).done_count).toBe(0);
    const fresh = await api('GET', p);
    const closing = await api('POST', p + '/close', {}, fresh.version);
    await expect.poll(async () => (await api('GET', closing.status_url)).status).toBe('done');
    const opened = await api('POST', '/api/filesystem/grants', { purpose: 'project_open' });
    await expect.poll(async () => (await api('GET', opened.status_url)).status).toBe('done');
    const grant = (await api('GET', opened.status_url)).result;
    const reopening = await api('POST', '/api/projects/open', { grant_id: grant.grant_id });
    await expect.poll(async () => (await api('GET', reopening.status_url)).status).toBe('done');
    const current = await api('GET', '/api/projects/current');
    const reopened = await api('GET', `/api/projects/${current.project_id}/jobs/${cancelJob.job_id}`);
    expect(reopened.status).toBe('canceled');
    expect(reopened.done_count).toBe(0);
    expect(errors).toEqual([]);
  } finally {
    if (existsSync(hold)) unlinkSync(hold);
    if (existsSync(entered)) unlinkSync(entered);
  }
});

test('提示词部分结果导出保留结构，TXT 必须确认信息损失', async ({ page }) => {
  const failure = join(base, 'fail-prompt-tree-zero');
  writeFileSync(failure, 'reject one local synthetic candidate');
  try {
    const id = crypto.randomUUID();
    await api('POST', '/api/prompt-production/dictionaries', {
      dictionary_id: id,
      kind: 'prompts',
      name: '部分导出词典',
      entries: Array.from({ length: 4 }, (_, i) => ({
        tags: ['no humans', 'forest', `tree ${i}`],
        rating: 'general',
        aspect: 'landscape',
      })),
    });
    const preview = await api('POST', '/api/prompt-production/coverage/preview', {
      prompt_dictionary_ids: [id],
      coverage_enabled: false,
      optimize: true,
      options: { tag_range: [1, 20] },
      plan: { target_count: 4, random_seed: 42 },
    });
    const taskId = crypto.randomUUID();
    await api('POST', '/api/prompt-production/tasks', {
      task_id: taskId,
      request: preview.request,
      preview_digest: preview.preview_digest,
    });
    const path = '/api/prompt-production/tasks/' + taskId;
    await expect.poll(async () => (await api('GET', path)).can_export_results).toBe(true);
    const original = await api('GET', path);
    expect(original.ready).toBe(false);
    await connect(page);
    await page.getByRole('link', { name: '提示词生产', exact: true }).click();
    await page.getByLabel('查看任务', { exact: true }).selectOption(taskId);
    await expect(page.getByRole('button', { name: '导出冻结提示词 JSON' })).toBeDisabled();
    const panel = page.getByLabel('合格结果导出', { exact: true });
    for (const format of ['json', 'csv', 'txt']) {
      await panel.getByLabel('结果文件格式').selectOption(format);
      await panel.getByRole('button', { name: '预览合格结果导出' }).click();
      const report = panel.getByLabel('结果导出预览');
      await expect(report).toContainText('合格结果 3 条 · 源任务目标 4 条 · 源任务尚未就绪');
      const download = panel.getByRole('button', { name: '下载合格结果文件' });
      if (format === 'txt') {
        await expect(download).toBeDisabled();
        await panel.getByLabel('确认接受 TXT 导出的信息损失').check();
      }
      const downloaded = page.waitForEvent('download');
      await download.click();
      const file = await downloaded;
      const content = readFileSync((await file.path())!, 'utf8');
      if (format === 'json') {
        const data = JSON.parse(content);
        expect(data.kind).toBe('prompt_task_results');
        expect(data.source_ready).toBe(false);
        expect(data.usable_for_start).toBe(false);
        expect(data.rows).toHaveLength(3);
        expect(
          data.rows.every((row: { entry: { aspect: string } }) => row.entry.aspect === 'landscape'),
        ).toBe(true);
      } else if (format === 'csv') {
        expect(content).toContain('rating_verified,refinement_status,selected_for_seeds');
        expect(content).toContain('landscape');
      } else expect(content.trim().split('\n')).toHaveLength(3);
    }
    expect((await api('GET', path)).revision).toBe(original.revision);
    await panel.scrollIntoViewIfNeeded();
    await page.screenshot({ path: join(temporary, 'f06-partial-results.png') });
    expect(errors).toEqual([]);
  } finally {
    if (existsSync(failure)) unlinkSync(failure);
  }
});

test('提示词任务占用、批量清理确认与项目来源独立', async ({ page }) => {
  const tasks = await api('GET', '/api/prompt-production/tasks');
  const partial = tasks.items.find(
    (row: { requested: number; ready: boolean }) => row.requested === 4 && !row.ready,
  );
  const full = tasks.items.find(
    (row: { requested: number; ready: boolean }) => row.requested === 100 && row.ready,
  );
  expect(partial).toBeTruthy();
  expect(full).toBeTruthy();
  const project = await api('GET', '/api/projects/current');
  const sources = await api('GET', `/api/projects/${project.project_id}/sources`);
  const dictionaries = await api('GET', '/api/prompt-production/dictionaries');
  const before = await api('GET', '/api/prompt-production/task-storage');
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('查看任务', { exact: true }).selectOption(partial.task_id);
  const panel = page.getByLabel('提示词任务存储与清理');
  for (const row of [partial, full])
    await panel.getByRole('checkbox', { name: '清理任务 ' + row.task_id.slice(0, 8) }).check();
  await panel.getByRole('button', { name: '清理所选任务（2）' }).click();
  const dialog = page.getByRole('dialog', { name: '确认清理提示词任务' });
  const remove = dialog.getByRole('button', { name: /按住清理任务/ });
  await expect(remove).toBeDisabled();
  await dialog.getByLabel('我已保存需要的结果，确认清理这些任务').check();
  await remove.focus();
  await page.keyboard.down('Space');
  await page.waitForTimeout(1100);
  await page.keyboard.up('Space');
  await expect(page.getByText('已清理 2 个任务，保留最小去重记录。')).toBeVisible();
  await expect(page.getByLabel('提示词任务详情')).not.toBeVisible();
  await expect(
    panel.getByRole('checkbox', { name: '清理任务 ' + partial.task_id.slice(0, 8) }),
  ).not.toBeVisible();
  const after = await api('GET', '/api/prompt-production/task-storage');
  expect(after.total).toBe(before.total - 2);
  expect(after.storage_bytes).toBeLessThan(before.storage_bytes);
  expect(after.receipt_count).toBe(before.receipt_count + 2);
  expect(await api('GET', `/api/projects/${project.project_id}/sources`)).toEqual(sources);
  expect(await api('GET', '/api/prompt-production/dictionaries')).toEqual(dictionaries);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f06-task-cleanup.png') });
  expect(errors).toEqual([]);
});

test('覆盖方案保存、JSON YAML 交换、载入采样与冻结导出闭环', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('词典名称', { exact: true }).fill('方案闭环词典');
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '方案闭环词典', exact: true })).toBeVisible();
  // The SSE list refresh may precede the save handler's detail read and draft reset.
  await expect(page.getByText(/已保存版本 1；合并重复条目/)).toBeVisible();
  await page.getByRole('link', { name: '管理覆盖方案' }).click();
  await expect(page.getByRole('heading', { name: '覆盖方案', exact: true })).toBeVisible();
  const editor = page.getByLabel('覆盖方案模板', { exact: true });
  const template = JSON.parse(await editor.inputValue());
  template.name = '浏览器六格方案';
  template.plan.target_count = 12;
  template.plan.random_seed = 123;
  await editor.fill(JSON.stringify(template, null, 2));
  await page.getByRole('button', { name: '校验模板' }).click();
  await expect(page.getByText('模板结构校验通过，尚未保存。')).toBeVisible();
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect(page.getByRole('heading', { name: '编辑 浏览器六格方案 · r1' })).toBeVisible();
  let yaml = '';
  for (const format of ['JSON', 'YAML']) {
    const exported = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出 ' + format + ' 模板' }).click();
    const content = readFileSync((await (await exported).path())!, 'utf8');
    expect(content).not.toContain('_operations');
    expect(content).not.toContain('coverage_id');
    if (format === 'YAML') yaml = content;
    else expect(JSON.parse(content).plan.random_seed).toBe(123);
  }
  await page.getByLabel('选择本地覆盖模板').setInputFiles({
    name: 'coverage.yaml',
    mimeType: 'application/yaml',
    buffer: Buffer.from(yaml.replace('浏览器六格方案', 'YAML 六格副本'), 'utf8'),
  });
  await page.getByRole('button', { name: '载入文件为新方案草稿' }).click();
  await expect(page.getByRole('heading', { name: '新方案', exact: true })).toBeVisible();
  expect((await api('GET', '/api/prompt-production/coverage-plans')).total).toBe(1);
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect(page.getByRole('heading', { name: '编辑 YAML 六格副本 · r1' })).toBeVisible();
  const plans = await api('GET', '/api/prompt-production/coverage-plans');
  const copy = plans.items.find((row: { name: string }) => row.name === 'YAML 六格副本');
  const saved = await api('GET', '/api/prompt-production/coverage-plans/' + copy.coverage_id);
  await page.getByLabel('覆盖方案编辑器').scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f06-coverage-plans.png') });
  await page.getByRole('link', { name: '返回提示词生产' }).click();
  await page.getByRole('checkbox', { name: '方案闭环词典', exact: true }).check();
  await page.getByLabel('已保存的覆盖方案').selectOption(copy.coverage_id);
  await page.getByRole('button', { name: '载入并替换覆盖设置' }).click();
  await expect(page.getByLabel('目标数量', { exact: true })).toHaveValue('12');
  await expect(page.getByLabel('随机种子', { exact: true })).toHaveValue('123');
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  await expect(page.getByRole('button', { name: '创建本地采样任务', exact: true })).toBeEnabled();
  await expect(page.getByLabel('覆盖报告')).toContainText('已选 12 / 12 条');
  await page.getByRole('button', { name: '载入并替换覆盖设置' }).click();
  await expect(page.getByRole('button', { name: '创建本地采样任务', exact: true })).not.toBeVisible();
  await expect(page.getByLabel('覆盖报告')).not.toBeVisible();
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  await page.getByRole('button', { name: '创建本地采样任务', exact: true }).click();
  const detail = page.getByLabel('提示词任务详情');
  await expect(detail.getByRole('heading', { name: '任务：已完成' })).toBeVisible();
  const oldTask = await page.getByLabel('查看任务', { exact: true }).inputValue();
  const before = await api('GET', '/api/prompt-production/tasks/' + oldTask);
  const updated = JSON.parse(saved.template);
  updated.plan.random_seed = 987;
  updated.plan.target_count = 24;
  await api(
    'PUT',
    '/api/prompt-production/coverage-plans/' + copy.coverage_id,
    { format: 'json', content: JSON.stringify(updated) },
    saved.version,
  );
  expect(await api('GET', '/api/prompt-production/tasks/' + oldTask)).toEqual(before);
  const exported = page.waitForEvent('download');
  await detail.getByRole('button', { name: '导出冻结提示词 JSON' }).click();
  expect(JSON.parse(readFileSync((await (await exported).path())!, 'utf8')).count).toBe(12);
  expect(errors).toEqual([]);
});

test('覆盖方案冲突与非法 YAML 导入保留草稿', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('link', { name: '管理覆盖方案' }).click();
  await page.getByRole('button', { name: '编辑 YAML 六格副本', exact: true }).click();
  await expect(page.getByRole('heading', { name: '编辑 YAML 六格副本 · r2' })).toBeVisible();
  const editor = page.getByLabel('覆盖方案模板', { exact: true });
  const draft = JSON.parse(await editor.inputValue());
  draft.plan.target_count = 36;
  const text = JSON.stringify(draft, null, 2);
  await editor.fill(text);
  await page.getByLabel('选择本地覆盖模板').setInputFiles({
    name: 'duplicate.yaml',
    mimeType: 'application/yaml',
    buffer: Buffer.from('name: a\nname: b\nplan: {}'),
  });
  await page.getByRole('button', { name: '载入文件为新方案草稿' }).click();
  await expect(page.getByText(/覆盖模板无效/)).toBeVisible();
  await expect(editor).toHaveValue(text);
  await expect(page.getByRole('dialog')).not.toBeVisible();
  const plans = await api('GET', '/api/prompt-production/coverage-plans');
  const row = plans.items.find((value: { name: string }) => value.name === 'YAML 六格副本');
  const current = await api('GET', '/api/prompt-production/coverage-plans/' + row.coverage_id);
  const newer = JSON.parse(current.template);
  newer.plan.target_count = 48;
  await api(
    'PUT',
    '/api/prompt-production/coverage-plans/' + row.coverage_id,
    { format: 'json', content: JSON.stringify(newer) },
    current.version,
  );
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect(page.getByText('内容已被另一次操作修改，请核对最新内容。')).toBeVisible();
  await expect(editor).toHaveValue(text);
  await page.getByRole('link', { name: '返回提示词生产' }).click();
  const dialog = page.getByRole('dialog', { name: '有未保存的覆盖方案' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '继续编辑' }).click();
  await expect(editor).toHaveValue(text);
  await page.getByRole('button', { name: '重新载入已保存版本' }).click();
  await dialog.getByRole('button', { name: '放弃草稿' }).click();
  await expect(page.getByRole('heading', { name: '编辑 YAML 六格副本 · r3' })).toBeVisible();
  expect(JSON.parse(await editor.inputValue()).plan.target_count).toBe(48);
  expect(errors).toEqual([]);
});

async function awaitingPromptReview() {
  let roles = await api('GET', '/api/llm/roles');
  if (!roles.roles.prompt) {
    const endpoint = await api('POST', '/api/llm/endpoints', {
      base_url: 'http://127.0.0.1:19333/v1',
      protocol: 'openai_chat',
      models: [{ name: 'fixture/vision', supports_images: false }],
    });
    await api('POST', `/api/llm/endpoints/${endpoint.endpoint_id}/tokens`, {
      value: 'browser-review-fixture-secret',
    });
    roles = await api('GET', '/api/llm/roles');
    roles = await api(
      'PUT',
      '/api/llm/roles',
      { roles: { ...roles.roles, prompt: { endpoint_id: endpoint.endpoint_id, model: 'fixture/vision' } } },
      roles.version,
    );
  }
  const dictionaryId = crypto.randomUUID();
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: dictionaryId,
    kind: 'prompts',
    name: '人工复验输入',
    entries: [
      {
        tags: ['no humans', 'forest', 'sunlight', 'tree review'],
        natural_language: null,
        aspect: 'landscape',
        rating: 'general',
      },
    ],
  });
  const flag = join(base, 'review-prompt-output');
  writeFileSync(flag, '1');
  try {
    const request = {
      prompt_dictionary_ids: [dictionaryId],
      optimize: true,
      coverage_enabled: false,
      plan: { target_count: 1, random_seed: 42 },
      options: { tag_range: [1, 20] },
    };
    const preview = await api('POST', '/api/prompt-production/coverage/preview', request);
    const task = await api('POST', '/api/prompt-production/tasks', {
      task_id: crypto.randomUUID(),
      request: preview.request,
      preview_digest: preview.preview_digest,
    });
    await expect
      .poll(async () => {
        const current = await api('GET', '/api/prompt-production/tasks/' + task.task_id);
        return current.status === 'done' && current.can_review;
      })
      .toBe(true);
    const current = await api('GET', '/api/prompt-production/tasks/' + task.task_id);
    const rows = await api('GET', '/api/prompt-production/tasks/' + task.task_id + '/outcomes');
    expect(rows.items[0].status).toBe('review_required');
    return { task: current, row: rows.items[0] };
  } finally {
    if (existsSync(flag)) unlinkSync(flag);
  }
}

test('提示词人工复验从待审核输出到冻结导出和词典追加', async ({ page }) => {
  const { task, row } = await awaitingPromptReview();
  const calls = readFileSync(join(base, 'prompt-calls.log'), 'utf8');
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('查看任务', { exact: true }).selectOption(task.task_id);
  await page.getByRole('link', { name: `编辑并复验 第 ${row.index + 1} 项` }).click();
  await expect(page.getByRole('heading', { name: `提示词人工复验 · 第 ${row.index + 1} 项` })).toBeVisible();
  const nl = page.getByLabel('自然语言描述', { exact: true });
  await expect(nl).toHaveValue('A dreamy forest surrounds the river.');
  await nl.fill('A watercolor painting.');
  await page.getByRole('button', { name: '复验修改并预览结果' }).click();
  await expect(page.getByText('复验未通过，原记录保持不变。')).toBeVisible();
  await expect(page.getByRole('button', { name: '确认保存复验结果' })).not.toBeVisible();
  await nl.fill('Trees stand beside a river.');
  await page.getByRole('link', { name: '返回任务结果' }).click();
  const dialog = page.getByRole('dialog', { name: '有未保存的提示词复验草稿' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '继续编辑' }).click();
  await expect(nl).toHaveValue('Trees stand beside a river.');
  await page.getByRole('button', { name: '复验修改并预览结果' }).click();
  await expect(page.getByText('复验通过，尚未保存。')).toBeVisible();
  const save = page.getByRole('button', { name: '确认保存复验结果' });
  await expect(save).toBeDisabled();
  await page.getByRole('checkbox', { name: /我已检查内容与评级/ }).check();
  await page.getByLabel('人工复验预览').scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f06-prompt-review.png') });
  await save.click();
  await expect(page.getByText('此条目已完成人工复验，当前为只读记录。')).toBeVisible();
  await expect(nl).toBeDisabled();
  const updated = await api('GET', '/api/prompt-production/tasks/' + task.task_id);
  expect(updated.ready).toBe(true);
  expect(updated.revision).toBe(task.revision + 1);
  const after = await api('GET', '/api/prompt-production/tasks/' + task.task_id + '/outcomes');
  expect(after.items[0].review.previous_status).toBe('review_required');
  expect(after.items[0].review_candidate.entry.natural_language).toContain('dreamy');
  await page.getByRole('link', { name: '返回任务结果' }).click();
  const detail = page.getByLabel('提示词任务详情');
  const download = page.waitForEvent('download');
  await detail.getByRole('button', { name: '导出冻结提示词 JSON' }).click();
  const seeds = JSON.parse(readFileSync((await (await download).path())!, 'utf8'));
  expect(seeds.count).toBe(1);
  expect(seeds.items[0].prompt).not.toContain('dreamy');
  const target = crypto.randomUUID();
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: target,
    kind: 'prompts',
    name: '人工复验输出',
    entries: [],
  });
  await detail.getByText('将结果追加到提示词词典', { exact: true }).click();
  await detail.getByLabel('追加目标词典').selectOption(target);
  await detail.getByRole('button', { name: '选择全部合格结果' }).click();
  await detail.getByRole('button', { name: '预览追加结果' }).click();
  await expect(detail.getByText('选中 1 条；预计新增 1 条，去重 0 条。')).toBeVisible();
  await detail.getByRole('button', { name: '确认追加任务结果' }).click();
  await expect(detail.getByText('词典追加已完成：新增 1 条，重复 0 条。')).toBeVisible();
  expect(readFileSync(join(base, 'prompt-calls.log'), 'utf8')).toBe(calls);
  expect(errors).toEqual([]);
});

test('提示词人工复验响应丢失后用同一请求核对且只保存一次', async ({ page }) => {
  const { task, row } = await awaitingPromptReview();
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('查看任务', { exact: true }).selectOption(task.task_id);
  await page.getByRole('link', { name: `编辑并复验 第 ${row.index + 1} 项` }).click();
  await page.getByLabel('自然语言描述', { exact: true }).fill('Trees stand beside a river.');
  await page.getByRole('button', { name: '复验修改并预览结果' }).click();
  await page.getByRole('checkbox', { name: /我已检查内容与评级/ }).check();
  const keys: string[] = [];
  await page.route(`**/api/prompt-production/tasks/${task.task_id}/reviews`, async (route) => {
    keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) {
      const response = await route.fetch();
      expect(response.ok()).toBe(true);
      await route.abort('failed');
    } else await route.continue();
  });
  const save = page.getByRole('button', { name: '确认保存复验结果' });
  await save.click();
  await expect(page.getByText('本次提交结果未知，请先查询任务和数据，不要重复提交。')).toBeVisible();
  await expect(page.getByLabel('自然语言描述', { exact: true })).toHaveValue('Trees stand beside a river.');
  await save.click();
  await expect(page.getByText('此条目已完成人工复验，当前为只读记录。')).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect((await api('GET', '/api/prompt-production/tasks/' + task.task_id)).revision).toBe(task.revision + 1);
  expect(errors).toEqual([]);
});

test('词典表单维护维度候选并报告保存后的去重评级合并', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('词典名称', { exact: true }).fill('表单特征词典');
  await page.getByRole('button', { name: '新增维度', exact: true }).click();
  await page.getByLabel('维度名称', { exact: true }).fill('weather');
  await page.getByLabel('候选标签', { exact: true }).fill('clear_sky');
  await page.getByLabel('条目评级', { exact: true }).selectOption('general');
  await page.getByRole('button', { name: '复制当前条目', exact: true }).click();
  await page.getByLabel('条目评级', { exact: true }).selectOption('sensitive');
  await page.getByRole('button', { name: '条目上移', exact: true }).click();
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByText('已保存版本 1；合并重复条目 1 条。')).toBeVisible();
  await expect(page.getByText(/维度「weather」的重复标签存在评级差异/)).toBeVisible();
  const id = await page.getByLabel('打开已有词典').inputValue();
  const saved = await api('GET', '/api/prompt-production/dictionaries/' + id);
  expect(saved.dimensions.weather).toEqual([{ tag: 'clear sky', rating: 'sensitive' }]);
  await page.getByLabel('当前特征维度').selectOption('weather');
  await page.getByRole('button', { name: '删除当前维度', exact: true }).click();
  await page.getByRole('button', { name: '确认从草稿删除', exact: true }).click();
  await page.getByRole('button', { name: '撤销最近一次删除', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存词典', exact: true })).toBeDisabled();
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出词典 JSON', exact: true }).click();
  const exported = JSON.parse(readFileSync((await (await downloaded).path())!, 'utf8'));
  expect(exported.dimensions).toEqual(saved.dimensions);
  expect(exported._operations).toBeUndefined();
  expect(errors).toEqual([]);
});

test('词典表单提示词编辑采样导出闭环及冻结任务不随词典变化', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('button', { name: '新建提示词词典', exact: true }).click();
  await page.getByLabel('词典名称', { exact: true }).fill('表单提示词闭环');
  await page.getByLabel('有序标签（每行一个）').fill('no humans\nforest\nsunlight\nwide_shot');
  await page.getByLabel('条目自然语言描述').fill('Trees surround a river.');
  await page.getByLabel('条目图幅').selectOption('landscape');
  await page.getByLabel('条目评级').selectOption('general');
  await page.getByRole('button', { name: '新增提示词条目', exact: true }).click();
  await page.getByLabel('有序标签（每行一个）').fill('no humans\nbeach\nsunlight\nwide shot');
  await page.getByLabel('条目图幅').selectOption('square');
  await page.getByLabel('条目评级').selectOption('general');
  await page.getByRole('button', { name: '条目上移', exact: true }).click();
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByText('已保存版本 1；合并重复条目 0 条。')).toBeVisible();
  const id = await page.getByLabel('打开已有词典').inputValue();
  const saved = await api('GET', '/api/prompt-production/dictionaries/' + id);
  expect(saved.entries.map((row: { tags: string[] }) => row.tags[1])).toEqual(['beach', 'forest']);
  expect(saved.entries[1].tags).toEqual(['no humans', 'forest', 'sunlight', 'wide shot']);
  await page.getByLabel('检索当前条目').fill('forest');
  await page.getByRole('button', { name: '编辑第 2 条', exact: true }).click();
  await page.getByLabel('条目自然语言描述').fill('Trees line a river.');
  await page.getByRole('button', { name: '新建特征词典', exact: true }).click();
  await page.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByLabel('条目自然语言描述')).toHaveValue('Trees line a river.');
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByText('已保存版本 2；合并重复条目 0 条。')).toBeVisible();
  await page.getByRole('checkbox', { name: '表单提示词闭环', exact: true }).check();
  await page.getByLabel('目标数量').fill('2');
  await page.getByRole('checkbox', { name: '启用覆盖格子配额' }).uncheck();
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  await page.getByRole('button', { name: '创建本地采样任务', exact: true }).click();
  const detail = page.getByLabel('提示词任务详情');
  await expect(detail.getByRole('heading', { name: '任务：已完成' })).toBeVisible();
  const taskId = new URL(page.url().split('#')[1], 'http://localhost').searchParams.get('task');
  const taskBefore = await api('GET', '/api/prompt-production/tasks/' + taskId);
  await page.getByRole('button', { name: '编辑第 1 条', exact: true }).click();
  await page.getByLabel('有序标签（每行一个）').fill('no humans\nmountain\nsunlight\nwide shot');
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByText('已保存版本 3；合并重复条目 0 条。')).toBeVisible();
  expect((await api('GET', '/api/prompt-production/tasks/' + taskId)).revision).toBe(taskBefore.revision);
  const downloaded = page.waitForEvent('download');
  await detail.getByRole('button', { name: '导出冻结提示词 JSON' }).click();
  const seeds = JSON.parse(readFileSync((await (await downloaded).path())!, 'utf8'));
  expect(seeds.count).toBe(2);
  expect(seeds.items.some((row: { prompt: string }) => row.prompt.includes('beach'))).toBe(true);
  expect(seeds.items.some((row: { prompt: string }) => row.prompt.includes('mountain'))).toBe(false);
  await page.getByRole('heading', { name: '提示词条目编辑' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f06-dictionary-editor.png') });
  await page
    .locator('fieldset')
    .filter({ has: page.getByLabel('有序标签（每行一个）') })
    .screenshot({ path: join(temporary, 'f06-dictionary-editor-fields.png') });
  expect(errors).toEqual([]);
});

test('词典表单冲突保留草稿并在响应丢失后用同一请求恢复', async ({ page }) => {
  const id = crypto.randomUUID(),
    p = '/api/prompt-production/dictionaries/' + id;
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: id,
    kind: 'prompts',
    name: '表单冲突测试',
    entries: [{ tags: ['no humans', 'forest'], rating: null }],
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('打开已有词典').selectOption(id);
  await expect(page.getByLabel('词典名称', { exact: true })).toHaveValue('表单冲突测试');
  await page.getByLabel('条目自然语言描述').fill('My local draft.');
  const before = await api('GET', p);
  await api('PUT', p, { kind: 'prompts', name: '外部新版', entries: before.entries }, before.version);
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByLabel('条目自然语言描述')).toHaveValue('My local draft.');
  await page.getByRole('button', { name: '重新载入已保存词典', exact: true }).click();
  await page.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByLabel('条目自然语言描述')).toHaveValue('My local draft.');
  await page.getByRole('button', { name: '重新载入已保存词典', exact: true }).click();
  await page.getByRole('button', { name: '放弃草稿', exact: true }).click();
  await expect(page.getByLabel('词典名称', { exact: true })).toHaveValue('外部新版');
  await page.getByLabel('条目自然语言描述').fill('Confirmed new draft.');
  const keys: string[] = [];
  await page.route('**' + p, async (route) => {
    if (route.request().method() !== 'PUT') {
      await route.continue();
      return;
    }
    keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) {
      expect((await route.fetch()).ok()).toBe(true);
      await route.abort('failed');
    } else await route.continue();
  });
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByText('本次提交结果未知，请先查询任务和数据，不要重复提交。')).toBeVisible();
  await expect(page.getByLabel('条目自然语言描述')).toHaveValue('Confirmed new draft.');
  await page.getByRole('button', { name: '保存词典', exact: true }).click();
  await expect(page.getByText('已保存版本 3；合并重复条目 0 条。')).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  const after = await api('GET', p);
  expect(after.revision).toBe(3);
  expect(after.entries[0].natural_language).toBe('Confirmed new draft.');
  expect(after.entries[0].rating).toBeNull();
  expect(errors).toEqual([]);
});

test('覆盖矩阵三维切片键盘明细缺口与排除格', async ({ page }) => {
  const id = crypto.randomUUID();
  const dims = {
    scene: ['forest', 'beach'],
    lighting: ['sunlight', 'night'],
    view: ['wide shot', 'upper body'],
  };
  const entries = dims.scene.flatMap((scene) =>
    dims.lighting.flatMap((lighting) =>
      dims.view.map((view) => ({ tags: ['no humans', scene, lighting, view], rating: 'general' })),
    ),
  );
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: id,
    kind: 'prompts',
    name: '三维矩阵验收',
    entries,
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('checkbox', { name: '三维矩阵验收', exact: true }).check();
  await page.getByLabel('目标数量').fill('7');
  await page.getByLabel('覆盖与约束规则（JSON）').fill(
    JSON.stringify({
      primary_dimensions: Object.keys(dims),
      cell_values: dims,
      constraints: [],
      exclude: [{ all_tags: ['beach', 'night', 'upper body'] }],
    }),
  );
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  const matrix = page.getByRole('region', { name: '覆盖矩阵', exact: true });
  const cell = matrix.getByRole('button', {
    name: 'scene: forest · lighting: sunlight · view: wide shot：目标 1，实际 1，满足格子配额',
    exact: true,
  });
  await cell.focus();
  await expect(cell).toBeFocused();
  await cell.press('Enter');
  await expect(matrix.getByRole('region', { name: '格子已选条目' })).toContainText(
    'no humans, forest, sunlight, wide shot',
  );
  await matrix.getByLabel('筛选维度 view').selectOption('upper body');
  await expect(matrix.getByRole('region', { name: '格子已选条目' })).toHaveCount(0);
  await expect(
    matrix.getByRole('button', {
      name: 'scene: beach · lighting: night · view: upper body：已排除',
      exact: true,
    }),
  ).toBeDisabled();
  await matrix.screenshot({ path: join(temporary, 'f06-coverage-matrix-light.png') });
  await page.evaluate(() => {
    document.documentElement.dataset.theme = 'dark';
  });
  await matrix.screenshot({ path: join(temporary, 'f06-coverage-matrix-dark.png') });
  await page.evaluate(() => {
    document.documentElement.dataset.theme = 'light';
  });
  await matrix.getByLabel('矩阵行维度').selectOption('view');
  await expect(matrix.getByLabel('筛选维度 scene')).toBeVisible();
  await matrix.getByLabel('筛选维度 scene').selectOption('beach');
  await expect(
    matrix.getByRole('button', {
      name: 'scene: beach · lighting: night · view: upper body：已排除',
      exact: true,
    }),
  ).toBeDisabled();
  await page.getByLabel('目标数量').fill('14');
  await expect(matrix).toHaveCount(0);
  await expect(page.getByText('采样条件已改变，请重新预览。')).toBeVisible();
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  await expect(
    matrix.getByRole('button', {
      name: /scene: forest · lighting: sunlight · view: wide shot：目标 2，实际 0，缺口 2/,
    }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: '创建本地采样任务', exact: true })).toBeDisabled();
  expect(errors).toEqual([]);
});

test('覆盖矩阵完整分页释放预览后仍可建任务并保持冻结明细', async ({ page }) => {
  const id = crypto.randomUUID();
  const entries = Array.from({ length: 205 }, (_, i) => ({
    tags: ['no humans', 'forest', 'sunlight', 'item ' + i],
    rating: 'general',
  }));
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: id,
    kind: 'prompts',
    name: '矩阵205条',
    entries,
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('checkbox', { name: '矩阵205条', exact: true }).check();
  await page.getByLabel('目标数量').fill('205');
  await page.getByLabel('覆盖与约束规则（JSON）').fill(
    JSON.stringify({
      primary_dimensions: ['scene', 'lighting'],
      cell_values: { scene: ['forest'], lighting: ['sunlight'] },
      constraints: [],
    }),
  );
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  const preview = page.getByRole('region', { name: '覆盖矩阵', exact: true }).first();
  await preview.getByRole('button', { name: /目标 205，实际 205/ }).click();
  await expect(preview.getByText('本格共 205 条 · 明细第 1 页')).toBeVisible();
  for (let number = 2; number <= 5; number++) {
    await preview.getByRole('button', { name: '格子明细下一页', exact: true }).click();
    await expect(preview.getByText(`本格共 205 条 · 明细第 ${number} 页`)).toBeVisible();
  }
  await expect(preview.getByRole('table', { name: '本格已选提示词' }).locator('tbody tr')).toHaveCount(5);
  await expect(preview.getByRole('cell', { name: '205', exact: true })).toBeVisible();
  await preview.getByRole('button', { name: '释放本次格子明细', exact: true }).click();
  await expect(preview.getByText('格子明细已释放；报告仍可查看，需要明细时请重新预览。')).toBeVisible();
  await page.getByRole('button', { name: '创建本地采样任务', exact: true }).click();
  const task = page.getByLabel('提示词任务详情');
  await expect(task.getByRole('heading', { name: '任务：已完成' })).toBeVisible();
  const dictionary = await api('GET', '/api/prompt-production/dictionaries/' + id);
  await api(
    'PUT',
    '/api/prompt-production/dictionaries/' + id,
    { kind: 'prompts', name: '已清空词典', entries: [] },
    dictionary.version,
  );
  await task.getByRole('button', { name: /目标 205，实际 205/ }).click();
  await expect(task.getByText('本格共 205 条 · 明细第 1 页')).toBeVisible();
  await task.getByRole('button', { name: '格子明细下一页', exact: true }).click();
  await expect(task.getByText('本格共 205 条 · 明细第 2 页')).toBeVisible();
  const download = page.waitForEvent('download');
  await task.getByRole('button', { name: '导出冻结提示词 JSON' }).click();
  expect(JSON.parse(readFileSync((await (await download).path())!, 'utf8')).count).toBe(205);
  expect(errors).toEqual([]);
});

test('覆盖矩阵过期明细明确报错且不自动重新采样', async ({ page }) => {
  const id = crypto.randomUUID();
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: id,
    kind: 'prompts',
    name: '矩阵过期',
    entries: [{ tags: ['no humans', 'forest'], rating: 'general' }],
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('checkbox', { name: '矩阵过期', exact: true }).check();
  await page.getByLabel('目标数量').fill('1');
  await page.getByRole('checkbox', { name: '启用覆盖格子配额' }).uncheck();
  await page.getByLabel('覆盖与约束规则（JSON）').fill(
    JSON.stringify({
      primary_dimensions: ['scene', 'lighting'],
      cell_values: { scene: ['forest'], lighting: ['sunlight'] },
      constraints: [],
    }),
  );
  const response = page.waitForResponse((r) => r.url().endsWith('/api/prompt-production/coverage/preview'));
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  const first = await (await response).json();
  for (let i = 0; i < 3; i++) await api('POST', '/api/prompt-production/coverage/preview', first.request);
  let resamples = 0;
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().endsWith('/coverage/preview')) resamples++;
  });
  const matrix = page.getByRole('region', { name: '覆盖矩阵', exact: true });
  await matrix.getByRole('button', { name: '随机池：目标 1，实际 1，满足格子配额', exact: true }).click();
  await expect(matrix.getByRole('alert')).toHaveText('预览已过期，请重新预览。');
  await expect(matrix.getByText('本格尚未选入条目。')).toHaveCount(0);
  await expect(matrix.getByRole('button', { name: /目标 1，实际 1/ })).toBeVisible();
  expect(resamples).toBe(0);
  expect(errors).toEqual([]);
});

test('覆盖表单编辑权重排除约束保存并载入实际采样', async ({ page }) => {
  const dictionaryId = crypto.randomUUID();
  const entries = ['forest', 'beach', 'city street'].flatMap((scene) =>
    ['sunlight', 'night'].flatMap((light) =>
      Array.from({ length: 10 }, (_, i) => ({
        tags: [scene, light, 'no humans', 'wide shot', 'item ' + i],
        rating: 'general',
      })),
    ),
  );
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: dictionaryId,
    kind: 'prompts',
    name: '表单采样词典',
    entries,
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('link', { name: '管理覆盖方案' }).click();
  await page.getByRole('button', { name: '使用表单编辑' }).click();
  await page.getByLabel('方案名称', { exact: true }).fill('图形化方案闭环');
  await page.getByLabel('方案目标数量').fill('12');
  const weighted = page.getByRole('button', {
    name: '编辑权重 scene: forest · lighting: sunlight',
    exact: true,
  });
  await weighted.focus();
  await weighted.press('Enter');
  await page.getByLabel('当前格子权重').fill('3');
  await page.getByRole('button', { name: '编辑权重 scene: forest · lighting: night', exact: true }).click();
  await page.getByRole('button', { name: '按当前格子标签新增排除条件' }).click();
  await page.getByLabel('最小比例').fill('0.25');
  const response = page.waitForResponse((r) => r.url().endsWith('/coverage-plans/editor/preview'));
  await page.getByRole('button', { name: '预览计划配额' }).click();
  const planned = await (await response).json();
  expect(planned.layout.active_cells).toHaveLength(5);
  expect(planned.layout.constraint_bounds['no humans'][0]).toBe(3);
  await expect(weighted).toContainText('计划配额');
  await expect(
    page.getByRole('button', { name: '编辑权重 scene: forest · lighting: night', exact: true }),
  ).toContainText('已排除');
  const grid = page.getByRole('region', { name: '格子权重编辑', exact: true });
  await grid.screenshot({ path: join(temporary, 'f06-coverage-editor-light.png') });
  await page.evaluate(() => {
    document.documentElement.dataset.theme = 'dark';
  });
  await grid.screenshot({ path: join(temporary, 'f06-coverage-editor-dark.png') });
  await page.evaluate(() => {
    document.documentElement.dataset.theme = 'light';
  });
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect(page.getByRole('heading', { name: '编辑 图形化方案闭环 · r1' })).toBeVisible();
  await expect(page.getByRole('button', { name: '保存覆盖方案' })).toBeDisabled();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 JSON 模板' }).click();
  const exported = JSON.parse(readFileSync((await (await download).path())!, 'utf8'));
  expect(exported.plan.cell_weights['[["scene","forest"],["lighting","sunlight"]]']).toBe(3);
  const saved = (await api('GET', '/api/prompt-production/coverage-plans')).items.find(
    (row: { name: string }) => row.name === '图形化方案闭环',
  );
  await page.getByRole('link', { name: '返回提示词生产' }).click();
  await page.getByRole('checkbox', { name: '表单采样词典', exact: true }).check();
  await page.getByLabel('已保存的覆盖方案').selectOption(saved.coverage_id);
  let releasePlan!: () => void;
  const loadingPlan = new Promise<void>((resolve) => {
    releasePlan = resolve;
  });
  const planRoute = '**/api/prompt-production/coverage-plans/' + saved.coverage_id;
  await page.route(planRoute, async (route) => {
    await loadingPlan;
    await route.continue();
  });
  await page.getByRole('button', { name: '载入并替换覆盖设置' }).click();
  await expect(page.getByRole('button', { name: '预览覆盖与样例', exact: true })).toBeDisabled();
  releasePlan();
  await expect(page.getByLabel('目标数量', { exact: true })).toHaveValue('12');
  await page.unroute(planRoute);
  const actualResponse = page.waitForResponse((r) => r.url().endsWith('/coverage/preview'));
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  const actual = await (await actualResponse).json();
  expect(actual.cell_targets).toEqual(planned.layout.cell_targets);
  expect(actual.cell_counts).toEqual(actual.cell_targets);
  await page.getByRole('button', { name: '创建本地采样任务', exact: true }).click();
  await expect(
    page.getByLabel('提示词任务详情').getByRole('heading', { name: '任务：已完成' }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test('覆盖表单精确种子丢失回包重试与冲突草稿保护', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('link', { name: '管理覆盖方案' }).click();
  await page.getByLabel('模板格式').selectOption('yaml');
  await page
    .getByLabel('覆盖方案模板', { exact: true })
    .fill('name: 精确种子表单\nplan:\n  target_count: 12\n  random_seed: 18446744073709551615\n');
  await page.getByRole('button', { name: '使用表单编辑' }).click();
  await expect(page.getByLabel('方案随机种子')).toHaveValue('18446744073709551615');
  await page.getByLabel('方案随机种子').fill('18446744073709551614');
  const requests: { key: string | undefined; body: string | null }[] = [];
  await page.route('**/api/prompt-production/coverage-plans', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    requests.push({ key: route.request().headers()['idempotency-key'], body: route.request().postData() });
    const response = await route.fetch();
    if (requests.length === 1) await route.abort('failed');
    else await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect.poll(() => requests.length).toBe(1);
  await expect(page.getByRole('button', { name: '保存覆盖方案' })).toBeEnabled();
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect(page.getByRole('heading', { name: '编辑 精确种子表单 · r1' })).toBeVisible();
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
  await page.unroute('**/api/prompt-production/coverage-plans');
  const row = (await api('GET', '/api/prompt-production/coverage-plans')).items.find(
    (v: { name: string }) => v.name === '精确种子表单',
  );
  const path = '/api/prompt-production/coverage-plans/' + row.coverage_id;
  const saved = await api('GET', path);
  expect(saved.random_seed).toBe('18446744073709551614');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 YAML 模板' }).click();
  expect(readFileSync((await (await download).path())!, 'utf8')).toContain('18446744073709551614');
  await api(
    'PUT',
    path,
    { format: 'json', content: saved.template.replace('精确种子表单', '另一编辑版本') },
    saved.version,
  );
  await page.getByLabel('方案目标数量').fill('24');
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect(
    page.getByRole('alert').filter({ hasText: '内容已被另一次操作修改，请核对最新内容。' }),
  ).toBeVisible();
  await expect(page.getByLabel('方案目标数量')).toHaveValue('24');
  await expect(page.getByLabel('方案随机种子')).toHaveValue('18446744073709551614');
  await page.getByRole('button', { name: '新建覆盖方案' }).click();
  await page.getByRole('button', { name: '继续编辑', exact: true }).click();
  await page.getByLabel('方案目标数量').fill('');
  await page.getByRole('button', { name: '切换到模板文本' }).click();
  await expect(page.getByText(/覆盖表单无效/)).toBeVisible();
  await expect(page.getByLabel('方案目标数量')).toHaveValue('');
  await page.getByRole('link', { name: '返回提示词生产' }).click();
  await page.getByRole('button', { name: '保存并继续' }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByRole('heading', { name: '覆盖方案', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '返回提示词生产' }).click();
  await page.getByRole('button', { name: '放弃草稿' }).click();
  await expect(page.getByRole('heading', { name: '提示词生产', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('覆盖表单三维切片和维度删除显式清理权重', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('link', { name: '管理覆盖方案' }).click();
  await page.getByRole('button', { name: '使用表单编辑' }).click();
  await page.getByRole('button', { name: '新增第三维度' }).click();
  await page.getByLabel('主维度 3 名称').fill('view');
  await page.getByLabel('主维度 3 取值（每行一个）').fill('wide shot\nupper body');
  await page.getByLabel('权重筛选 view').selectOption('upper body');
  await page
    .getByRole('button', { name: '编辑权重 scene: forest · lighting: night · view: upper body', exact: true })
    .click();
  await page.getByLabel('当前格子权重').fill('5');
  await page.getByLabel('权重行维度').selectOption('2');
  await page.getByLabel('权重筛选 scene').selectOption('forest');
  await expect(
    page.getByRole('button', {
      name: '编辑权重 scene: forest · lighting: night · view: upper body',
      exact: true,
    }),
  ).toContainText('权重 5');
  await page.getByRole('button', { name: '预览计划配额' }).click();
  await expect(page.getByText(/有效格子 12 个/)).toBeVisible();
  await page.getByRole('button', { name: '删除主维度 3' }).click();
  await page.getByRole('button', { name: '确认删除主维度' }).click();
  await expect(page.getByText(/有 1 项权重对应的格子已不存在/)).toBeVisible();
  await page.getByRole('button', { name: '预览计划配额' }).click();
  await expect(page.getByText(/覆盖表单无效/)).toBeVisible();
  await page.getByRole('button', { name: '清理已失效格子权重' }).click();
  await page.getByRole('button', { name: '预览计划配额' }).click();
  await expect(page.getByText(/有效格子 6 个/)).toBeVisible();
  await page.getByLabel('方案名称', { exact: true }).fill('三维编辑后另存');
  await page.getByRole('button', { name: '保存覆盖方案' }).click();
  await expect(page.getByRole('heading', { name: '编辑 三维编辑后另存 · r1' })).toBeVisible();
  expect(errors).toEqual([]);
});

async function confirmLibraryDelete(page: Page, label: string, retry = false) {
  const dialog = page.getByRole('dialog', { name: '确认删除' + label });
  await dialog.getByLabel('我已保存需要的内容，确认删除' + label).check();
  const button = dialog.getByRole('button', { name: retry ? /重试删除/ : new RegExp('按住删除' + label) });
  await button.focus();
  await page.keyboard.down('Space');
  await page.waitForTimeout(1100);
  await page.keyboard.up('Space');
}

test('库删除覆盖方案保护草稿与版本并支持丢失回包重试', async ({ page }) => {
  const id = crypto.randomUUID(),
    path = '/api/prompt-production/coverage-plans/' + id;
  const template = { name: '库删除方案', plan: { target_count: 12, random_seed: 42 } };
  await api('POST', '/api/prompt-production/coverage-plans', {
    coverage_id: id,
    format: 'json',
    content: JSON.stringify(template),
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByRole('link', { name: '管理覆盖方案' }).click();
  await page.getByRole('button', { name: '编辑 库删除方案', exact: true }).click();
  const editor = page.getByLabel('覆盖方案模板', { exact: true });
  const original = await editor.inputValue();
  await editor.fill(original + '\n');
  await expect(page.getByRole('button', { name: '删除覆盖方案', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '重新载入已保存版本' }).click();
  await page.getByRole('button', { name: '放弃草稿' }).click();
  await expect(page.getByRole('button', { name: '删除覆盖方案', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '删除覆盖方案', exact: true }).click();
  const before = await api('GET', path);
  await api(
    'PUT',
    path,
    {
      format: 'json',
      content: JSON.stringify({ ...template, plan: { ...template.plan, target_count: 24 } }),
    },
    before.version,
  );
  await confirmLibraryDelete(page, '覆盖方案');
  await expect(page.getByRole('dialog').getByText('内容已被另一次操作修改，请核对最新内容。')).toBeVisible();
  expect((await api('GET', path)).revision).toBe(2);
  await page.getByRole('button', { name: '取消删除' }).click();
  await page.getByRole('button', { name: '重新载入已保存版本' }).click();
  await expect(page.getByRole('heading', { name: '编辑 库删除方案 · r2' })).toBeVisible();
  const attempts: { key: string | undefined; version: string | undefined }[] = [];
  await page.route('**' + path, async (route) => {
    if (route.request().method() !== 'DELETE') return route.continue();
    attempts.push({
      key: route.request().headers()['idempotency-key'],
      version: route.request().headers()['if-match'],
    });
    const response = await route.fetch();
    if (attempts.length === 1) await route.abort('failed');
    else await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '删除覆盖方案', exact: true }).click();
  await page.getByRole('dialog').screenshot({ path: join(temporary, 'f06-library-delete-confirm.png') });
  await confirmLibraryDelete(page, '覆盖方案');
  await expect(page.getByRole('dialog').getByRole('alert')).toBeVisible();
  await confirmLibraryDelete(page, '覆盖方案', true);
  await expect(page.getByRole('heading', { name: '新方案', exact: true })).toBeVisible();
  expect(attempts).toHaveLength(2);
  expect(attempts[1]).toEqual(attempts[0]);
  await page.unroute('**' + path);
  await expect(page.getByRole('button', { name: '编辑 库删除方案', exact: true })).not.toBeVisible();
  const removed = await fetch('http://' + credentials.authority + path, {
    headers: { Authorization: 'Bearer ' + credentials.token },
  });
  expect(removed.status).toBe(410);
  const replay = await fetch('http://' + credentials.authority + '/api/prompt-production/coverage-plans', {
    method: 'POST',
    headers: {
      Authorization: 'Bearer ' + credentials.token,
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify({ coverage_id: id, format: 'json', content: JSON.stringify(template) }),
  });
  expect(replay.status).toBe(410);
  expect(errors).toEqual([]);
});

test('库删除词典移除采样选择但保留已冻结任务和导出', async ({ page }) => {
  const id = crypto.randomUUID();
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: id,
    kind: 'prompts',
    name: '库删除词典',
    entries: [0, 1].map((i) => ({
      tags: ['forest', 'sunlight', 'no humans', 'wide shot', 'item ' + i],
      rating: 'general',
    })),
  });
  const preview = await api('POST', '/api/prompt-production/coverage/preview', {
    prompt_dictionary_ids: [id],
    coverage_enabled: false,
    plan: { target_count: 2, random_seed: 42 },
  });
  const taskId = crypto.randomUUID();
  await api('POST', '/api/prompt-production/tasks', {
    task_id: taskId,
    request: preview.request,
    preview_digest: preview.preview_digest,
  });
  await expect
    .poll(async () => (await api('GET', '/api/prompt-production/tasks/' + taskId)).can_cleanup)
    .toBe(true);
  const before = await api('GET', '/api/prompt-production/tasks/' + taskId);
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('打开已有词典').selectOption(id);
  await expect(page.getByLabel('词典名称', { exact: true })).toHaveValue('库删除词典');
  await page.getByRole('checkbox', { name: '库删除词典', exact: true }).check();
  await page.getByLabel('目标数量', { exact: true }).fill('2');
  await page.getByRole('checkbox', { name: '启用覆盖格子配额' }).uncheck();
  await page.getByRole('button', { name: '预览覆盖与样例', exact: true }).click();
  await expect(page.getByRole('button', { name: '创建本地采样任务', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '删除整份词典', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: /按住删除整份词典/ })).toBeDisabled();
  await confirmLibraryDelete(page, '整份词典');
  await expect(page.getByLabel('词典名称', { exact: true })).toHaveValue('');
  await expect(page.getByRole('checkbox', { name: '库删除词典', exact: true })).not.toBeVisible();
  await expect(page.getByRole('button', { name: '创建本地采样任务', exact: true })).not.toBeVisible();
  expect(await api('GET', '/api/prompt-production/tasks/' + taskId)).toEqual(before);
  await page.getByLabel('查看任务', { exact: true }).selectOption(taskId);
  const detail = page.getByLabel('提示词任务详情');
  await expect(detail.getByRole('heading', { name: '任务：已完成' })).toBeVisible();
  const download = page.waitForEvent('download');
  await detail.getByRole('button', { name: '导出冻结提示词 JSON' }).click();
  expect(JSON.parse(readFileSync((await (await download).path())!, 'utf8')).count).toBe(2);
  expect(errors).toEqual([]);
});

test('库删除中断后重新打开页面可继续清理剩余文件', async ({ page }) => {
  for (const kind of ['dictionary', 'coverage']) {
    const id = crypto.randomUUID(),
      label = kind === 'dictionary' ? '整份词典' : '覆盖方案';
    const prefix = '/api/prompt-production/' + (kind === 'dictionary' ? 'dictionaries' : 'coverage-plans');
    const name = '中断清理' + label;
    await api(
      'POST',
      prefix,
      kind === 'dictionary'
        ? { dictionary_id: id, kind: 'prompts', name, entries: [] }
        : { coverage_id: id, format: 'json', content: JSON.stringify({ name, plan: {} }) },
    );
    await page.goto('about:blank');
    await connect(page);
    await page.getByRole('link', { name: '提示词生产', exact: true }).click();
    if (kind === 'coverage') {
      await page.getByRole('link', { name: '管理覆盖方案' }).click();
      await page.getByRole('button', { name: '编辑 ' + name, exact: true }).click();
    } else await page.getByLabel('打开已有词典').selectOption(id);
    writeFileSync(join(base, 'fail-library-remove-once'), id, 'utf8');
    await page.getByRole('button', { name: '删除' + label, exact: true }).click();
    await confirmLibraryDelete(page, label);
    await expect(page.getByRole('dialog').getByText(/删除记录已保存，但文件清理尚未完成/)).toBeVisible();
    expect(
      (await api('GET', prefix + '/deletions')).items.some(
        (row: { resource_id: string }) => row.resource_id === id,
      ),
    ).toBe(true);
    await page.getByRole('button', { name: '取消删除' }).click();
    await page.goto('about:blank');
    await connect(page);
    await page.getByRole('link', { name: '提示词生产', exact: true }).click();
    if (kind === 'coverage') await page.getByRole('link', { name: '管理覆盖方案' }).click();
    await page.getByRole('button', { name: '检查未完成删除', exact: true }).click();
    const pending = page.getByRole('region', { name: '未完成的库文件删除' });
    await pending.getByRole('button', { name: '继续清理 ' + name, exact: true }).click();
    await confirmLibraryDelete(page, label);
    await expect(pending.getByText(/待完成文件清理：0 项/)).toBeVisible();
    expect((await api('GET', prefix + '/deletions')).total).toBe(0);
  }
  expect(errors).toEqual([]);
});

test('可选编码回退、严格覆盖、坏 BOM 阻止导入与重开后的编码明细', async ({ page }) => {
  const prepare = (mode: string) =>
    execFileSync(python, ['-m', 'tests.web_fixture', 'encoding-input', base, mode], {
      cwd: repo,
      env: environment,
      windowsHide: true,
    });
  prepare('gbk');
  await connect(page);
  await page.getByRole('link', { name: '导入导出方案', exact: true }).click();
  await page.getByRole('button', { name: '新建方案', exact: true }).click();
  await page.getByLabel('方案名称').fill('混合编码验收');
  await page.getByLabel('标注解析').selectOption('literal-tags-v1');
  await expect(page.getByLabel('文本编码')).toHaveValue('utf-8-sig');
  await page.getByLabel('文本编码').selectOption('auto');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '编辑 混合编码验收', exact: true })).toBeVisible();
  const project = await api('GET', '/api/projects/current');
  const p = `/api/projects/${project.project_id}`;
  const scheme = (await api('GET', p + '/import-schemes')).items.find(
    (row: { name: string }) => row.name === '混合编码验收',
  );
  expect(scheme.executable).toBe(true);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('导入方案').selectOption(String(scheme.scheme_id));
  await expect(page.getByLabel('TXT 编码')).toHaveValue('auto');
  await page.getByLabel('来源名称').fill('混合编码浏览器导入');
  await page.getByRole('button', { name: '选择图片目录', exact: true }).click();
  await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
  const row = page.getByRole('row').filter({ hasText: 'encoding-example.png' });
  await expect(row).toContainText('一名女孩站在树旁。');
  await expect(row).toContainText('UTF-8 解码失败，已使用 GBK，请核对中文');
  await expect(page.getByText('共 1 项使用 GBK 回退（含未显示项），请核对中文后再导入。')).toBeVisible();
  await page.getByLabel('TXT 编码').selectOption('utf-8-sig');
  await expect(page.getByRole('button', { name: '确认导入', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
  await expect(row).toContainText('encoding:invalid');
  await expect(page.getByRole('button', { name: '确认导入', exact: true })).toBeDisabled();
  await page.getByLabel('TXT 编码').selectOption('auto');
  prepare('broken-bom');
  await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
  await expect(row).toContainText('encoding:invalid');
  await expect(page.getByRole('button', { name: '确认导入', exact: true })).toBeDisabled();
  prepare('gbk');
  await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
  await expect(row).toContainText('一名女孩站在树旁。');
  await page.screenshot({ path: join(temporary, 'f10-import-encoding-preview.png'), fullPage: true });
  await page.getByRole('button', { name: '确认导入', exact: true }).click();
  await expect(page.getByText(/导入结束：成功 1，/)).toBeVisible();
  const source = (await api('GET', p + '/sources')).items.find(
    (item: { name: string }) => item.name === '混合编码浏览器导入',
  );
  const summary = await api('GET', `${p}/imports/${source.source_id}/summary`);
  expect(summary.items.find((item: { path: string }) => item.path === 'encoding-example.png')).toMatchObject({
    encoding: 'gbk',
    encoding_fallback: true,
    warnings: ['encoding:gbk_fallback'],
  });
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page
    .getByRole('article')
    .filter({ hasText: '混合编码浏览器导入' })
    .getByRole('button', { name: '查看明细' })
    .click();
  await expect(page.getByRole('dialog')).toContainText('UTF-8 解码失败，已使用 GBK，请核对中文');
  await expect(page.getByRole('dialog')).toContainText('gbk');
  await page.getByRole('dialog').screenshot({ path: join(temporary, 'f10-import-encoding-history.png') });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  expect(errors).toEqual([]);
});

test('硬链接方案风险确认、重新预览撤销确认和共享文件报告', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '导入导出方案', exact: true }).click();
  await page.getByRole('button', { name: '导出方案', exact: true }).click();
  await page.getByRole('button', { name: '新建方案', exact: true }).click();
  await page.getByLabel('方案名称').fill('硬链接验收');
  await expect(page.getByLabel('图片导出方式')).toHaveValue('copy');
  await page.getByLabel('图片导出方式').selectOption('hardlink');
  await expect(page.getByText(/硬链接与项目原图共享文件内容/)).toBeVisible();
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '编辑 硬链接验收', exact: true })).toBeVisible();
  const project = await api('GET', '/api/projects/current');
  const p = `/api/projects/${project.project_id}`;
  const scheme = (await api('GET', p + '/export-schemes')).items.find(
    (row: { name: string }) => row.name === '硬链接验收',
  );
  expect(scheme.executable).toBe(true);
  await page.getByRole('link', { name: '导出数据集', exact: true }).click();
  await page.getByLabel('导出方案', { exact: true }).selectOption(String(scheme.scheme_id));
  await page.getByLabel('包含未审核图片（默认只导出保留项）').check();
  const generate = page.getByRole('button', { name: '生成导出预览', exact: true });
  const confirm = page.getByRole('button', { name: /确认导出 \d+ 张/ });
  const acknowledge = page.getByLabel('我已了解修改导出图片也会影响项目原图，确认本次使用硬链接');
  const triggerAck = page.getByLabel('已了解触发词与现有标签重合，继续导出');
  await generate.click();
  await expect(acknowledge).toBeVisible();
  if (await triggerAck.isVisible()) await triggerAck.check();
  await expect(confirm).toBeDisabled();
  await acknowledge.check();
  await expect(confirm).toBeEnabled();
  await generate.click();
  await expect(acknowledge).not.toBeChecked();
  if (await triggerAck.isVisible()) await triggerAck.check();
  await expect(confirm).toBeDisabled();
  await acknowledge.check();
  await page.screenshot({ path: join(temporary, 'f10-hardlink-confirmation.png'), fullPage: true });
  const response = page.waitForResponse(
    (res) => res.url().endsWith(p + '/exports') && res.request().method() === 'POST',
  );
  await confirm.click();
  const accepted = await (await response).json();
  const card = page.getByRole('article').filter({ hasText: accepted.export_id });
  await expect(card.getByRole('button', { name: '查看报告' })).toBeVisible();
  const report = await api('GET', `${p}/exports/${accepted.export_id}/report`);
  expect(report.output_config.image_mode).toBe('hardlink');
  expect(report.hardlink_ack_digest).toMatch(/^[0-9a-f]{64}$/);
  expect(report.counts.exported).toBeGreaterThan(0);
  expect(report.counts.failed).toBe(0);
  for (const item of report.images.filter((item: { status: string }) => item.status === 'exported')) {
    const source = join(base, 'browser.slproj', 'images', item.filename);
    const target = join(base, 'browser.slproj', report.directory, item.output_path);
    const a = statSync(source, { bigint: true }),
      b = statSync(target, { bigint: true });
    expect([a.dev, a.ino]).toEqual([b.dev, b.ino]);
    expect(a.nlink).toBeGreaterThan(1n);
    expect(readFileSync(target)).toEqual(readFileSync(source));
  }
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '导出数据集', exact: true }).click();
  await expect(card).toContainText('硬链接导出，共享项目原图内容。');
  await card.getByRole('button', { name: '查看报告' }).click();
  await expect(page.getByRole('dialog')).toContainText('图片导出方式：硬链接');
  await expect(page.getByRole('dialog')).toContainText('文件哈希是导出时的记录');
  await page.getByRole('dialog').screenshot({ path: join(temporary, 'f10-hardlink-report.png') });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  expect(errors).toEqual([]);
});

test('本地金字塔排序保留标签集合、提示未分类并导出冻结报告', async ({ page }) => {
  await connect(page);
  const project = await api('GET', '/api/projects/current');
  const p = `/api/projects/${project.project_id}`;
  const current = await api('GET', p + '/annotations/1');
  const tags = ['moonlight', 'outdoors', 'shirt', 'wide shot', 'smile', '1girl', 'unknown z', 'unknown a'];
  const edited = await api(
    'PUT',
    p + '/annotations/1/manual',
    {
      tags: tags.map((tag) => ({ tag })),
      read_set: current.refs,
    },
    current.version,
  );
  await page.getByRole('link', { name: '导入导出方案', exact: true }).click();
  await page.getByRole('button', { name: '导出方案', exact: true }).click();
  await page.getByRole('button', { name: '新建方案', exact: true }).click();
  await page.getByLabel('方案名称').fill('本地排序验收');
  await expect(page.getByLabel('标签排序')).toHaveValue('layer_first');
  await page.getByLabel('标签排序').selectOption('prompt_pyramid');
  await expect(page.getByText(/不推断多人物归属/)).toBeVisible();
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '编辑 本地排序验收', exact: true })).toBeVisible();
  const scheme = (await api('GET', p + '/export-schemes')).items.find(
    (row: { name: string }) => row.name === '本地排序验收',
  );
  expect(scheme.executable).toBe(true);
  await page.getByRole('link', { name: '导出数据集', exact: true }).click();
  await page.getByLabel('导出方案', { exact: true }).selectOption(String(scheme.scheme_id));
  await page.getByLabel('包含未审核图片（默认只导出保留项）').check();
  await page.getByRole('button', { name: '生成导出预览', exact: true }).click();
  const row = page.getByRole('row').filter({ has: page.getByRole('cell', { name: '1.png', exact: true }) });
  const expected =
    'geometric style, 1girl, smile, shirt, wide shot, outdoors, moonlight, unknown z, unknown a';
  await expect(row).toContainText(expected);
  await expect(row).toContainText('未分类 2 项，按原相对顺序放在末尾。');
  await expect(page.getByText(/标签排序：本地金字塔排序/)).toBeVisible();
  const triggerAck = page.getByLabel('已了解触发词与现有标签重合，继续导出');
  if (await triggerAck.isVisible()) await triggerAck.check();
  await page.screenshot({ path: join(temporary, 'r4-pyramid-preview.png'), fullPage: true });
  const response = page.waitForResponse(
    (res) => res.url().endsWith(p + '/exports') && res.request().method() === 'POST',
  );
  await page.getByRole('button', { name: /确认导出 \d+ 张/ }).click();
  const accepted = await (await response).json();
  const card = page.getByRole('article').filter({ hasText: accepted.export_id });
  await expect(card.getByRole('button', { name: '查看报告' })).toBeVisible();
  const report = await api('GET', `${p}/exports/${accepted.export_id}/report`);
  expect(report.sorter.name).toBe('prompt_pyramid');
  expect(report.sorter.rules_sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(report.sorter.skill_hash).toMatch(/^[0-9a-f]{64}$/);
  expect(report.sort_summary.unclassified_tag_occurrences).toBeGreaterThanOrEqual(2);
  expect(readFileSync(join(base, 'browser.slproj', report.directory, '1.txt'), 'utf8')).toBe(expected + '\n');
  const after = await api('GET', p + '/annotations/1');
  expect(after.version).toBe(edited.version);
  expect(after.refs).toEqual(edited.annotation.refs);
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '导出数据集', exact: true }).click();
  await card.getByRole('button', { name: '查看报告' }).click();
  await expect(page.getByRole('dialog')).toContainText('标签排序：本地金字塔排序');
  await expect(page.getByRole('dialog')).toContainText('已保留并稳定放在末尾');
  await page.getByRole('dialog').screenshot({ path: join(temporary, 'r4-pyramid-report.png') });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  expect(errors).toEqual([]);
});

test('词典维度 TXT 下载及跨页选择有效导入记录后重新预览追加', async ({ page }) => {
  const id = crypto.randomUUID(),
    path = '/api/prompt-production/dictionaries/' + id;
  await api('POST', '/api/prompt-production/dictionaries', {
    dictionary_id: id,
    kind: 'features',
    name: '词典交换验收',
    dimensions: {
      scene: [{ tag: 'forest', rating: 'general' }],
      lighting: [{ tag: 'sunlight', rating: null }],
    },
  });
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('打开已有词典').selectOption(id);
  await page.getByRole('button', { name: '导出词典 TXT', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('保留标签与评级');
  const zipButton = page.getByRole('button', { name: '下载维度 TXT 压缩包', exact: true });
  await expect(zipButton).toBeDisabled();
  await page.getByLabel('确认接受词典 TXT 导出的信息损失').check();
  await page.getByRole('dialog').screenshot({ path: join(temporary, 'f06-dictionary-txt-export.png') });
  const download = page.waitForEvent('download');
  await zipButton.click();
  const archive = await download;
  expect(archive.suggestedFilename()).toBe('dictionary-' + id + '.zip');
  const archivePath = join(temporary, 'f06-feature-dictionary.zip');
  await archive.saveAs(archivePath);
  expect(readFileSync(archivePath).subarray(0, 4).toString('hex')).toBe('504b0304');
  await page.getByText('从文件追加内容', { exact: true }).click();
  await page.getByLabel('导入格式', { exact: true }).selectOption('csv');
  const csv =
    'dimension,tag,rating\n' +
    Array.from({ length: 101 }, (_, i) => `imported,feature ${i},general`).join('\n') +
    '\nimported,,general\n';
  await page
    .getByLabel('选择本地词典文件')
    .setInputFiles({ name: 'mixed.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });
  await page.getByRole('button', { name: '预览导入', exact: true }).click();
  await expect(page.getByText(/存在错误，未写入词典/)).toBeVisible();
  await expect(page.getByRole('button', { name: '确认追加到词典', exact: true })).toBeDisabled();
  await page.getByText('选择有效行并重新预览', { exact: true }).click();
  await page.getByLabel('选择导入记录 2', { exact: true }).check();
  await page.getByRole('button', { name: '下一页导入记录', exact: true }).click();
  await expect(page.getByLabel('选择导入记录 52', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '下一页导入记录', exact: true }).click();
  await page.getByLabel('选择导入记录 102', { exact: true }).check();
  await expect(page.getByLabel('选择导入记录 103', { exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '生成所选行新预览', exact: true }).click();
  await expect(page.getByText(/保留 2 条，排除 100 条/)).toBeVisible();
  await page.screenshot({ path: join(temporary, 'f06-dictionary-selected-preview.png'), fullPage: true });
  await page.getByRole('button', { name: '确认追加到词典', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认追加到词典', exact: true })).toHaveCount(0);
  const saved = await api('GET', path);
  expect(saved.dimensions.imported.map((row: { tag: string }) => row.tag)).toEqual([
    'feature 0',
    'feature 100',
  ]);
  expect(saved.dimensions.scene).toEqual([{ tag: 'forest', rating: 'general' }]);
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('打开已有词典').selectOption(id);
  const csvDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出词典 CSV', exact: true }).click();
  const csvFile = await csvDownload;
  await csvFile.saveAs(join(temporary, 'f06-feature-dictionary.csv'));
  expect(readFileSync(join(temporary, 'f06-feature-dictionary.csv'), 'utf8')).toContain(
    'imported,feature 100,general',
  );
  expect(errors).toEqual([]);
});

test('提示词 TXT 有损确认绑定词典版本，冲突后重新载入可下载', async ({ page }) => {
  const id = crypto.randomUUID(),
    path = '/api/prompt-production/dictionaries/' + id;
  const body = {
    kind: 'prompts',
    name: '提示词 TXT 验收',
    entries: [
      { tags: ['forest'], natural_language: 'A river.\nA tree.', aspect: 'landscape', rating: 'general' },
    ],
  };
  await api('POST', '/api/prompt-production/dictionaries', { dictionary_id: id, ...body });
  const initial = await api('GET', path);
  await connect(page);
  await page.getByRole('link', { name: '提示词生产', exact: true }).click();
  await page.getByLabel('打开已有词典').selectOption(id);
  await page.getByRole('button', { name: '导出词典 TXT', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('图幅、评级');
  await expect(page.getByRole('button', { name: '下载提示词 TXT', exact: true })).toBeDisabled();
  await page.getByLabel('确认接受词典 TXT 导出的信息损失').check();
  await api('PUT', path, { ...body, entries: [{ ...body.entries[0], tags: ['beach'] }] }, initial.version);
  await page.getByRole('button', { name: '下载提示词 TXT', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toBeVisible();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '重新载入已保存词典', exact: true }).click();
  await expect(page.getByLabel('有序标签（每行一个）')).toHaveValue('beach');
  await page.getByRole('button', { name: '导出词典 TXT', exact: true }).click();
  await expect(page.getByLabel('确认接受词典 TXT 导出的信息损失')).not.toBeChecked();
  await page.getByLabel('确认接受词典 TXT 导出的信息损失').check();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载提示词 TXT', exact: true }).click();
  const file = await download;
  await file.saveAs(join(temporary, 'f06-prompt-dictionary.txt'));
  expect(readFileSync(join(temporary, 'f06-prompt-dictionary.txt'), 'utf8')).toBe(
    'beach, A river. A tree.\n',
  );
  expect((await api('GET', path)).revision).toBe(2);
  expect(errors).toEqual([]);
});

test('自定义尺寸预设复制到生成源，编辑删除和重开不改已有像素', async ({ page }) => {
  await connect(page);
  const builtin = (await api('GET', '/api/presets')).items.find(
    (row: { builtin_key: string }) => row.builtin_key === 'large',
  );
  await page.getByRole('link', { name: '尺寸预设', exact: true }).click();
  await page.getByLabel('打开尺寸预设').selectOption(builtin.preset_id);
  await expect(page.getByLabel('横向宽度')).toBeDisabled();
  await expect(page.getByRole('button', { name: '删除尺寸预设', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '复制为自定义预设', exact: true }).click();
  await page.getByLabel('预设名称', { exact: true }).fill('自定义尺寸验收');
  await page.getByLabel('横向宽度').fill('1600');
  await page.getByRole('button', { name: '保存尺寸预设', exact: true }).click();
  await expect(page.getByText(/尺寸预设已保存，版本 1/)).toBeVisible();
  const id = await page.getByLabel('打开尺寸预设').inputValue();
  const saved = await api('GET', '/api/presets/' + id);
  await page.screenshot({ path: join(temporary, 'f24-custom-presets.png'), fullPage: true });
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('生成源名称').fill('尺寸快照验收');
  await page.getByText('高级 JSON 配置', { exact: true }).click();
  const config = JSON.parse(await page.getByLabel('生成配置（JSON）').inputValue());
  config.backend = 'nai_launcher'; // Existing unbounded preset fixture remains a legacy draft.
  config.profile = {
    model: 'nai-diffusion-5-full',
    effort: 'high',
    sampler: 'fixture-sampler',
    steps: 17,
    scale: 4.5,
    cfg_rescale: 0,
    noise_schedule: 'fixture-schedule',
    quality_toggle: false,
    uc_preset: null,
    smea: false,
    smea_dyn: false,
    variety_plus: false,
    decrisp: false,
  };
  await page.getByLabel('生成配置（JSON）').fill(JSON.stringify(config));
  await page.getByLabel('填入尺寸预设').selectOption(id);
  await expect
    .poll(async () => JSON.parse(await page.getByLabel('生成配置（JSON）').inputValue()).sizes)
    .toEqual(saved.sizes);
  await page.getByRole('button', { name: '保存生成源草稿', exact: true }).click();
  await expect(page.getByText('生成源草稿已保存；尚未锁定或提交生成。')).toBeVisible();
  const sourceId = await page.getByLabel('打开生成源').inputValue();
  const project = await api('GET', '/api/projects/current'),
    sourcePath = `/api/projects/${project.project_id}/sources/${sourceId}`;
  const before = await api('GET', sourcePath);
  expect(before.config_json.sizes).toEqual(saved.sizes);
  expect(JSON.stringify(before.config_json)).not.toContain(id);
  await page.getByRole('link', { name: '尺寸预设', exact: true }).click();
  await page.getByLabel('打开尺寸预设').selectOption(id);
  await page.getByLabel('横向宽度').fill('1664');
  await page.getByRole('button', { name: '保存尺寸预设', exact: true }).click();
  await expect(page.getByText(/尺寸预设已保存，版本 2/)).toBeVisible();
  await page.getByRole('button', { name: '删除尺寸预设', exact: true }).click();
  await page.getByLabel('确认删除这份自定义尺寸预设').check();
  await page.getByRole('dialog').screenshot({ path: join(temporary, 'f24-delete-preset.png') });
  const remove = page.getByRole('button', { name: /按住删除预设/ });
  await remove.focus();
  await page.keyboard.down('Enter');
  await page.waitForTimeout(1100);
  await page.keyboard.up('Enter');
  await expect(page.getByText(/尺寸预设已删除/)).toBeVisible();
  expect((await api('GET', sourcePath)).config_json).toEqual(before.config_json);
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(sourceId);
  await expect
    .poll(async () => JSON.parse(await page.getByLabel('生成配置（JSON）').inputValue()).sizes)
    .toEqual(saved.sizes);
  expect(
    (await api('GET', '/api/presets')).items.some((row: { preset_id: string }) => row.preset_id === id),
  ).toBe(false);
  expect((await api('GET', '/api/generation/capabilities')).generation).toBe(false);
  expect(errors).toEqual([]);
});

test('尺寸预设保存丢失回包可核对且版本冲突保留草稿', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '尺寸预设', exact: true }).click();
  await page.getByLabel('预设名称', { exact: true }).fill('预设丢失回包');
  const keys: string[] = [];
  await page.route('**/api/presets', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) {
      await route.fetch();
      await route.abort('failed');
    } else await route.continue();
  });
  await page.getByRole('button', { name: '保存尺寸预设', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByLabel('预设名称', { exact: true })).toHaveValue('预设丢失回包');
  await page.getByRole('button', { name: '保存尺寸预设', exact: true }).click();
  await expect(page.getByText(/尺寸预设已保存，版本 1/)).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  const id = await page.getByLabel('打开尺寸预设').inputValue();
  expect(
    (await api('GET', '/api/presets')).items.filter((row: { name: string }) => row.name === '预设丢失回包'),
  ).toHaveLength(1);
  const before = await api('GET', '/api/presets/' + id);
  await page.getByLabel('预设名称', { exact: true }).fill('本地冲突草稿');
  await api('PUT', '/api/presets/' + id, { name: '外部更新预设', sizes: before.sizes }, before.version);
  await page.getByRole('button', { name: '保存尺寸预设', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByLabel('预设名称', { exact: true })).toHaveValue('本地冲突草稿');
  await page.getByRole('button', { name: '重新载入尺寸预设', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('有未保存的尺寸预设');
  await page.getByRole('button', { name: '放弃草稿', exact: true }).click();
  await expect(page.getByLabel('预设名称', { exact: true })).toHaveValue('外部更新预设');
  expect(errors).toEqual([]);
});

test('设置恢复记录在重开后保留阻塞，损坏时不自动解除或修改后端', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: 'Launcher / MCP', exact: true }).click();
  const panel = page.getByRole('region', { name: '生成设置恢复状态' });
  await expect(panel.getByText('没有待处理的设置恢复记录')).toBeVisible();
  const fixture = (action: string) =>
    execFileSync(python, ['-m', 'tests.web_fixture', 'settings-lease', base, action], {
      cwd: repo,
      env: environment,
      windowsHide: true,
      timeout: 10_000,
    });
  fixture('conflict');
  const path = join(base, 'runtime', 'product', 'private', 'recovery', 'launcher', 'settings-lease.json');
  const conflict = readFileSync(path);
  try {
    await expect(panel.getByText('设置尚未释放')).toBeVisible();
    await expect(panel.getByText(/保留外部修改/)).toBeVisible();
    await expect(panel.getByRole('button')).toHaveCount(0);
    await page.screenshot({ path: join(temporary, 'f03-settings-conflict.png'), fullPage: true });
    expect((await api('GET', '/api/backends/nai_launcher/settings-lease')).phase).toBe('conflict');
    await page.goto('about:blank');
    await connect(page);
    await page.getByRole('link', { name: 'Launcher / MCP', exact: true }).click();
    await expect(panel.getByText(/保留外部修改/)).toBeVisible();
    expect(readFileSync(path)).toEqual(conflict);
    writeFileSync(path, '{');
    await expect(panel.getByText(/恢复记录格式或校验失败/)).toBeVisible();
    await expect(panel.getByText('没有待处理的设置恢复记录')).toHaveCount(0);
    expect(readFileSync(path, 'utf8')).toBe('{');
  } finally {
    writeFileSync(path, conflict);
    fixture('restore');
  }
  await expect(panel.getByText('没有待处理的设置恢复记录')).toBeVisible();
  expect((await api('GET', '/api/generation/capabilities')).generation).toBe(false);
  expect(errors).toEqual([]);
});

test('生成费用策略明确确认后持久保存，本地估算不冒称扣费或启用生成', async ({ page }) => {
  const project = await api('GET', '/api/projects/current');
  const path = `/api/projects/${project.project_id}/sources`;
  const listing = await api('GET', path);
  const original = listing.items.find((row: { type: string }) => row.type === 'nai');
  const source = await api('POST', path, {
    name: '费用策略浏览器验收',
    target_count: original.target_count,
    cost_policy: 'ask',
    style_prompt: original.style_prompt,
    negative_prompt: original.negative_prompt,
    config: original.config_json,
  });
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(String(source.source_id));
  const panel = page.getByRole('region', { name: '生成费用与估算' });
  await expect(panel.getByText('累计本地估算：0 Anlas')).toBeVisible();
  await expect(panel.getByText(/余额、订阅和体力未知/)).toBeVisible();
  await panel.getByLabel('运行费用策略').selectOption('allow');
  await panel.getByRole('button', { name: '保存费用策略' }).click();
  const dialog = page.getByRole('dialog', { name: '允许非零费用', exact: true });
  const hold = dialog.getByRole('button', { name: /按住确认费用操作/ });
  await expect(hold).toBeDisabled();
  expect((await api('GET', path + '/' + source.source_id)).cost_policy).toBe('ask');
  await dialog.getByLabel('我理解费用范围并同意').check();
  await hold.focus();
  await page.keyboard.down('Space');
  await page.waitForTimeout(1100);
  await page.keyboard.up('Space');
  await expect(panel.getByText('费用策略已保存，原有周期同意已失效。')).toBeVisible();
  expect((await api('GET', path + '/' + source.source_id)).cost_policy).toBe('allow');
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(String(source.source_id));
  await expect(panel.getByLabel('运行费用策略')).toHaveValue('allow');
  await panel.getByLabel('运行费用策略').selectOption('deny');
  await panel.getByRole('button', { name: '保存费用策略' }).click();
  await expect(panel.getByText('禁止策略不提供费用同意操作。')).toBeVisible();
  await expect(panel.getByRole('button', { name: '确认本周期费用并继续' })).toHaveCount(0);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f04-generation-costs.png') });
  const estimate = await api(
    'GET',
    `/api/backends/nai_launcher/account-status?project_id=${project.project_id}&source_id=${source.source_id}`,
  );
  expect(estimate.actual_charged_anlas).toBeNull();
  expect(estimate.zero_estimate_is_free).toBe(false);
  expect((await api('GET', '/api/generation/capabilities')).generation).toBe(false);
  expect(errors).toEqual([]);
});

test('生成恢复只读对账后单独长按恢复设置且刷新不重发', async ({ page }) => {
  let project = await api('GET', '/api/projects/current');
  if (!project) {
    await connect(page);
    await page.getByRole('button', { name: '选择保存位置并创建' }).click();
    await expect.poll(async () => api('GET', '/api/projects/current')).not.toBeNull();
    project = await api('GET', '/api/projects/current');
    await page.goto('about:blank');
  }
  const draft = JSON.parse(readFileSync(join(base, 'recovery-draft.json'), 'utf8'));
  writeFileSync(join(base, 'enable-recovery-fixture'), 'synthetic-only');
  const source = await api('POST', `/api/projects/${project.project_id}/sources`, {
    ...draft,
    name: 'Recovery browser fixture',
  });
  const path = `/api/projects/${project.project_id}/sources/${source.source_id}`;
  await expect.poll(async () => (await api('GET', path)).status).toBe('paused');
  const recovery = await api('GET', path + '/recovery');
  const id = recovery.items[0].item_id;
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(String(source.source_id));
  const panel = page.getByRole('region', { name: '生成恢复与对账' });
  await expect(panel.getByText('等待只读对账')).toBeVisible();
  await panel.getByRole('button', { name: `核查条目 #${id}`, exact: true }).click();
  await expect(panel.getByText('等待恢复设置')).toBeVisible();
  expect((await api('GET', '/api/backends/nai_launcher/settings-lease')).phase).toBe('needs_restore');
  const afterProbe = await api('GET', path + '/items');
  expect(afterProbe.items[0].status).toBe('done');
  expect(afterProbe.items[0].attempts).toBe(1);
  await panel.getByRole('button', { name: `恢复设置 #${id}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '确认恢复生成设置' });
  await dialog.getByLabel('我已核对条目并同意恢复设置').check();
  const hold = dialog.getByRole('button', { name: /按住恢复设置/ });
  await expect(hold).toBeEnabled();
  await hold.focus();
  await page.keyboard.down('Space');
  await page.waitForTimeout(1100);
  await page.keyboard.up('Space');
  await expect(panel.getByText('操作已结清')).toBeVisible();
  expect((await api('GET', '/api/backends/nai_launcher/settings-lease')).blocked).toBe(false);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f02-generation-recovery.png') });
  await page.reload();
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(String(source.source_id));
  await expect(panel.getByText('操作已结清')).toBeVisible();
  const afterReload = await api('GET', path + '/items');
  expect(afterReload.items.map((item: { status: string }) => item.status)).toEqual(['done', 'pending']);
  expect((await api('GET', path)).status).toBe('paused');
  expect(errors).toEqual([]);
});

test('直连未知请求人工结束来源并持久保留未知记录且不重发', async ({ page }) => {
  let project = await api('GET', '/api/projects/current');
  if (!project) {
    await connect(page);
    await page.getByRole('button', { name: '选择保存位置并创建' }).click();
    await expect.poll(async () => api('GET', '/api/projects/current')).not.toBeNull();
    project = await api('GET', '/api/projects/current');
    await page.goto('about:blank');
  }
  writeFileSync(join(base, 'enable-direct-review-fixture'), 'synthetic-only');
  const draft = JSON.parse(readFileSync(join(base, 'direct-review-draft.json'), 'utf8'));
  const source = await api('POST', `/api/projects/${project.project_id}/sources`, {
    ...draft,
    name: 'Direct unknown browser fixture',
  });
  const path = `/api/projects/${project.project_id}/sources/${source.source_id}`;
  await expect.poll(async () => (await api('GET', path)).status).toBe('paused');
  const before = await api('GET', path + '/unknown-disposition');
  expect(before.can_stop).toBe(true);
  expect(before.unknown_count).toBe(1);
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(String(source.source_id));
  const panel = page.getByRole('region', { name: '未知请求人工处置', exact: true });
  await panel.getByRole('button', { name: '核对并结束此来源' }).click();
  const dialog = page.getByRole('dialog', { name: '确认结束未知请求所在来源' });
  const hold = dialog.getByRole('button', { name: /按住结束来源/ });
  await expect(hold).toBeDisabled();
  await dialog.getByLabel('我确认结束此来源，未提交条目将不再运行').check();
  await expect(hold).toBeDisabled();
  await dialog.getByLabel('我理解未知请求可能已生成或扣费，结束来源不会结清或退款').check();
  await expect(hold).toBeEnabled();
  await hold.focus();
  await page.keyboard.down('Space');
  await page.waitForTimeout(1100);
  await page.keyboard.up('Space');
  await expect(panel.getByText('已保存人工结束决定；上游结果仍未知。')).toBeVisible();
  const after = await api('GET', path + '/unknown-disposition');
  expect(after.source_status).toBe('canceled');
  expect(after.upstream_outcome).toBe('unknown');
  const items = (await api('GET', path + '/items')).items;
  expect(items.map((item: { status: string }) => item.status)).toEqual(['sent', 'pending']);
  expect(items.map((item: { attempts: number }) => item.attempts)).toEqual([1, 0]);
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(String(source.source_id));
  await expect(panel.getByText('已保存人工结束决定；上游结果仍未知。')).toBeVisible();
  expect((await api('GET', path + '/unknown-disposition')).decision).toEqual(after.decision);
  expect(readFileSync(join(base, 'direct-review-request-count'), 'utf8')).toBe('1');
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'f02-manual-unknown.png') });
  expect(errors).toEqual([]);
});

test('项目存储只读扫描，明确预览确认后清理缩略图并保留恢复证据', async ({ page }) => {
  if (!(await api('GET', '/api/projects/current'))) {
    await connect(page);
    await page.getByRole('button', { name: '选择保存位置并创建' }).click();
    await expect.poll(async () => !!(await api('GET', '/api/projects/current'))).toBe(true);
    await page.goto('about:blank');
  }
  const project = await api('GET', '/api/projects/current');
  const root = join(base, 'browser.slproj');
  const cache = join(root, 'cache', 'thumbnails');
  mkdirSync(cache, { recursive: true });
  const target = join(cache, 'f'.repeat(64) + '.webp');
  writeFileSync(target, 'storage browser fixture');
  const evidence = join(root, 'images', '.pending', 'storage-browser');
  mkdirSync(evidence, { recursive: true });
  const receipt = join(evidence, 'receipt.json');
  writeFileSync(receipt, '{"fixture":true}');
  await connect(page);
  await page.getByRole('link', { name: '存储占用与清理', exact: true }).click();
  await page.getByRole('button', { name: '扫描当前项目', exact: true }).click();
  await expect(page.getByRole('region', { name: '扫描结果' })).toBeVisible();
  expect(existsSync(target)).toBe(true);
  await page.getByRole('button', { name: '预览候选文件', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '确认清理候选文件' });
  const hold = dialog.getByRole('button', { name: /按住清理这些文件/ });
  await expect(hold).toBeDisabled();
  expect(existsSync(target)).toBe(true);
  await dialog.getByLabel('我已核对文件范围并理解清理后果').check();
  await hold.focus();
  await page.keyboard.down('Space');
  await page.waitForTimeout(1100);
  await page.keyboard.up('Space');
  await expect(page.getByRole('heading', { name: '清理完成', exact: true })).toBeVisible();
  expect(existsSync(target)).toBe(false);
  expect(readFileSync(receipt, 'utf8')).toBe('{"fixture":true}');
  await page.getByRole('button', { name: '扫描当前项目', exact: true }).click();
  await expect(page.getByRole('button', { name: '扫描当前项目', exact: true })).toBeEnabled();
  const summary = await api('GET', `/api/projects/${project.project_id}/storage/summary`);
  expect(summary.scan.partial).toBe(false);
  await page.screenshot({ path: join(temporary, 'project-storage.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('关闭项目后扫描最近项目并使用持久编号重新打开', async ({ page }) => {
  if (!(await api('GET', '/api/projects/current'))) {
    await connect(page);
    await page.getByRole('button', { name: '选择保存位置并创建' }).click();
    await expect.poll(async () => !!(await api('GET', '/api/projects/current'))).toBe(true);
    await page.goto('about:blank');
  }
  const original = await api('GET', '/api/projects/current');
  expect(original.known_id).toBeTruthy();
  const closed = await api('POST', `/api/projects/${original.project_id}/close`, {}, original.version);
  await expect.poll(async () => (await api('GET', closed.status_url)).status).toBe('done');
  await connect(page);
  await expect(page.getByRole('region', { name: '最近项目' })).toBeVisible();
  await page.getByRole('link', { name: '存储占用与清理', exact: true }).click();
  const storage = page.getByRole('region', { name: '跨项目存储统计' });
  await storage.getByRole('button', { name: '扫描最近项目', exact: true }).click();
  await expect(storage.getByRole('heading', { name: /已统计/ })).toBeVisible();
  await expect(storage.getByText('browser.slproj', { exact: true })).toBeVisible();
  await expect(storage.getByRole('cell', { name: /^已扫描(?: · \d+ 个硬链接文件)?$/ })).toBeVisible();
  expect(await api('GET', '/api/projects/current')).toBeNull();
  await page.screenshot({ path: join(temporary, 'known-project-storage.png'), fullPage: true });
  await page.goto('http://' + credentials.authority + '/#/');
  const recent = page.getByRole('region', { name: '最近项目' });
  await recent.getByRole('button', { name: `打开 ${original.metadata.name}`, exact: true }).click();
  await expect(page).toHaveURL(/\/workbench$/);
  const reopened = await api('GET', '/api/projects/current');
  expect(reopened.known_id).toBe(original.known_id);
  expect(reopened.project_id).not.toBe(original.project_id);
  expect(errors).toEqual([]);
});

test('存储阈值持久保存，跨页提醒保留旧阈值并在重扫后更新', async ({ page }) => {
  if (!(await api('GET', '/api/projects/current'))) {
    await connect(page);
    await page.getByRole('button', { name: '选择保存位置并创建' }).click();
    await expect.poll(async () => !!(await api('GET', '/api/projects/current'))).toBe(true);
    await page.goto('about:blank');
  }
  const project = await api('GET', '/api/projects/current');
  writeFileSync(join(base, 'browser.slproj', 'threshold-fixture.bin'), Buffer.alloc(2 * 1024 ** 2));
  await connect(page);
  await page.getByRole('link', { name: '存储占用与清理', exact: true }).click();
  await page.getByLabel('单项目提醒阈值（MiB）').fill('1');
  await page.getByLabel('扫描范围总量提醒阈值（MiB）').fill('1');
  await page.getByRole('button', { name: '保存提醒设置', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存提醒设置', exact: true })).toBeDisabled();
  await expect
    .poll(async () => (await api('GET', '/api/config')).storage_thresholds.project_bytes)
    .toBe(1024 ** 2);
  await page.getByRole('button', { name: '扫描当前项目', exact: true }).click();
  await expect(page.getByRole('region', { name: '扫描结果' })).toBeVisible();
  const known = page.getByRole('region', { name: '跨项目存储统计' });
  await known.getByRole('button', { name: '扫描最近项目', exact: true }).click();
  await expect(known.getByRole('heading', { name: /已统计/ })).toBeVisible();
  await page.getByRole('link', { name: '项目概览', exact: true }).click();
  const notice = page.getByRole('status', { name: '存储占用提醒' });
  await expect(notice).toContainText('当前项目上次扫描');
  await expect(notice).toContainText('已超过阈值');
  const before = await api('GET', '/api/storage/summary');
  const config = await api('GET', '/api/config');
  await api(
    'PATCH',
    '/api/config',
    { storage_thresholds: { project_bytes: 20 * 1024 ** 3, total_bytes: 100 * 1024 ** 3 } },
    config.version,
  );
  await expect(notice).toContainText('阈值已修改，请重新扫描。');
  await expect(notice).toBeInViewport();
  await page.locator('#main').evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(notice).toBeInViewport();
  const changed = await api('GET', '/api/storage/summary');
  expect(changed.current_project.scan_id).toBe(before.current_project.scan_id);
  expect(changed.current_project.project_threshold_bytes).toBe(1024 ** 2);
  expect(changed.known_projects.scan_id).toBe(before.known_projects.scan_id);
  await page.screenshot({ path: join(temporary, 'storage-threshold-notice.png'), fullPage: true });
  await notice.getByRole('link', { name: '查看存储与重新扫描' }).click();
  await expect(page.getByLabel('单项目提醒阈值（MiB）')).toHaveValue('20480');
  await page.getByRole('button', { name: '扫描当前项目', exact: true }).click();
  await expect
    .poll(
      async () => (await api('GET', `/api/projects/${project.project_id}/storage/summary`)).config_changed,
    )
    .toBe(false);
  await known.getByRole('button', { name: '扫描最近项目', exact: true }).click();
  await expect
    .poll(async () => (await api('GET', '/api/storage/summary')).known_projects.config_changed)
    .toBe(false);
  await expect(notice).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('恢复原图核验拒绝错误文件，通过后保留审图和标注并可再次导出', async ({ page }) => {
  if (!(await api('GET', '/api/projects/current'))) {
    await connect(page);
    await page.getByLabel('触发词', { exact: true }).fill('restore_fixture_style');
    await page.getByRole('button', { name: '选择保存位置并创建' }).click();
    await expect(page.getByRole('heading', { name: /数据清洗工作台/ })).toBeVisible();
    await page.getByRole('link', { name: '来源与导入', exact: true }).click();
    await page.getByRole('button', { name: '选择图片目录', exact: true }).click();
    await page.getByRole('button', { name: '扫描并预览', exact: true }).click();
    await expect(page.getByRole('button', { name: '确认导入', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '确认导入', exact: true }).click();
    await expect(page.getByText('导入结束：成功 6', { exact: false })).toBeVisible();
    await page.goto('about:blank');
  }
  const project = await api('GET', '/api/projects/current');
  const path = `/api/projects/${project.project_id}`;
  const listing = await api('GET', path + '/images');
  const row = listing.items.find((item: { file_missing: boolean }) => !item.file_missing);
  expect(row).toBeTruthy();
  const file = join(base, 'browser.slproj', 'images', row.filename);
  const backup = readFileSync(file);
  const annotations = await api('GET', path + `/annotations/${row.image_id}`);
  await api('POST', path + '/images/review', {
    ids: [row.image_id],
    target: 'reject',
    expected_versions: { [row.image_id]: row.version },
  });
  const previewTask = await api('POST', path + '/storage/cleanup/preview', { kind: 'rejected_files' });
  await expect.poll(async () => (await api('GET', previewTask.status_url)).status).toBe('done');
  const preview = (await api('GET', previewTask.status_url)).result;
  const cleanupTask = await api('POST', path + '/storage/cleanup', {
    preview_id: preview.preview_id,
    digest: preview.digest,
    confirm: true,
  });
  await expect.poll(async () => (await api('GET', cleanupTask.status_url)).status).toBe('done');
  expect(existsSync(file)).toBe(false);
  writeFileSync(file, Buffer.alloc(backup.length));
  await connect(page);
  await page.getByRole('link', { name: '数据工作台', exact: true }).click();
  await page.getByRole('checkbox', { name: '文件缺失', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '文件缺失', exact: true })).toBeChecked();
  await page.locator(`[data-image-id="${row.image_id}"]`).click();
  const panel = page.getByRole('region', { name: '恢复原图核验' });
  await expect(panel.getByText(`images/${row.filename}`, { exact: true })).toBeVisible();
  async function submitVerification() {
    // Thumbnail reads may briefly hold the file lease after navigation. The
    // product deliberately fails fast; retry only a confirmed lease rejection
    // through the same explicit UI action, never an unknown write outcome.
    for (let attempt = 0; attempt < 5; attempt++) {
      const response = page.waitForResponse(
        (value) =>
          value.url().endsWith(`/images/${row.image_id}/verify-file`) && value.request().method() === 'POST',
      );
      await panel.getByRole('button', { name: '核验已放回的原图', exact: true }).click();
      const submitted = await response;
      expect(submitted.status()).toBe(202);
      const accepted = await submitted.json();
      await expect
        .poll(async () => (await api('GET', accepted.status_url)).status)
        .not.toMatch(/^(pending|running)$/);
      const operation = await api('GET', accepted.status_url);
      if (operation.error?.code !== 'storage:leased') return;
      await expect(panel.getByRole('alert')).toContainText('项目文件正在读取');
    }
    throw new Error('File lease remained busy after five confirmed rejections');
  }
  await submitVerification();
  await expect(panel.getByText(/原图内容已变化/)).toBeVisible();
  expect((await api('GET', path + `/images/${row.image_id}`)).file_missing).toBe(true);
  writeFileSync(file, backup);
  await submitVerification();
  await expect(panel.getByText(/上次原图核验通过/)).toBeVisible();
  await expect
    .poll(async () => (await api('GET', path + `/images/${row.image_id}`)).file_missing)
    .toBe(false);
  await expect(page.locator(`[data-image-id="${row.image_id}"]`)).toHaveCount(0);
  const restored = await api('GET', path + `/images/${row.image_id}`);
  expect(restored.review_status).toBe('reject');
  const after = await api('GET', path + `/annotations/${row.image_id}`);
  expect(after.tags).toEqual(annotations.tags);
  expect(after.refs).toEqual(annotations.refs);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(temporary, 'image-file-verification.png') });
  await page.getByRole('checkbox', { name: '文件缺失', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '文件缺失', exact: true })).not.toBeChecked();
  await expect(page.locator(`[data-image-id="${row.image_id}"] img`)).toBeVisible();
  await page.locator(`[data-image-id="${row.image_id}"]`).click();
  await page.getByRole('button', { name: '✓ 保留 K', exact: true }).click();
  await expect
    .poll(async () => (await api('GET', path + `/images/${row.image_id}`)).review_status)
    .toBe('keep');
  // Earlier recovery fixtures intentionally use the original trigger in their
  // style prompt. Pick a distinct trigger for this final export acceptance.
  const current = await api('GET', path);
  await api('PATCH', path, { trigger_word: 'restore_fixture_style' }, current.version);
  const exporting = await api('POST', path + '/exports/preview', {
    options: { source_ids: [row.source_id], allow_existing_trigger: true },
  });
  await expect
    .poll(async () => (await api('GET', exporting.status_url)).status)
    .not.toMatch(/^(pending|running)$/);
  const exportOperation = await api('GET', exporting.status_url);
  expect(exportOperation.status, JSON.stringify(exportOperation.error)).toBe('done');
  const exportPreview = exportOperation.result;
  const exported = await api('POST', path + '/exports', {
    preview_id: exportPreview.preview_id,
    digest: exportPreview.digest,
  });
  await expect
    .poll(async () => (await api('GET', `/api/operations/${exported.operation_id}`)).status)
    .toBe('done');
  const record = await api('GET', exported.status_url);
  const report = await api('GET', record.report_url);
  expect(report.images.find((item: { image_id: number }) => item.image_id === row.image_id).status).toBe(
    'exported',
  );
  expect(readFileSync(file)).toEqual(backup);
  expect(errors).toEqual([]);
});

test('生成参数能力表单在浏览器中校验、保存和重开', async ({ page }) => {
  await connect(page);
  await expect(
    page
      .getByLabel('项目名称', { exact: true })
      .or(page.getByRole('link', { name: '来源与导入', exact: true }))
      .first(),
  ).toBeVisible();
  if (await page.getByRole('button', { name: '选择保存位置并创建', exact: true }).isVisible()) {
    await page.getByLabel('项目名称', { exact: true }).fill('参数表单验收');
    await page.getByLabel('触发词', { exact: true }).fill('parameter_style');
    await page.getByRole('button', { name: '选择保存位置并创建' }).click();
    await expect(page.getByRole('heading', { name: '数据清洗工作台', exact: false })).toBeVisible();
  }
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await expect(page.getByLabel('步数', { exact: true })).toHaveValue('23');
  await expect(page.getByLabel('采样器', { exact: true })).toHaveAttribute('readonly');
  await page.getByLabel('生成源名称').fill('能力表单浏览器验收');
  await page.getByLabel('步数', { exact: true }).fill('51');
  await expect(page.getByText('允许范围：1 至 50。', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '保存生成源草稿', exact: true }).click();
  await expect(page.getByText('请先修正生成参数表单中的错误。', { exact: true })).toBeVisible();
  await page.getByLabel('步数', { exact: true }).fill('31');
  await page.getByLabel('模型', { exact: true }).selectOption('nai-diffusion-4-5-full');
  await page.getByLabel('提示词引导强度', { exact: true }).fill('6.25');
  await page.getByRole('button', { name: '保存生成源草稿', exact: true }).click();
  await expect(page.getByText('生成源草稿已保存；尚未锁定或提交生成。')).toBeVisible();
  const sourceId = await page.getByLabel('打开生成源').inputValue();
  const project = await api('GET', '/api/projects/current');
  const stored = await api('GET', `/api/projects/${project.project_id}/sources/${sourceId}`);
  expect(stored.config_json.profile).toMatchObject({
    model: 'nai-diffusion-4-5-full',
    steps: 31,
    scale: 6.25,
  });
  expect(stored.status).toBe('draft');
  expect(stored.locked).toBe(false);
  await page.reload();
  await expect(page.getByRole('heading', { name: '重新连接本地服务' })).toBeVisible();
  await page.goto('about:blank');
  await connect(page);
  await page.getByRole('link', { name: '来源与导入', exact: true }).click();
  await page.getByLabel('打开生成源').selectOption(sourceId);
  await expect(page.getByLabel('步数', { exact: true })).toHaveValue('31');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel('模型', { exact: true })).toBeVisible();
  await page.getByLabel('模型', { exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: join(temporary, 'generation-form-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('主要页面在桌面与窄屏的浅深主题无障碍矩阵', async ({ page }) => {
  test.setTimeout(480_000);
  await connect(page);
  const links = [
    '项目概览',
    '数据工作台',
    '来源与导入',
    '导出数据集',
    '导入导出方案',
    '规则操作日志',
    '近似重复扫描',
    'WD 标注任务',
    '标注批次',
    '视觉描述任务',
    '任务进度',
    '提示词生产',
    '本地标签库',
    '替换规则库',
    'Launcher / MCP',
    'NovelAI 直连',
    '尺寸预设',
    'WD 模型管理',
    'LLM 连接与角色',
    '视觉关键词',
    '存储占用与清理',
    '界面设置',
  ];
  await page.evaluate(readFileSync(resolve('node_modules/axe-core/axe.min.js'), 'utf8'));
  const matrix = [];
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ['light', 'dark']) {
      await page.getByRole('link', { name: '界面设置', exact: true }).click();
      await page.getByLabel('外观', { exact: true }).selectOption(theme);
      for (const name of links) {
        const link = page.getByRole('link', { name, exact: true });
        const target = await link.getAttribute('href');
        await link.click();
        await expect.poll(() => new URL(page.url()).hash).toBe(target!.slice(target!.indexOf('#')));
        await expect(page.locator('main h1')).toBeVisible();
        const result = await page.evaluate(async () => {
          const axe = (
            window as unknown as {
              axe: {
                run: () => Promise<{
                  violations: { id: string; impact: string; nodes: { target: string[] }[] }[];
                }>;
              };
            }
          ).axe;
          const findings = (await axe.run()).violations.filter((v) =>
            ['serious', 'critical'].includes(v.impact),
          );
          return { overflow: document.documentElement.scrollWidth > innerWidth, findings };
        });
        matrix.push({ width, theme, name, ...result });
        writeFileSync(join(temporary, 'completion-accessibility.json'), JSON.stringify(matrix, null, 2));
      }
    }
  }
  writeFileSync(join(temporary, 'completion-accessibility.json'), JSON.stringify(matrix, null, 2));
  expect(matrix.filter((row) => row.overflow || row.findings.length)).toEqual([]);
  await page.getByRole('link', { name: '界面设置', exact: true }).click();
  await page.getByLabel('外观', { exact: true }).selectOption('light');
  await page.getByLabel('外观', { exact: true }).focus();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('工作台密度', { exact: true })).toBeFocused();
  expect(errors).toEqual([]);
});

test('真实万图项目的分页、虚拟滚动和帧时间', async ({ page }) => {
  test.skip(!process.env.STYLELORA_SCALE_REPORT, 'Opt-in real-file scale acceptance');
  test.setTimeout(240_000);
  const report = JSON.parse(readFileSync(process.env.STYLELORA_SCALE_REPORT!, 'utf8'));
  expect(report.passed).toBe(true);
  const current = await api('GET', '/api/projects/current');
  if (current) {
    const accepted = await api('POST', `/api/projects/${current.project_id}/close`, {}, current.version);
    if (accepted.status_url)
      await expect.poll(async () => (await api('GET', accepted.status_url)).status).toBe('done');
  }
  const grant = JSON.parse(
    execFileSync(python, ['-m', 'tests.web_fixture', 'credentials'], {
      cwd: repo,
      env: environment,
      windowsHide: true,
      encoding: 'utf8',
      input: JSON.stringify({ pid: child.pid, scale_project: report.project }),
    }),
  );
  const openStarted = performance.now();
  const accepted = await api('POST', '/api/projects/open', { grant_id: grant.grant_id });
  await expect
    .poll(async () => (await api('GET', accepted.status_url)).status, { timeout: 120_000 })
    .toBe('done');
  const openSeconds = (performance.now() - openStarted) / 1000;
  await connect(page);
  await page.getByRole('link', { name: '数据工作台', exact: true }).click();
  const grid = page.getByRole('grid', { name: '图片列表' });
  await expect(grid.getByRole('gridcell').first()).toBeVisible();
  const frames = await grid.evaluate(async (element) => {
    const intervals: number[] = [];
    let last = performance.now(),
      peak = 0;
    const start = last;
    await new Promise<void>((resolve) => {
      function step(now: number) {
        intervals.push(now - last);
        last = now;
        element.scrollTop += 35;
        peak = Math.max(peak, element.querySelectorAll('[role="gridcell"]').length);
        if (now - start < 8000) requestAnimationFrame(step);
        else resolve();
      }
      requestAnimationFrame(step);
    });
    intervals.sort((a, b) => a - b);
    return {
      p95_ms: intervals[Math.floor(intervals.length * 0.95)],
      frames: intervals.length,
      peak_cells: peak,
    };
  });
  expect(frames.p95_ms).toBeLessThan(50);
  expect(frames.peak_cells).toBeLessThan(200);
  expect(frames.frames).toBeGreaterThan(160);
  await page.getByLabel('图片排序', { exact: true }).selectOption('-id');
  await grid.evaluate((element) => {
    element.scrollTop = 0;
  });
  await expect(grid.locator('[data-image-id="10000"]')).toBeVisible();
  writeFileSync(
    join(temporary, 'completion-browser-scale.json'),
    JSON.stringify({ project: report.project, open_seconds: openSeconds, ...frames }, null, 2),
  );
  expect(errors).toEqual([]);
});
